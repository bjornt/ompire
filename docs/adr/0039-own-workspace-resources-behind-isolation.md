# ADR 0039: Own workspace resources behind isolation

- Status: Accepted
- Date: 2026-09-11

## Context

Every task runs inside a disposable workspace — a confined clone of the
project checkout plus a Workshop container — and several daemon operations
must not run over each other's workspace. Before this decision, the code that
provided those resources lived wherever it had grown: the spawn pipeline in
`spawn.py` (which also recorded task state, published events, verified
handoffs, resolved the pinned definition, and started the workflow), the
Workshop CLI adapter and additions staging beside it, the writer guard inside
`delivery.py` next to the candidate policy it has nothing to do with, and the
hardened Git invocation helpers under delivery as well. Consequences a
maintainer paid for that layout:

- Changing sandbox execution or session-adjacent mechanics could require
  editing the workflow engine (`workflows.py` built `workshop exec` argv and
  supervised its process group) or the agent supervisor (`agent.py` built the
  same prefix again for native starts and the `ask.timeout` probe).
- Delivery carried a lazy private import of `spawn._ensure_git_excludes`, and
  review imported `spawn.Step` and `spawn._run_step`, so the "workspace"
  code was really a shared utility nobody owned.
- The resource code could not be exercised without a task row, a workflow
  revision, an event hub, and (transitively) the workflow engine — the spawn
  pipeline resolved the task's pinned definition and started a run.

This is the second child of the modular-monolith epic
([ADR-0038](0038-own-modules-and-compose-local-transactions.md)); the first
established `work` and the application command layer.

## Decision

One owner — the `isolation` package — owns the disposable workspace as a
**resource**, and takes resolved values, not product state:

- A frozen `WorkspaceSpec` carries an opaque integer owner id, the clone path
  and owned task root, the accepted checkout/fetch remote/base/branch, an
  optional pinned source commit, the additions selection, supplied protected
  destinations, the launcher argv, and the Git/Workshop deadlines. Nothing is
  re-read from a project, profile, or `Config` during preparation.
- Staged operations replace the provisioning function:
  `prepare_clone` (confinement, destination refusal, fetch, hardlink clone,
  exclusions, branch), `verify_pinned_source`, `launch_workshop` (additions
  staged around the launcher, container identity returned — not recorded),
  and `destroy_workspace` (independent root confinement, container-first
  teardown, idempotent clone removal).
- The same package owns the Workshop status/removal adapter, finite sandbox
  command execution (`workshop exec` argv construction, process-group
  teardown on timeout/cancellation, nonzero-exit-as-data), piped
  long-lived-process transport for native agents, one literal Git-exclusion
  mechanism, and the workspace writer guard with its busy/blocked errors.
- Technical invocation helpers that were already shared move to neutral
  ground: the checked host-subprocess step runner to
  `platform/processes.py`, the hardened Git argv/execution helpers to
  `platform/git.py`. Both are platform code — standard library only — with
  neutral failure types (`ProcessStepError`, `GitCommandError`) that calling
  owners classify.

Application orchestration decides *when accepted work uses those resources*:
`application/spawn.py` projects the task's accepted inputs onto a
`WorkspaceSpec`, translates live resource progress into task events,
coordinates retained-attachment installation between clone and container,
records lifecycle observations through the work owner, and starts the pinned
workflow only after every required workspace operation succeeds.
`application/cleanup.py` composes the existing admission rules (active
delivery, run-position refusal, the shared cleanup hold) around the guarded
resource teardown and owner finalization. `application/execution.py` adapts
the workflow engine's consumer-owned command contract to the isolation
operation; the engine declares `CommandExecutor`/`CommandOutcome`/
`CommandExecutionError` and constructs no container argv itself.
`agent.py` builds the *native* argv (without the workshop prefix) and adopts
the process the resource boundary started; protocol, readiness, policy
verification, and graceful shutdown stay agent-owned.

The boundary is mechanically enforced. `daemon/tests/test_architecture.py`
classifies `isolation` as an owner, keeps every `platform` module pure, and
rejects: any product import from isolation (work registry, workflow,
delivery, artifacts, HTTP transport, native RPC), cross-owner imports of
isolation submodules or symbols outside the declared public surface, and
imports of the moved symbols from their retired locations. `spawn.py`,
`workshop.py`, and `workshopadditions.py` are gone, and the two paid-off
checker exceptions (delivery's and review's private spawn imports) are
deleted with them.

### Rejected alternatives

- **A task-aware provisioning facade** (keep one `provision_task(task_id)`
  function, move it): preserves the coupling this decision removes — the
  resource code would still need task rows, inputs decoding, events, and the
  workflow start, and could not be exercised without them.
- **A second, delivery-side guard**: two exclusion mechanisms drift; exactly
  one guard instance with host/agent ownership kinds, execution-context
  reentrancy, and owner-derived blocks is the property that makes
  review/delivery/agent/capture/cleanup contention sound.
- **Independently deployed services or a resource daemon**: ADR-0038 already
  fixed the modular monolith; the workspace owner is an in-process module
  like every other owner.

## Consequences

- Isolation can be imported and exercised with an arbitrary integer owner and
  no database, FastAPI app, or omp protocol loaded — that testability is the
  boundary's acceptance proof, not an extractable distribution.
- Preparation cannot start or select a workflow, and no low-level resource
  function writes product execution state; the coordinator records outcomes
  and starts the run. A failure in preparation is one classified
  `WorkspaceOperationError` naming its phase, which the coordinator maps to
  the same visible failed-task behavior the pipeline always had.
- Delivery keeps candidate identity, protected-path policy, and clone-safety
  refusal, and still decides why a workspace is blocked and when a block may
  clear; the guard retains only the mechanical block and reason. Technical
  Git failures surface as `GitCommandError` from the platform helper, which
  delivery/review/ship catch alongside their content refusals so a broken
  Git invocation stays a classified refusal rather than an uncaught 500.
- Exclusion installation remains a convenience: candidate-tree and
  retained-history checks in delivery are the publication guarantee, and the
  protected set still comes from the task's accepted attachments.
- What this does *not* change, stated plainly: the hardlink clone behavior
  and shared object storage of ADR-0006; the OS sandbox's strength,
  credential boundary, or any network policy (an agent's native escape hatch
  and egress are untouched — the guard coordinates daemon-managed writers
  only, and inherited execution-context admission is convenience, not a
  security claim); additions restoration stays best-effort; and no stronger
  best-effort-restoration or automatic-retry guarantee is introduced.

## Alternatives considered

In addition to the rejected alternatives above: extracting a reusable
"forge/tool boundary" from the shared Git helpers was considered and
deferred — the helpers moved to `platform` precisely because they are
technical and owner-neutral, and a second real consumer (the future
work-item broker) is the bar ADR-0038 set for extracting anything more.
