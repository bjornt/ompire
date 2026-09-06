# Epic: Durable task results and explicit handoffs

## Outcome

Exploration is a useful, complete task outcome without a source commit, push,
or PR. An operator can inspect and retain its files in Ompire, deliberately
export them, or start another isolated task with an exact revision of those
files. Cleaning up the producing task does not destroy the accepted result.

A planning task can produce `epics/<name>/EPIC.md` or a change's `SPEC.md` and
`PLAN.md`; a later task can consume them without putting temporary coordination
files on the mainline. This is a high-level proposal. Storage layout and the
precise import/export contract are decided in child proposals.

## Vision alignment

Implements [the vision](../../docs/VISION.md)'s artifacts, exploratory work,
durable results, and explicit trusted export. Preserves task isolation: tasks
exchange immutable inputs rather than sharing a clone or writable directory.
Advances the artifact slice of ADR-0016 without claiming to implement its full
session-log archive, commit lineage, or retention system.

## Boundaries

### Proposed product model

Separate three things that Git currently tends to conflate:

1. **Workspace:** mutable files where agents work, disposable after safe cleanup.
2. **Result bundle:** immutable captured bytes and a manifest, retained by the
   daemon outside the workspace. The manifest records producing task/run/step
   where available, revision, paths, media types, checksums, and relevant project
   and source-base identity. A description alone is not a captured file.
3. **Delivery action:** human inspection/download, explicit checkout export,
   or attaching the bundle to another task. Git publication is a different
   action and never follows merely because files exist.

A small Markdown/file bundle is the initial product surface, not a generic
package registry or document collaboration system. Capture uses explicit
allowlisted files; do not archive an entire clone, `.git`, native session store,
credentials, or arbitrary paths named by the agent. Apply resource limits and
safe path handling at the trusted boundary. A valid manifest/checksum proves
identity, not factual correctness or permission to execute its content.

Proposed workflow:

- The producing task offers a result for review. The operator sees actual bytes
  and a file list/diff, with producing evidence and capture errors.
- Accepting identifies a specific immutable revision. New edits create a new
  revision; acceptance never floats to the latest files.
- **Start task from this result** opens the ordinary launch form with explicit
  input attachments and workflow/project/model-profile choices. The target
  task pins those revisions alongside its other accepted inputs.
- The daemon materializes reviewed files inside the target task's own workspace
  before agent execution. No link back to the producer's mutable files, clone,
  container, or session is required. The recipient has fresh sessions; useful
  context travels as explicit evidence, not a cross-task session transplant.
- The recipient may revise a plan and offer a successor result. It does not
  overwrite the producer's bundle or silently update another running consumer.

### Keeping planning material out of Git

Handoff inputs are non-publishable by default. They can appear at the repository
paths expected by the planning skills, but their classification and provenance
remain daemon-owned. Ordinary code changes produced from a plan can be reviewed
and shipped; the plan itself must not accidentally ride along.

Clone-local excludes alone are insufficient: tracked files and agent checkpoint
commits can still carry content, and the current squash publisher stages the
whole delta. Both squash and retained-history publication must inspect the
actual proposed Git result and reject accidental inclusion of protected handoff
paths. Do not silently delete files, rewrite away an operator's work, or pretend
a path exclude proves that retained commits are clean. A publishability change
requires explicit human review and new approval of the affected delivery.

“No publication,” “no durable source commit,” and “no agent-local checkpoint
commit anywhere” are different guarantees. This epic needs no commit to capture,
retain, or pass a result, and exploratory examples request none. It does not
claim to prohibit every invocation of Git inside an agent's sandbox. If literal
prohibition of agent-local commits is required, that needs an enforceable sandbox
capability design, not a prompt saying “do not commit.” Temporary checkpoint
history is not the result store and must never make a planning artifact eligible
for mainline publication.

### Delivery boundaries and unresolved choices

- Ompire inspection and explicit task attachment are the default. Export into
  the operator's checkout is optional, previewed, conflict-aware, and host-owned;
  ordinary task execution never writes there.
- Initially attach within one project. Record the producer base and preview
  target-base differences. A plan can remain relevant after the base changes,
  but cannot be presented as validated against the new base. Conflicting paths
  require a human decision before the recipient starts.
- Bundles preserve relative paths and internal links. Exact destination mapping,
  supported file/media limits, and conflict choices belong to child proposals.
- Workspace cleanup and durable-result purge are distinct actions. Referenced
  revisions cannot disappear silently; choose the precise retention and purge
  UX before the first storage change is accepted.
- A later task may consume a selected revision; automatically starting a train
  of tasks, rebasing dependent code branches, merging plans, cross-project
  transfer, live document synchronization, and artifact marketplaces are out.
- Downloading content never executes it. Render agent-authored Markdown and
  other supported previews as untrusted content.

### Relationship to editable workflows

The sibling [editable-workflows epic](../editable-workflows/EPIC.md) owns
versioned authoring, state-machine UX, and review/Git/forge step composition.
This epic owns artifact capture, storage, review, export, attachment, and the
non-publishable-path contract, not another workflow editor or publisher.

It is independently deliverable against today's task UI and workflow engine.
Capture and consumption are trusted task operations with step provenance when
invoked by a workflow. The final child supplies a complete exploratory workflow
through the definition mechanism supported at implementation time. If editable
workflows have landed, register these operations in that vocabulary and editor;
otherwise use the current built-in mechanism. Do not introduce a private YAML
interpreter, placeholder plugin API, or require the other epic to be complete.
When selecting that child, explicitly reconcile whichever mechanism is current.

### Repository evidence and ownership

- `daemon/src/ompire_daemon/workflows.py:read_outcome` validates a small JSON
  envelope. Its `artifacts` map contains workflow-defined values, not a durable
  blob/file store; `.ompire/` still lives inside the disposable clone.
- [Workflow behavior](../../docs/use/reference/workflow-engine.md) keeps a
  completed workspace alive until cleanup. It does not make those files a
  durable result independent of the workspace.
- `daemon/src/ompire_daemon/ship.py:_stage_delta` uses `git add --all` and
  `commit_and_ship` supports squash and retain. Carrying plans into a later
  code task therefore requires trusted publication checks, not just a new
  download button or an instruction for the coder to remember cleanup.
- `launch.py` and `execution_inputs.py` own pinned launch inputs;
  `spawn.py` owns workspace preparation. Attachments must enter those existing
  consistency and failure boundaries before an agent starts.
- `frontend/src/routes/TaskDetailView.tsx` and `SpawnView.tsx` are the existing
  result-inspection and launch integration points, with daemon-owned state.
- ADR-0006, ADR-0009, ADR-0011, ADR-0016, and ADR-0026 constrain isolation,
  evidence, publishing, retention, and input pinning. No active epic/change
  directories existed during initial research; recheck ownership at selection.

## Changes

### [ ] 1. capture-and-inspect-durable-task-results

Add immutable result capture, provenance, owner-private storage, and the task
UI for inspecting, accepting, and downloading a revision. Establish cleanup,
retention, and purge semantics together so the first usable result cannot be
lost merely because its workspace is removed. Retain honest gaps for older
results whose producing step or source identity was not recorded.

- Depends on: None
- Acceptance: an operator captures a selected planning directory, reviews the
  actual files, accepts a revision, restarts Ompire, cleans up the source task,
  and still reads/downloads exactly the accepted content. Later workspace edits
  do not mutate it. Failed/incomplete capture is visible and cannot be passed
  off as a complete accepted bundle. Unsafe paths, symlink escapes, forbidden
  metadata, and oversized inputs are rejected at the daemon boundary.
- Verification: browser capture/review/download journey, real local capture
  and checksum comparison before/after restart and cleanup, and targeted
  adversarial capture and explicit-purge checks.

### [ ] 2. launch-tasks-from-pinned-results

Add explicit bundle attachments to launch preview/acceptance and safe workspace
materialization, with a Start task action from an accepted result. Include the
publication boundary in this slice: handing a plan to a code task is not safe
until the existing ship paths cannot accidentally publish it.

- Depends on: capture-and-inspect-durable-task-results
- Acceptance: a second isolated task consumes the exact reviewed revision after
  its producer was cleaned up. Its preview records files, source/base context,
  destinations, and non-publishable classification; stale selections or path
  conflicts cannot start an agent under unreviewed inputs. Updated source
  bundles do not alter the recipient. A recipient can ship intended code while
  planning inputs stay out of the proposed tree and retained commit history;
  detected contamination blocks publication with an actionable explanation.
- Verification: browser result → launch → agent-reads-plan journey; compare
  materialized bytes to the accepted bundle, including producer cleanup and
  failed/restarted materialization. Exercise conflicts, stale preview, tracked
  paths, checkpoint contamination, and squash/retain publishing refusal using
  existing `daemon/tests/test_ship.py` and local ship scenarios as appropriate.

### [ ] 3. export-results-with-preview-and-conflict-control

Offer an explicit alternative to task attachment: export selected result files
into an allowlisted destination in the operator's project checkout through the
trusted daemon. This is deliberate file delivery, not a commit or a merge.
Reuse the capture/attachment manifest and review identity rather than a second
file-selection format.

- Depends on: capture-and-inspect-durable-task-results
- Acceptance: preview shows the exact revision, destinations, creates/changes,
  and conflicts. Approval applies only to that preview; a changed destination
  requires renewed review. Existing files are never silently overwritten;
  traversal/symlink escapes are rejected. Interruption has a recoverable,
  visible result rather than an unexplained partial export. No Git or forge
  action is implied by export.
- Verification: real-browser export to an isolated local checkout, filesystem
  comparison to the preview, conflict and stale-approval checks, and interrupted
  export recovery. Never use the actual developer checkout as a test target.

### [ ] 4. deliver-exploration-and-planning-handoffs

Provide an exploratory/planning workflow that ends by offering a durable result
for human review, not by sending the operator to Ship flow. Compose the earlier
operations into an end-to-end proposal → acceptance → downstream-task journey,
with producing step/evidence links and explicit no-publication behavior. Use
current lightweight epic/change artifacts rather than reviving OpenSpec.

- Depends on: capture-and-inspect-durable-task-results, launch-tasks-from-pinned-results
- Acceptance: an agent creates an epic or change proposal; the operator reviews
  and accepts it in Ompire; a later task reads those files and can produce a
  separately accepted successor revision or implement the proposal. Producer
  cleanup loses neither result nor lineage. Completion requires no source
  commit or PR, and no published artifact appears merely because a task ended.
  If the declarative editor exists, this same flow is authorable there without
  special Python prompt/route hooks or a parallel artifact execution path.
- Verification: local E2E browser planning → review → cleanup → downstream
  launch journey, including rejection/revision, missing capture, restart, and
  attempts to publish an exploration-only result. Reconcile task lifecycle,
  workflow, publishing, artifact, and launch documentation and relevant ADRs.

## Completion

Demonstrate a planning task that produces useful epic/change files without
committing or publishing them, makes them inspectable to a human, survives
workspace cleanup, and transfers an accepted revision into another isolated
task. Show that source edits cannot silently alter an accepted result or a
consumer's inputs. Show a successor result without rewriting the original.

Demonstrate explicit previewed export and safe handling of destination changes.
Demonstrate that a downstream implementation can publish its intended code while
handoff-only files cannot leak through squash or retained-history shipping.
Durable provenance and clear capture/conflict failures must explain each action.

The browser and filesystem/Git evidence above, operator/contributor documentation,
and an artifact lifecycle ADR must agree on current behavior and retention.
Reconcile only the delivered portion of ADR-0016 and extend ADR-0026's pinned
input contract. No child requires automated roadmaps or another epic's delivery.
`capture-and-inspect-durable-task-results` is ready for `epic-propose-next`;
remaining UX/schema/storage choices are explicitly deferred to child proposals.
