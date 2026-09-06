# Epic: Editable, durable workflows

## Outcome

An operator can understand, create, edit, and launch a workflow through Ompire's
UI without modifying Python or deploying the daemon. A portable YAML document
represents the same definition the UI edits. Each accepted task retains its
exact definition revision, named agent sessions, evidence, routing decisions,
and human approvals through edits and restarts.

Workflows own the procedure through its chosen ending: stop with a useful
result, make a local signed commit, push a branch, or open a pull request.
Finishing agent work does not implicitly authorize publishing.

This is a high-level proposal, not an accepted YAML schema or an implementation
plan. The change entries are independently specifiable delivery boundaries.

## Vision alignment

Advances [the vision](../../docs/VISION.md)'s workflow engine, explicit
uncertainty, durable runs, and scarce-human-attention principles. Retains the
task-owned workspace and named-session model of ADR-0008, the daemon's trusted
publishing boundary, and ADR-0026's immutable launch resolution. Operator
creation of definitions triggers ADR-0018's explicit requirement to supersede
the Python-only representation decision.

## Boundaries

### Proposed product model

- **Workflow definition:** named agents, steps, declared inputs and outcomes,
  transitions, human gates, bounded repetition, and allowed delivery actions.
  YAML is a serialization, not executable Python or a general scripting language.
- **Agent:** a workflow-local name mapped to a durable task session. QA can own
  both reproduction and verification; a different agent owns diagnosis/fixing.
  Reusing a name reuses the native conversation, not a newly synthesized summary.
  Context can still be compacted by the native agent; this is not a promise of
  unlimited model context. Session loss is visible, never silently a fresh start.
- **Model role:** separate from the agent's identity. Keep the existing abstract
  model-role/profile resolution and accepted per-step model policy rather than
  creating another model configuration system in YAML.
- **Step attempt:** one execution with attributable inputs, outputs, and result.
  Re-entering a step creates another attempt; validation must refer to the fix
  attempt it actually checked, not an older successful result.
- **Transition:** the daemon chooses a declared successor from validated data.
  Distinguish work outcomes such as `not_reproduced` or `no_root_cause` from
  infrastructure failure, missing evidence, waiting for a human, and completion.
- **Gate:** a question with named choices, evidence, and explicit destinations,
  not merely a Resume button. Record the choice and feedback. Waiting is not
  successful completion; stopping without a fix must not claim the bug was fixed.
- **Delivery:** review and privileged Git/forge actions are typed workflow steps
  using trusted daemon services. A definition requests authority; it cannot grant
  itself credentials or bypass project/operator policy.

Execute one step at a time in the task's shared clone. Branches and bounded
loops are in scope; concurrent steps, joins, nested workflows, a plugin platform,
marketplace, automated roadmap scheduling, and changing publishing identity are
not. Do not turn this into a general CI system.

### Authoring experience

Use a first-class Workflows library with built-in examples, duplication, drafts,
and immutable saved revisions. Start with an ordered flow of step cards and an
agents panel. Each card exposes its kind, assigned agent, instruction, required
outcome, and what happens next. Branches have readable outcome labels; loops
show their bound and exhaustion destination. Offer an overview diagram without
requiring the operator to wire a free-form node canvas for an ordinary sequence.

The visual editor and YAML import/export use one validated semantic model.
Editing does not require YAML. Invalid drafts can be saved but not launched;
errors point to the offending step/field. Unsupported format versions are
rejected rather than silently dropping fields. Formatting/comment preservation
is not promised; semantic round-trip fidelity is.

Launch preview shows the exact revision, agents/models, possible branches,
gates, and privileged actions. Task detail shows that pinned flow with the
current attempt, evidence, decisions, and session links. Editing the library
never edits a running task.

### Worked example and open product choices

Agreed bugfix default:

1. QA attempts reproduction and records what it tried, the observed behavior,
   and any missing prerequisites.
2. Both reproduced and not-reproduced outcomes continue to code diagnosis.
   The diagnosing agent receives the explicit reproduction status and evidence,
   including failed attempts; inability to reproduce is not evidence that no
   bug exists and must not disappear from the handoff.
3. If diagnosis identifies a candidate bug after failed reproduction, send its
   code findings, suspected trigger, and suggested reproduction back to QA in
   the original session. QA attempts reproduction again against the unfixed
   code with that new information, before implementation changes the behavior.
   A code-level hypothesis alone must not be relabeled as a reproduced bug.
4. A found root cause with reproduction evidence permits planning/fixing.
   No root cause, or an informed reproduction attempt that still cannot
   demonstrate the bug, pauses for a human decision with the evidence attached.
   The human can supply information and retry, explicitly authorize proceeding
   without reproduction, or stop. Further diagnosis/reproduction cycles have a
   declared bound; they cannot spin indefinitely.
5. QA verifies the fix in its original session using the reproduction evidence.
   A rejected fix loops with the report within an explicit bound; exhaustion
   gates. Any human-authorized path without reproduction retains that limitation
   in its validation evidence rather than claiming a before/after reproduction.
6. Independent review and human approval precede whichever delivery actions the
   workflow declares. Review rejection returns actionable feedback to the flow.

Non-reproduction routing remains workflow policy, not an engine rule. Authors
may choose an earlier human gate, but the standard bugfix flow investigates
first and returns new findings to QA before fixing. Exact gate choices, retry
bounds, editor layout, outcome schemas, predicate syntax, and revision naming
are refined in child proposals. This agreed direction does not block proposing
the first change.

### Relationship to task artifacts

The sibling [task-artifacts epic](../task-artifacts/EPIC.md) owns durable file
bundles, human inspection/export, and cross-task consumption. This epic does not
build a second artifact store. It can deliver editable code workflows and a
non-publishing terminal result without that sibling: a result remains inspectable
in task history, without promising file survival after workspace cleanup.

The composition boundary is task/run/step provenance and independently callable
trusted delivery services. Once both surfaces exist, expose artifact operations
through the same workflow vocabulary/editor; that convergence must be explicitly
scoped when selecting the relevant child, not treated as an undeclared cross-epic
dependency or duplicated implementation. Each epic remains useful on its own.

### Repository evidence and migration obligations

- `daemon/src/ompire_daemon/workflows.py` contains `Workflow`, Python prompt and
  route callables, the registry/catalog, `WorkflowRunner`, and both built-ins.
  `bugfix` already reuses `reproducer` for reproduction and agent validation.
- `daemon/src/ompire_daemon/registry/workflows.py` persists run position and
  attempt history. Name-only lookup is not immutable executable semantics.
- `daemon/src/ompire_daemon/launch.py:launch_fingerprint` covers a descriptor,
  roles, and resolved inputs, not the full prompt/routing definition. Extend
  this consistency boundary rather than replacing accepted launch resolution.
- `frontend/src/routes/SpawnView.tsx` consumes a read-only catalog;
  `TaskDetailView.tsx` shows executed steps and a resume gate. Neither is an
  authoring surface today.
- `ship.py:commit_and_ship` currently combines signing, push, and PR creation;
  most ship state is transient. `review.py` and `ship.py` remain the trusted
  operation owners, not logic to copy into a YAML interpreter.
- [Workflow reference](../../docs/use/reference/workflow-engine.md),
  [bugfix reference](../../docs/use/reference/bugfix-workflow.md), ADR-0008,
  ADR-0009, ADR-0011, ADR-0016, ADR-0018, and ADR-0026 constrain the change.
  The hidden judge fallback in the current engine must become explicit or be
  removed, not disappear from authoring while continuing to influence routing.
- No active `epics/` or `changes/` artifacts existed during proposal research;
  legacy roadmaps and archived OpenSpec material are context, not duplicate
  live ownership. Recheck active work when selecting each child.

## Changes

### [x] 1. pin-declarative-workflow-revisions

Introduce the constrained, versioned definition model and durable revision
identity, and migrate the built-in workflows to it. Preserve the current
usable launch path while removing Python callables as the definition format.
This first slice provides executable built-ins, not just a schema. Include the
bounded data references, outcome predicates, and prompts needed for their real
behavior. Supersede ADR-0018 and reconcile the relevant ADR-0026 boundary.

- Depends on: None
- Acceptance: new tasks execute retained declarative revisions; editing a
  prompt or route invalidates an old launch preview. Active legacy runs have
  an explicit, checked continuation migration or a visible confirmation block,
  never an invented historical revision or lookup against today's definition.
  Unsupported semantics fail visibly; built-ins no longer require a parallel
  Python-definition path after cutover.
- Verification: exercise built-in launches and restart recovery with the local
  harness; preserve meaningful `daemon/tests/test_workflows.py` and launch
  reconciliation coverage. Prove a definition edit cannot alter an accepted run.

### [~] 2. declare-outcome-routing-and-human-decisions

Make domain outcomes, data handoffs, bounded loops, and structured gates fully
expressible and inspectable. Extend task detail to answer gates with explicit
choices. Any semantic judge is a declared, recorded step; unknown or malformed
evidence cannot silently become success. Separate diagnosis and its no-root-cause
stop in the bugfix example, with a findings-driven return to reproduction before
fixing when the initial attempt could not reproduce.

- Depends on: pin-declarative-workflow-revisions
- Acceptance: reproduced, not-reproduced, no-root-cause, malformed-result,
  rejected-fix, and exhausted-loop paths have explicit observable results;
  failed reproduction reaches diagnosis with its negative evidence intact.
  A candidate bug returns code findings to the original QA session for another
  reproduction attempt against unfixed code. No root cause or continued
  non-reproduction gates rather than silently permitting a fix.
  QA verification also reuses that session. Human choice and feedback select
  only permitted destinations and survive restart. Repeated or stale gate
  submissions cannot advance a different attempt or grant new authority.
- Verification: local E2E browser exercise of initial non-reproduction →
  diagnosis → informed reproduction → fix → verification, plus no-root-cause
  and still-not-reproduced gates. Check evidence handoffs, QA continuity, and
  restart during the return to QA and while gated; targeted regressions cover
  stale evidence, loop bounds, and gate replay. Reconcile ADR-0009's
  implicit-judge conflict.

### [ ] 3. create-and-manage-workflow-library

Add daemon-owned definition CRUD and a first-class UI library to inspect flows,
create/duplicate drafts, edit/import YAML, validate, save executable revisions,
export, and archive entries. This is a complete expert authoring path before
the form-based builder, not an API-only registry. Catalog changes reach clients
through the existing REST/snapshot-and-delta architecture.

- Depends on: declare-outcome-routing-and-human-decisions
- Acceptance: an operator creates a custom workflow without daemon edits or
  restart, launches it from Spawn, then edits or archives its library entry
  without changing the old run. Drafts cannot execute; referenced revisions
  remain available. Invalid YAML, unsafe constructs, and stale edits receive
  actionable errors without breaking the catalog or daemon startup.
- Verification: browser create → validate → save → launch → edit → reconnect;
  import/export semantic round trip and conflicting-edit/version checks.

### [ ] 4. build-visual-workflow-authoring

Provide the intuitive step-card builder and named-agent editor over the same
library, including branches, bounded loops, outcome requirements, prompts with
explicit input references, and human choices. Surface the same flow in launch
preview and task detail, with future routes distinct from executed attempts.

- Depends on: create-and-manage-workflow-library
- Acceptance: without writing YAML, create a QA → diagnosis/fix → QA workflow,
  configure the diagnosis → informed reproduction route, a no-root-cause gate,
  and rejected-fix loop, launch it, and inspect evidence and the reason for its
  current route. YAML and visual edits preserve all supported behavior.
  Invalid references cannot be hidden by the diagram.
- Verification: real-browser creation and execution at desktop and narrow
  widths, including keyboard interaction, session links, validation errors,
  and revisiting an older pinned revision after library edits.

### [ ] 5. make-trusted-delivery-recoverable-and-selectable

Separate the existing trusted publishing operation into explicit local commit,
push, and PR actions with durable authorization/progress and operation-specific
reconciliation. Keep the existing Ship flow usable through those same services;
do not create a second publisher. Review approval must identify the actual
revision being delivered and be invalidated when that content changes.

- Depends on: pin-declarative-workflow-revisions
- Acceptance: an operator can stop after local commit or push without creating
  a PR. Restart at each side-effect boundary either reconciles the observed
  result or asks for a human decision, never blindly repeats signing or forge
  writes. Existing review, signing, target, squash/retain, and credential checks
  apply equally to direct service/API callers and UI actions.
- Verification: exercise existing ship scenarios and targeted interrupted
  commit/push/PR scenarios with local fakes; use the browser for selectable
  endings. Advance only this delivery slice of ADR-0016, not a claim to have
  solved full commit lineage or transcript retention.

### [ ] 6. compose-review-and-delivery-in-workflows

Expose review and selectable trusted delivery actions in definitions and the
visual editor. Complete the bugfix example through review, human approval, and
publishing, while allowing workflows to end without any publish action. Route
manual review/ship controls through the same run authorization so they cannot
bypass a gate or publish while an agent mutates the workspace.

- Depends on: declare-outcome-routing-and-human-decisions, build-visual-workflow-authoring, make-trusted-delivery-recoverable-and-selectable
- Acceptance: authored workflows can end with no publication, a local signed
  commit, a pushed branch, or a PR, exactly as previewed and authorized. Review
  rejection feeds a bounded correction path and revalidation. A non-publishing
  workflow refuses privileged publication through every entry point. The UI
  distinguishes completed work, waiting approval, and published work.
- Verification: local E2E browser runs for no-publication and PR endings,
  review rejection and correction, denied bypass attempts, and restart around
  approval/publication; reconcile workflow, review, shipping, and launch docs.

## Completion

An operator creates a new branching workflow entirely in the UI, shares it as
YAML, launches it, and follows QA reproduction → diagnosis/fix → verification
in the original QA session. Initial non-reproduction reaches diagnosis with
negative evidence intact; a candidate bug sends QA back to reproduce against
unfixed code using the new findings. Absent root cause or continued
non-reproduction waits for a real human choice, and no hypothesis is silently
promoted to reproduction evidence. Review and publishing are visible workflow
steps, not mandatory post-processing or hidden agent commands.

Editing or archiving the definition and restarting the daemon cannot change an
accepted run or duplicate privileged actions. Existing built-in users have a
safe, explicit migration. Every step, gate decision, and delivery action remains
explainable from retained definition and execution records.

Completion evidence includes the browser journeys and interrupted-run scenarios
above, updated operator/contributor documentation, and reconciled ADR-0009,
ADR-0018, ADR-0026, plus the relevant portion of ADR-0016. Do not label proposed
behavior as current until its owning change is delivered. Child selection can
start with `pin-declarative-workflow-revisions`; no product decision blocks that
proposal. Refine later schema and UX choices through their own child proposals.
