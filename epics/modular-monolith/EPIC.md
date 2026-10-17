# Epic: Make Ompire's architectural boundaries visible and enforceable

## Outcome

A maintainer can decide where a change belongs, what it may depend on, which
state it may change, and what must remain true without reading the whole daemon.
An AI making that change receives the same small, explicit contract, with
mechanical checks preventing it from quietly crossing the boundary.

Use a **modular monolith**: one Python daemon process, one SQLite database, one
migration history, direct in-process calls, and the existing React client.
Retain the existing agent/container/tool subprocesses and external authentication
gateway. “One process” describes Ompire's control plane, not moving untrusted
agents or the gateway into it. No internal HTTP services, message broker,
independently deployed packages, or distributed transactions.

This records the reviewed target architecture, not the current module map.
It includes the architecture review that motivates the changes. Child proposals
and delivery state are tracked below; implementation requires a separate invocation.

## Vision alignment

The [four parts in the vision](../../docs/VISION.md#the-four-parts) are useful
product responsibilities, but are not four independent code modules:

| Product part | Proposed implementation ownership |
|---|---|
| Agent isolation | `isolation`; `sessions` uses its execution boundary |
| Parallel task oversight | `work`, `oversight`, and feature-organized frontend surfaces |
| Workflow engine | `workflows`; asks `sessions`, `delivery`, and `artifacts` to perform bounded operations through explicit adapters |
| Refinement loop | Future consumer of workflow history, session evidence, and retained artifacts; not a new engine or an empty package today |

Delivery and retained artifacts deserve visible supporting modules even though
neither is a fifth product pillar. Their authority and lifetime differ from a
workflow's. Session supervision is also separate from isolation: speaking native
omp RPC is not the same responsibility as provisioning a safe place to run it.

The refinement loop can eventually be a workflow using existing execution and
artifact contracts. Its distinctive policy is evidence-based improvement
proposals with human acceptance, not another scheduler. Preserve recorded
identities and explicit missing-history states; this refactor does not implement
the outstanding transcript archive or commit-lineage model.

## Boundaries

### Review: keep the consequential design decisions

The architecture is not an undifferentiated experiment. Important seams already
exist and should survive this work:

- **Accepted intent is immutable.** `launch.py`, `execution_inputs.py`, and
  `taskdefinition.py` distinguish mutable configuration from the exact inputs
  and workflow revision a task accepted (ADRs 0026–0028).
- **Definition semantics are separate from execution.**
  `workflow_definitions.py` has no task or registry dependency. Its large size
  is not, by itself, evidence of bad coupling.
- **Uncertainty and human decisions are explicit.** Frozen evidence, bounded
  routes, and committed gate snapshots avoid interpreting a transcript as
  authority (ADRs 0029–0030).
- **Eligibility is not execution.** `runauthority.py` resolves authority from
  durable state; trusted review and publishing managers perform the operations.
  A caller cannot authorize itself by supplying an `allowed` flag (ADR 0033).
- **Approval identifies protected content.** Candidate capture, review, and
  publishing bind to retained content rather than a moving workspace (ADR 0032).
- **External effects have their own recovery model.** Delivery and checkout
  export record intent and reconcile uncertain effects instead of replaying
  them blindly (ADRs 0033 and 0036).
- **Retained results are not a workspace or publication permission.** Capture,
  acceptance, handoff, export, and publication have different meanings
  (ADRs 0034–0037).
- **The browser is replaceable.** REST commands, snapshot-plus-delta observation,
  and separate session streams are the right outer boundary (ADRs 0002 and 0004).

The recommendation is to expose and enforce these decisions, not replace the
workflow engine, introduce event sourcing, or rewrite the application in a new
framework.

### Review: where structure obscures ownership

Source paths below are relative to `daemon/src/ompire_daemon/`. The inspection
included a Python AST scan of all 57 daemon Python modules; counts include
comments and blank lines and are scale indicators, not quality scores.

| Evidence | Architectural consequence |
|---|---|
| `api/rest.py` is 4,250 lines with 39 distinct local-module dependencies. `spawn_task_route` performs observation, validation, the reserved-write acceptance transaction, and scheduling (around lines 2125–2256). | HTTP routing also owns application use cases. Splitting router files alone would leave this coupling intact. |
| `workflows.py` is 2,604 lines. `_ensure_session` persists native session policy/identity; `_run_command_step` builds `workshop exec` argv and supervises the subprocess (around lines 1933–2004 and 2483–2509). | Workflow sequencing knows execution mechanisms. Changing sandbox execution or session policy can require editing the engine. |
| `delivery.py` owns both candidate capture and `WorkspaceGuard`; its `_ensure_excludes` imports the private `spawn._ensure_git_excludes`. `review.py` imports `spawn.Step` and `_run_step`. | General workspace exclusion and shared execution mechanics live under unrelated product operations. |
| `registry/tasks.py:task_payload` resolves workflow readiness and builds the REST/event shape. `Task` carries task, workspace, run, and PR fields. | Persistence knows presentation, while one broad record hides several state owners. |
| `registry/model_profiles.py:reserved_write` implements the common SQLite write reservation used by workflow, delivery, result, export, task, project, and library mutations. `RoleBinding` also lives in that persistence module. | Low-level transaction mechanics and model values have acquired an accidental dependency on profile storage. |
| `registry/workflows.py:resolve_gate` calls delivery authorization and result verification; `settle_delivery_step` commits delivery completion with the next workflow state. | This is intentional transactional coupling. It needs a named composition owner, not independent commits hidden behind module facades. |
| `app.py:create_app` constructs managers through `app.state` and setter-injects guard, operations, and results into the runner. | Composition exists, but routes can reach almost the whole object graph and partially wired runtime objects are representable. |

The static import graph contains a six-module strongly connected group:
`delivery`, `results`, `review`, `ship`, `spawn`, and `workflows`. It includes local
and `TYPE_CHECKING` imports; it does **not** establish a runtime import failure.
It does show that these names are not currently independent architectural units.
Smaller groups include `registry.tasks`/`taskdefinition` and `agent`/`sessions`.

### Review: make documentation a usable contract

Keep `docs/VISION.md` as the north star. Do not make it the place where a reader
must determine whether a feature is already implemented. Update the existing
architecture overview and daemon module map as boundaries land, rather than
adding a competing architecture handbook.

Specific reconciliation needed:

- `docs/develop/reference/daemon-modules.md` currently calls a single giant REST
  module deliberate because commands use REST. One mutation transport does not
  require one source file or require business transactions to live in routes.
- `docs/develop/explanation/trust-model.md` says gate answers grant no authority
  and describes review/ship as temporary Git rewrites. Its current-behavior
  explanation needs to agree with ADR 0033's declared delivery grants and
  ADR 0032's protected-candidate review. Preserve historical ADRs and their
  format-specific qualifications; correct the current summary.
- The vision's “leaks nothing that matters” is too strong. Source code, task
  inputs, and a live scoped capability can matter even without raw credentials.
  State the guarantee precisely: no raw host/provider credentials or authority
  outside the declared sandbox capabilities. Capability misuse and egress are
  separate risks. This wording correction does not introduce network policy.
- The vision still names OpenSpec outputs/operations in places, while the
  maintained workflow uses lightweight SPEC/PLAN and epic artifacts. Distinguish
  optional external integrations from the project's retired planning workflow.
- Keep current limitations conspicuous: per-workflow network policy, dedicated
  bot-by-default publishing, full transcript retention, and full commit lineage
  are not delivered merely because a target module has a name. See the current
  architecture overview and ADRs 0016–0017.

“Frontend owns nothing” should mean **no authoritative execution or policy
state**. Local form drafts, selection, editor history, stream caches, and
presentation calculations are legitimate frontend responsibilities.

### Target module map

Seven current-behavior modules, plus explicit application composition and small
technical infrastructure. These names are proposed, not new APIs already present.

```text
daemon/src/ompire_daemon/
  app.py                       # construction and FastAPI lifespan only
  api/                         # HTTP/WS auth, decoding, encoding, error mapping
    tasks.py  workflows.py  sessions.py  delivery.py  artifacts.py ...
  application/                 # named cross-module use cases and adapters
    launch.py  gates.py  delivery.py  cleanup.py  recovery.py ...
  work/                        # projects, profiles, task identity, accepted inputs
  isolation/                   # disposable workspace/container and writer ownership
  sessions/                    # named agent sessions, policy handoff, native RPC
  workflows/                   # definition, library, attempts, routing, authority
  delivery/                    # protected candidates, review, publication journals
  artifacts/                   # retained results, attachment, explicit checkout export
  oversight/                   # authoritative read models, attention, advisories
  platform/                    # DB/transactions, config mechanics, bounded I/O helpers
```

Within a module, group by meaningful responsibility, not a mandatory stack of
`controller/service/manager/repository/factory` classes. Public operations and
value types are discoverable in a small declared public surface; SQL and mutable
runtime internals remain private. Repository functions live with the module
whose state they manage, not in a global `registry` bucket. Keep one migration
chain and one schema metadata assembly; physical table relocation is optional.

#### 1. Work: what work was accepted?

- **Owns:** project registration and setup, model profiles, task identity and
  lifecycle, immutable launch inputs, configuration reconciliation.
- **Public contract:** inspect/update a project or profile; resolve a launch;
  accept an exact preview through the application coordinator; read a task's
  accepted inputs; archive or purge through the coordinated lifecycle operation.
- **Inputs/outputs:** validated selections and expected edit/preview versions →
  immutable accepted inputs or a specific conflict/readiness refusal.
- **Must not:** start agent processes in a database function, reinterpret old
  inputs from today's settings, decide workflow routes, or mutate PR/run state
  through a generic task update API.
- **Current sources:** `launch.py`, `launchconfig.py`, `execution_inputs.py`,
  project setup/inspection, profile/project/task registries, launch routes.

Project checkout setup remains distinct from task isolation: creating/adopting a
registered base checkout is not the same operation as provisioning a disposable
clone. Native model-policy value types move out of profile persistence; shared
model-role vocabulary can retain its existing small, dependency-free owner.

#### 2. Isolation: where may untrusted work execute?

- **Owns:** task clone and Workshop lifecycle, sandbox execution mechanics,
  bounded workspace access, owned-root cleanup, Workshop additions, and the
  workspace writer guard.
- **Public contract:** prepare/inspect/destroy a workspace; execute a bounded
  sandbox command; provide process transport for a supervised agent; acquire a
  declared workspace hold; block/unblock admission and report its reason.
- **Inputs/outputs:** opaque owner identity, resolved workspace specification,
  explicit command/time limit → workspace reference, process/command result,
  or classified failure. No workflow definition or full `Task` object required.
- **Must not:** choose a model, parse omp RPC, route a workflow, sign or publish,
  access another task's workspace, or decide that retained evidence may be purged.
- **Current sources:** provisioning parts of `spawn.py`, `workshop.py`,
  `workshopadditions.py`, `delivery.WorkspaceGuard`, Workshop invocation paths.

Move the guard's **mechanism**, not delivery policy, here. Preserve host/agent
ownership distinctions, execution-context reentrancy, long-lived holds, cleanup
reservation, and restored blocked state. Delivery owns the durable explanation
for an unresolved effect; startup composition restores the corresponding guard
block before admitting writers. The guard coordinates daemon-managed writers;
it does not restrain an agent's native escape hatch or replace the OS sandbox.

Protected-path policy must not drift during this move. Artifacts/accepted inputs
identify handoff destinations; isolation can install literal Git exclusion
patterns for those supplied paths, while delivery still checks the actual tree
and retained history. Excludes are workspace mechanics, never proof that content
is safe to publish. Candidate identity and contamination checks stay in delivery.

The extraction criterion is useful without actually extracting a distribution:
isolation can be imported and exercised with an arbitrary owner ID and workspace
specification without FastAPI, workflow registries, or the omp protocol.

#### 3. Sessions: how is a named agent conversation controlled?

- **Owns:** session identity and applied policy records, child supervision,
  status transitions, native RPC, questions/approvals, steering, interruption,
  process replacement, and live session streams.
- **Public contract:** ensure/resume a named session on a supplied policy;
  perform a bounded turn; steer/answer/interrupt/stop; observe native events and
  structured lifecycle state.
- **Inputs/outputs:** session/workspace reference and exact policy → a usable
  session or a classified failure. Persist verified applied policy before a
  caller can start the dependent turn.
- **Must not:** infer the next workflow step, look up a mutable model profile to
  replace pinned intent, authorize publication, or interpret narrative text as
  a successful workflow result.
- **Current sources:** `agent.py`, `rpc.py`, `sessions.py`, session registry,
  `_ensure_session`, relevant recovery and agent routes.

Keep deep omp features. A narrow lifecycle contract is not a universal chat API:
the native adapter still exposes rich frames and optional native capabilities.
Do not implement another agent adapter just to justify the interface.

#### 4. Workflows: what is allowed to happen next, and why?

- **Owns:** format-aware definitions and canonical revisions, mutable library,
  pinned-definition readiness, runs/attempts, frozen evidence, outcomes,
  decisions, gates, bounded routing, retry semantics, and workflow authority.
- **Public contract:** validate/save/resolve a definition; start/recover a run;
  inspect a pending gate; commit an answer through the application coordinator;
  record an attempt result; resolve eligibility and refusal reasons.
- **Inputs/outputs:** pinned execution context, declared evidence and committed
  operation results → next attempt, gate, explicit pause, or named ending.
- **Must not:** construct Workshop/Git/forge argv, operate credentials, inspect
  another module's tables, own session policy persistence, or treat live event
  delivery as durable completion of a privileged effect.
- **Internal pieces:** `definitions/` (model, codec, validation, expressions,
  evidence), `library`, `engine`, `authority`, and run persistence. These are
  pieces of one module, not independent services or seven registries.

The engine remains the runner, not a newly invented framework. It calls narrow,
typed execution ports for agent, command, review, delivery, and capture steps.
Application wiring supplies adapters to the owning modules. Split the existing
methods by responsibility without changing retained format semantics or hashes.

#### 5. Delivery: what content may leave through Git and the forge?

- **Owns:** protected candidates, independent review records/reports, publication
  preview and authorization records, commit/push/PR action journals, Git/GPG/gh
  adapters, and operation-specific reconciliation/PR observation.
- **Public contract:** capture/review a candidate; prepare an exact delivery
  preview; record a workflow-bound grant within the shared transaction; execute
  one declared effect; inspect/reconcile that effect and its observed identity.
- **Inputs/outputs:** candidate, attempt, grant, destination, identity and
  expected versions → review evidence or durable effect result/refusal.
- **Must not:** grant itself workflow authority, silently extend an authorized
  ending, start hidden corrective agent turns, or publish from mutable files
  that merely share a task ID with the approved candidate.
- **Internal pieces:** candidates, review, publication, journal/reconciliation,
  and tool adapters. Review and publisher have separate public operations but
  remain together initially because protected candidate identity binds them.
- **Current sources:** candidate parts of `delivery.py`, `review.py`, `ship.py`,
  review/ship registries, `gpg.py`, `gh.py`, `prwatch.py`.

There is no generic `run_privileged_command` interface. Each effect retains its
specific authorization, correlation, cancellation, and reconciliation contract.
A future work-item broker can justify extracting a reusable forge boundary when
it has a real second consumer; that is not required here.

#### 6. Artifacts: which exact bytes remain useful after cleanup?

- **Owns:** immutable result manifests/bytes, capture and integrity checks,
  acceptance, consumer references, retention/purge, handoff materialization,
  and the distinct previewed checkout-export journal.
- **Public contract:** capture a declared selection; inspect/download/accept a
  revision; verify and attach that exact revision within launch acceptance;
  materialize pinned inputs; preview/export selected files; reconcile/purge.
- **Inputs/outputs:** bounded selection and producer/consumer identities → exact
  revision/manifest references, verified bytes, decisions or explicit refusals.
- **Must not:** choose a workflow route, grant publication by accepting a result,
  infer missing provenance, or read a producer's mutable workspace on behalf of
  a downstream task instead of its pinned retained result.
- **Internal pieces:** capture/store, handoff, checkout export. Export remains an
  explicit trusted writer into an operator checkout, never a general storage
  helper or an isolation operation.
- **Current sources:** `results.py`, `result_exports.py`, `handoff.py` and their
  registries. Retain one manifest/path classification contract across them.

Workflow evidence values and retained file bundles are different objects. Keep
the workflow outcome schema in workflows; do not make every outcome an artifact
BLOB. Existing result storage is not yet a full transcript/provenance archive.

#### 7. Oversight: what should the operator see and attend to?

- **Owns:** composed task/detail/dashboard read models, attention policy,
  advisories, notification decisions, and authoritative observation projections.
- **Public contract:** current snapshot/task detail and committed upserts;
  derived attention/refusal information; separately addressed session streams.
- **Inputs/outputs:** owner-supplied state/history and current session observations
  → disposable read models with stable identity and existing version semantics.
- **Must not:** write workflow/delivery state, authorize an action because a UI
  button is enabled, or start work as a side effect of building a snapshot.
- **Current sources:** `task_payload`, snapshot assembly in `api/ws.py`, manager
  payload builders, `notifications.py`, `advisories.py`, frontend attention rules.

Domain owners still determine their own permission/refusal and result facts.
Oversight composes those facts; it must not become a second authority evaluator.
It provides a read path across owners without exposing their mutable internals.

### Dependency rules and application composition

The dependency graph concerns imports and state access, not deployment:

1. `platform` and the tiny shared model vocabulary import no product modules.
   Put transaction mechanics here, not generic domain managers or all value types.
2. `isolation` depends only on these foundations and its external-tool boundary.
   `sessions` uses isolation's public execution contract.
3. `work`, `workflows`, `delivery`, and `artifacts` expose their own typed values,
   operations and reads. Work's launch snapshot may reference other owners'
   immutable contract values; no module imports another owner's persistence or
   concrete manager. Delivery/artifacts may use public isolation operations.
4. Workflow execution ports and delivery admission ports are **consumer-owned**.
   `application` implements the adapters, reading the relevant public durable
   state and using the same authority resolver for REST, runner, and direct
   service callers. This removes reciprocal concrete-manager imports without
   pretending authorization is a caller-supplied boolean or durable bearer token.
5. `application` may compose owners, but owners never import it. It contains
   named launch, gate-answer, effect-settlement, cleanup and recovery use cases,
   not copies of validation, routing, signing, capture or session logic.
6. `oversight` composes public reads. `api` depends on public application commands
   and read/stream contracts, never SQL tables, internal managers, or subprocesses.
   `app.py` builds fully wired objects before making them available to requests.

Use ordinary Python functions and dataclasses. Add a `Protocol` only for a real
consumer/provider seam or effect adapter. Do not create a DI container, universal
repository interface, service locator, plugin loader, or version every internal
method. Version persistent documents and external contracts when their semantics
require it, as today.

Extract already-shared bounded subprocess and hardened Git invocation mechanics
out of spawn/delivery where necessary; do not make host-side review call a
sandbox command runner or make artifact capture depend on the publisher just
to observe a Git revision. A technical runner handles argv, timeout, output and
cancellation, not command selection or authorization. External-tool adapters
retain those policies; arbitrary workflow commands remain sandbox-only.

### Data ownership does not mean separate transactions

One database remains a deliberate advantage. Each state family has one writer:
work owns identity and accepted intent; workflows own run/attempt state; sessions
own session identity/applied policy; delivery owns candidate/review/effect state;
artifacts own retained results/references/exports. Isolation owns workspace
resource state; oversight owns no authoritative execution state.

The existing `tasks` row physically mixes several of these facts. Initially
preserve its schema and assign **column-level write ownership**, with narrow
owner APIs and read views instead of passing the whole `Task` everywhere. Do not
add a task→many-runs schema migration under cover of moving files. One active run
per task and the current historical representation remain unchanged.

Expose a small, explicitly named set of connection-scoped operations for
cross-owner transactions. Only their application coordinators may compose them;
ordinary callers use committed public operations. Module repositories execute
only their own SQL even when sharing a SQLAlchemy connection. No nested commits,
awaited subprocesses, filesystem operations, or notification publication inside
the reserved write.

Preserve these atomic groups:

| Coordinator | Must commit or refuse together |
|---|---|
| Launch acceptance | Re-resolved configuration/library selection, preview comparison, accepted task inputs, and artifact consumer references |
| Gate answer | Exact persisted question/choice, required result acceptance/readability check or delivery grant, and next attempt/named ending |
| Delivery settlement | Observed action journal result and the workflow transition consuming that result |
| Explicit purge | Dependency/export guards, owned record deletion/tombstones, and reference release under existing retention rules |

Example: an approving delivery gate is **not** an event published to a shipping
subscriber. The coordinator checks the exact preview and pending gate, reserves
the write, commits workflow choice + delivery grant + successor together, then
wakes the runner. The delivery service revalidates durable authority at admission,
journals intent before the external effect, and settlement commits the observed
result with advancement. A restart adopts proven effects or requires explicit
continuation; it does not republish.

Filesystem and remote effects cannot join the SQLite transaction. Preserve the
existing intent-before-effect journals and operation-specific recovery. This is
not an exactly-once execution claim.

### Recovery and events

Recovery is part of every stateful owner's public contract. The application
startup coordinator orders those contracts; it does not learn how to repair each
owner's files or remote effects. Preserve startup barriers: reconcile relevant
journals and restore workspace blocks before admitting writers, restore session
identity/policy before re-driving its run, and report unrecoverable evidence
rather than guessing. Test the dependency order, not just individual `recover`
methods in isolation.

Keep the in-memory `EventHub` for observation. It is neither a durable history
store nor a business command bus. Session events may still wake waiters, but a
privileged effect's semantic completion must come from its durable owner record.
Publish committed changes after commit; a missed UI event is recovered through
the existing snapshot mechanism, not by replaying an action.

Preserve stream contracts during the refactor: raw agent frames stay separate,
result bytes stay off the dashboard stream, version-ordered upserts retain their
ordering rules, and connection sequence numbers are not global record versions.
Do not quietly claim all current event types have durable/global ordering.

### Frontend structure

Organize UI code by user capability while retaining a shared transport/cache:

```text
frontend/src/
  app/                         # routing, chrome, composition
  transport/                   # REST, daemon socket, agent channels, wire DTOs
  state/                       # reconnectable cache/reducer and selectors
  features/
    tasks/                     # fleet, task cockpit, launch
    sessions/                  # transcript, composer, questions, native frames
    workflows/                 # library, editor, flow, attempt rendering
    delivery/                  # candidate/review/ship UI
    artifacts/                 # result inspection, handoff, export
    projects/                  # project setup and configuration
    settings/                  # profiles and daemon preferences
  shared/                      # genuinely reusable visual primitives/formatters
```

Route-level task composition may import feature public components. Features must
not import each other's route implementations or internal hooks; neutral UI and
transport/state contracts are shared. Do not create separate sockets, competing
state stores, or independent copies of a task per feature. Partition the large
wire types/API files by contract ownership without changing their wire format.

Editor validation, attention aggregation, and button presentation may improve
usability, but cannot confer authority. Explicitly mark which frontend calculations
are presentation-only; server admission remains decisive. If centralized task
attention requires a new wire field, propose that semantic change separately
rather than hiding it in a directory move.

### How boundaries stay visible six months later

The existing daemon module map becomes the short contract index. For each owner
record: question answered, owned state/columns, public operations and value types,
allowed dependencies, forbidden effects, failure/cancellation/recovery behavior,
and the existing behavioral verification surface. Link detailed current contracts
and ADRs rather than reproducing them in package READMEs.

Add mechanical import-boundary checks as part of the first implementation change:
cover Python module-level, local and type-only imports, and frontend imports as
its feature boundaries move. Reject cross-module private imports, product imports
from platform/isolation, and transport imports into business code. Include a
negative fixture proving an illegal dependency is rejected; the check verifies
architecture policy rather than pinning arbitrary source wording.

During migration, keep an explicit finite list of existing legacy edges tied to
the change that removes them; no wildcard exemptions or new debt. Each moved
module has a clean cutover: migrate all callers/tests, remove old aliases and
re-exports, and shrink the list. It is empty at epic completion. Import checks
cannot prove correct SQL ownership or security; contract review and behavioral
checks remain necessary.

A proposed change should answer five questions in its SPEC:

1. Which owner changes, and which invariant remains unchanged?
2. Does any public contract, authority, or state ownership change?
3. What happens on stale input, cancellation, and restart?
4. Which transaction/journal makes the operation safe?
5. What observable scenario would fail if this change crossed the boundary?

### Deliberately unchanged and excluded

No workflow-language redesign, workflow revision rehashing, DB replacement,
wire-protocol redesign, universal event log, microservices, new agent backend,
automated refinement, roadmap/work-item engine, VM backend, network-policy
implementation, publishing identity change, or full provenance/transcript archive.
Do not collapse result acceptance, review verdict, and publication authorization.
Do not treat Python package boundaries as a credential/security sandbox: all
these daemon modules still share one trusted process.

The existing `epics/task-artifacts/EPIC.md` contains four finished entries and no
active child was found during this review. Preserve that delivered behavior;
this epic does not reopen its product decisions. Recheck active work before
selecting a child, because ownership may change after this proposal.

## Changes

Entries are independently deliverable refactors, not permission for one giant
rename. Every child maintains a runnable daemon, current external contracts,
existing migration history, and the boundary ratchet. Detailed symbol moves and
any necessary narrower decomposition belong to that child's proposal.

### [x] 1. expose-work-and-launch-boundaries

Establish `work`, named application launch acceptance, and thin work-related
routers. Move the shared reserved-write primitive and model values out of profile
persistence. Separate task records from wire projection without changing their
schema or payload. Publish the initial owner/column map and import-boundary
ratchet, and reconcile the current documentation discrepancies identified above.
This is a real vertical slice: launch can be exercised without an HTTP request.

- Depends on: None
- Acceptance: preview/acceptance use one application contract, stale previews
  produce no task or job, accepted configuration remains pinned, and routes do
  not own the launch transaction. All moved symbols have one canonical path.
- Verification: existing task/launch/profile/library/handoff REST and registry
  checks; exercise local browser launch and stale-preview refusal. Run the
  dependency checker against an intentionally forbidden fixture.

### [x] 2. isolate-workspace-lifecycle-and-execution

Move clone/Workshop lifecycle, command/process launching, additions, shared
workspace mechanics and the guard into `isolation`. Application spawn orchestration
asks it for a workspace and starts the workflow separately. Remove delivery's
private dependency on spawn helpers and workflow code's Workshop argv ownership.

- Depends on: expose-work-and-launch-boundaries
- Acceptance: isolation imports no workflow, delivery, work registry, FastAPI,
  or native omp protocol code; arbitrary owner IDs can exercise it. Guard
  semantics, interrupted-additions recovery, bounded execution and safe cleanup
  remain intact. No workflow starts from a low-level provisioning function.
- Verification: existing spawn/workshop/additions/recovery tests; exercise local
  launch, cancellation and `cleanup`, including cleanup/capture contention and
  refusal outside owned roots. Verify command exit/timeout behavior through the
  existing workflow scenarios, not only a mocked execution port.

### [~] 3. encapsulate-named-agent-sessions

Consolidate supervisor, RPC, status tracker, identity/policy persistence and the
native-session handoff into `sessions`. Move `_ensure_session` mechanics out of
the engine. Routes and workflow adapters use the same admitted session commands.
Keep the native transcript and capability surface intact.

- Depends on: isolate-workspace-lifecycle-and-execution
- Acceptance: changing native process policy requires no workflow interpreter
  changes; a failed policy handoff leaves no promptable process; replacement
  retains conversation identity and streaming reconnect behavior. Session code
  neither reads workflow tables nor determines a run's next step.
- Verification: existing agent/RPC/session/model-policy/recovery tests; browser
  `ask-approval`, steering/interrupt, policy replacement/reconnect and
  `crash-recovery` journeys against the real local harness boundary.

### [ ] 4. separate-workflow-semantics-and-effect-coordination

Move definitions/library/engine/authority and run persistence into `workflows`.
Replace concrete manager coupling with bounded execution/admission ports wired
by application adapters. Move gate/grant/result checks and effect settlement
into named shared-transaction coordinators, preserving each owner's SQL.
Do not redesign routing or rewrite retained definitions.

- Depends on: expose-work-and-launch-boundaries, encapsulate-named-agent-sessions
- Acceptance: the engine imports no concrete session/review/ship/result manager
  or tool launcher; definition parsing stays independent of persistence; gate
  answer and effect settlement retain their existing atomicity. Direct service
  and REST callers cannot bypass the same durable authority resolver.
- Verification: existing definition/library/workflow/ship regressions, especially
  duplicate/stale gate answers and interrupted-effect adoption. Local
  `workflow-decisions`, `ship-interrupted`, and `planning-handoff` journeys;
  inspect durable choice/grant/attempt records across a daemon restart.

### [ ] 5. encapsulate-trusted-review-and-publication

Move candidate capture, review, publication, journals and host Git/GPG/forge
adapters into `delivery`, with internal candidate/review/publisher boundaries.
Remove remaining task-wide/private imports and expose operation-specific recovery
and read contracts. Keep PR observation and current identity behavior unchanged.

- Depends on: separate-workflow-semantics-and-effect-coordination
- Acceptance: only delivery owns candidate/review/action writes and publication
  subprocesses; review, signed content, authorized prefix and reconciled effect
  identities remain bound. An unknown outcome blocks further workspace writers.
- Verification: existing review/ship/ship-registry/GPG/gh/PR-watch tests and local
  `review-comments`, `ship-endings`, `ship-retain`, `ship-interrupted`, and
  `merge-poll`. Exercise real local Git state and signed/pushed content through
  harness fakes, never personal forge identity or a production repository.

### [ ] 6. encapsulate-retained-artifacts-and-handoffs

Move result capture/storage, handoff and checkout export into `artifacts`, with
one public revision contract and private persistence. Application coordinators
own attachment/purge composition and result-gate participation. Keep export's
privileged writer and recovery boundary distinct from sandbox provisioning.

- Depends on: separate-workflow-semantics-and-effect-coordination
- Acceptance: artifacts do not import concrete workflow/delivery managers or
  mutate their state; captured bytes survive producer cleanup and remain exact
  in a downstream clone. Acceptance grants no delivery permission; protected
  handoff paths remain unpublishable in squash and retained history.
- Verification: existing result capture/registry/retention/export/handoff tests;
  local browser `durable-results` and `planning-handoff`, explicit export to an
  isolated checkout, stale/conflicting selection and interrupted-export checks.

### [ ] 7. centralize-oversight-and-startup-composition

Finish moving task/detail/snapshot composition out of registries and transport
into `oversight`. Expose owner-supplied reads and refusal facts, centralize server
attention policy, and finish application startup/recovery/shutdown composition
using the public owner contracts. Remove the remaining legacy boundary edges.

- Depends on: encapsulate-trusted-review-and-publication, encapsulate-retained-artifacts-and-handoffs
- Acceptance: routes/sockets depend on public command/query/stream contracts;
  registry code never builds a cross-domain UI projection; startup restores
  writer blocks before work resumes. Domain code cannot reach global app state.
  No new ordering or attention semantics are introduced implicitly.
- Verification: existing WS/event/notification/advisory/recovery checks; browser
  reconnect while work changes, multiple active tasks, `advisories-stalls`,
  `ws-watch`, and `crash-recovery`. Confirm snapshot/delta and raw-session
  isolation, and an empty daemon legacy-boundary exception list.

### [ ] 8. organize-frontend-by-user-capability

Move frontend feature components, hooks and presentation rules to the feature
map above. Keep route composition, wire contracts, shared daemon cache and native
streams explicit. This can proceed independently because backend wire contracts
remain unchanged throughout the epic; do not wait for every backend move.

- Depends on: None
- Acceptance: route files compose feature public surfaces instead of owning
  transcript/editor/delivery/result internals; features have no private
  cross-feature imports or competing daemon caches. Browser-local drafts and
  editor behavior remain local; execution and authority remain server-owned.
- Verification: existing affected frontend checks where contracts break, plus
  actual browser launch → cockpit → question/gate → review/ship and planning →
  retained result → downstream launch journeys; exercise editor drafts and
  reconnect. Dependency checks reject a forbidden feature-internal import.

## Completion

A maintainer can trace launch → workspace → session → workflow → review/result →
delivery/export through public contracts and the named cross-module coordinators,
without consulting unrelated internals. Each owner has an explicit state/write
map, dependency rule, recovery contract, and behavioral verification surface in
the maintained documentation. No legacy forwarding modules or boundary exemptions
remain; no generic application object is available inside business modules.

The daemon still runs as one process with the same SQLite history and deployed
workflow semantics. Demonstrate both publication and no-publication planning
journeys, stale approvals, interrupted-effect recovery, simultaneous tasks,
browser reconnect, cleanup, retained handoff and explicit export. Run the local
E2E harness with browser evidence for affected surfaces, and the repository's
build/lint/typecheck/test commands at integration. Those are future implementation
acceptance requirements, not checks claimed by this proposal.

Update the existing architecture overview, daemon module map, repository layout,
API/WS, lifecycle/recovery references, and affected operator documentation as the
code moves. Record the accepted modular-monolith and shared-transaction rationale
in an ADR during implementation. Reconcile only delivered vision/ADR scope;
network policy, bot identity, transcript retention, full lineage and refinement
remain separate product decisions.

Original epic-proposal evidence: repository/document inspection and an executed
AST dependency analysis of all 57 daemon Python modules. No application tests,
browser journeys, runtime security audit, or proof of total architectural
correctness is claimed for that read-only review.

The first work/launch boundary is finished and documented in the
[daemon module map](../../docs/develop/reference/daemon-modules.md) and
[ADR-0038](../../docs/adr/0038-own-modules-and-compose-local-transactions.md).
The second child, `isolate-workspace-lifecycle-and-execution`, is finished
too: the resource boundary is documented in the same
[module map](../../docs/develop/reference/daemon-modules.md) and
[ADR-0039](../../docs/adr/0039-own-workspace-resources-behind-isolation.md).
The third child, `encapsulate-named-agent-sessions`, has an active
[specification](changes/encapsulate-named-agent-sessions/SPEC.md) and
[implementation plan](changes/encapsulate-named-agent-sessions/PLAN.md).
Implementation has not started.
