# Daemon module map

All paths are under `daemon/src/ompire_daemon/`.

## Ownership at a glance

The daemon is one process with explicit module ownership rather than strict
layer isolation. Four packages draw the first hard boundary, and
`daemon/tests/test_architecture.py` enforces it on every pytest run (see
[Build, test, and run](../how-to/build-test-run.md#architecture-dependency-checks)):

| Package | Owns | May not import |
|---|---|---|
| `platform/` | The shared SQLite write reservation | Any product code |
| `work/` | Projects, profiles, tasks, launch resolution, accepted inputs, and their reconciliation — the records and rules of accepted work | Transport, application commands, task projections |
| `application/` | Transport-independent commands: `LaunchService` (preview/accept), work configuration, task reads and explicit Continue | Transport of any kind |
| `oversight/` | The one wire shape of a task row and of a reviewed launch resolution | — |

`model_config.py` carries the same guarantee in file form: the shared
model-value vocabulary (thinking levels, the four roles, `RoleBinding` and
its validation) imports no product code at all.

Everything else is still flat modules at their historical paths, listed below
under the owner a later change will formalize. The map is honest about what
is extracted and what is not: `work` does not contain isolation, sessions,
workflow semantics, delivery, or artifacts yet, and their existing edges into
work-owned modules are named as migration exceptions in the checker, each
assigned to the change that will remove it.

## Entry and wiring

| Module | Responsibility |
|---|---|
| `__main__.py` | `ompire-daemon` entry point. Loads config, builds the app, runs uvicorn. |
| `app.py` | Application construction and lifespan. Owns the shared state every route depends on, and constructs the `LaunchService` and `SpawnScheduler` with explicit collaborators. |
| `config.py` | `config.toml` loading, validation, and defaults. Fails startup on an unknown or invalid key. |
| `auth.py` | Bearer token generation, the REST dependency, and the WebSocket check. |
| `static.py` | Serving the built frontend, including SPA deep-link fallback. |
| `platform/transactions.py` | `reserved_write`: the one SQLite write reservation (`BEGIN IMMEDIATE`, commit or roll back) that every check-then-write mutation in the daemon admits through. Platform code — the standard library and SQLAlchemy only. |

## API surface

| Module | Responsibility |
|---|---|
| `api/rest.py` | The authenticated `/api` router: composition for the routes not yet extracted (workflow library and gates, review, delivery, results and exports, agent control, settings, cleanup and purge), and the single place the work routers are included behind the common bearer boundary. |
| `api/projects.py`, `api/profiles.py`, `api/tasks.py` | Thin work routers: wire conversion, one application command or public work query per route, and error mapping. No storage, no managers, no business rules. |
| `api/work_models.py` | The shared work wire schemas (projects, profiles, task creation/continuation). Class names are the OpenAPI component names. |
| `api/deps.py` | The FastAPI dependency accessors every router reads its collaborators through. |
| `api/errors.py` | Shared domain-error → HTTP mapping for the work routers. |
| `api/ws.py` | `/api/ws`: snapshot then deltas. Accepts no commands. |
| `events.py` | The in-process event hub everything publishes to. Publishing is safe from any thread; fan-out always runs on the daemon's event loop. |

## The work and launch boundary

`application/` is the transport-independent command layer. `LaunchService`
owns preview and acceptance end to end: the attachment Git observation
outside every lock, mention validation against the accepted base, the
reserved re-resolution and token comparison, the task-plus-references
transaction, and — only after that commit — producer projection refresh, the
`task_created` event, and preparation scheduling through the injected
`SpawnScheduler`. HTTP routes convert a wire body into a typed
`LaunchRequest` and map the service's domain errors to status codes; the
acceptance algorithm itself never sees a request.

| Module | Responsibility |
|---|---|
| `application/launch.py` | `LaunchService.preview/accept` and `SpawnScheduler` (strong job references, completion removal, lifespan cancellation — shared by spawn pipelines and delivery jobs). |
| `application/work.py` | Project registration/update/removal with checkout admission, setup retry, profile commands, and reconciliation confirmation — each with its committed-change event. |
| `application/tasks.py` | Task list/detail composition (workshop status, attempt history) and the guarded explicit Continue. |
| `work/launch.py` | Launch resolution: one set of rules shared by preview, acceptance, and legacy confirmation. Pure with respect to the world outside the database, so it can run inside a write reservation. |
| `work/inputs.py` | The typed launch decision pinned to a task — including its workflow revision — its JSON codec, and `ModelPolicy`, the complete native role map one omp process runs under. |
| `work/projects.py` | Project records: CRUD against the `projects` table, the guarded default-model-profile reference, setup-state transitions, and the launch-reconciliation state. |
| `work/profiles.py` | Model-profile records: names, CRUD, and reference-guarded deletion. The role *values* live in `model_config.py`. |
| `work/tasks.py` | Task records: the pinned-input guards, lifecycle classification, atomic purge (with its cross-owner result/delivery guards, held intact until the artifacts extraction), and startup reconciliation. |
| `work/launch_evidence.py` | Inert upgrade evidence and the operator decisions that close out a reconciliation. Nothing here is read to execute anything. |
| `work/reconciliation.py` | Startup initialization for the template retirement, plus the project and legacy-task reconciliation flows — including the workflow continuation candidate, its compatibility check, and the history boundary a confirmation records. |
| `work/checkout.py` | Read-only inspection of a base checkout, plus the URL and remote-name validators that guard `git clone` argv. Never writes to a checkout. |
| `work/setup.py` | `ProjectSetupManager`: the supervised clone-mode setup job, its step events, retry, and startup reconciliation of interrupted clones. |
| `work/files.py` | Project file search, and the `@file` mention rule: validated at submit against the checkout, resolved again against the clone before delivery. |
| `oversight/tasks.py` | `task_payload` and `resolution_payload`: the one wire shape REST responses, `task_*` events, and the WS snapshot all serialize through. Resolves the pinned definition to describe it; storage never depends on it. |

Allowed dependencies, enforced: `api` work routers → `application` commands →
`work` operations → `platform`/`model_config` values, with `oversight`
reading `work` records and the pinned-definition resolver. `work` modules
may still call today's handoff, workflow-library, and result APIs — those
owners have not moved yet — but never transport, commands, or projections.

## Persistence

| Module | Responsibility |
|---|---|
| `db.py` | Engine, schema definition, WAL configuration. Note it does *not* enable `PRAGMA foreign_keys` — see [Database schema](database-schema.md#reference-safety-without-global-fk-enforcement). |
| `model_config.py` | The vocabularies every model consumer agrees on: thinking levels, the four abstract roles, and the pure `RoleBinding` value with its validation. No persistence, no SQLAlchemy. |
| `workflow_definitions.py` | The workflow document in both formats: immutable data model, strict format-aware YAML loader, canonicalization and content identity, verified YAML emission, the bounded three-valued evaluator, result contracts, and evidence selection. Imports nothing from the registry or the task model. See [Workflow definitions](workflow-definitions.md). |
| `migrate.py` | Runs Alembic migrations at startup. |
| `registry/sessions.py` | Session identity, `(task_id, name)` |
| `registry/workflows.py` | Workflow runs, step records, and the atomic waiting, retry, and gate-decision transitions |
| `registry/workflow_definitions.py` | Retained revisions: append-only, verified on read, cached by content identity and never by name |
| `registry/workflow_library.py` | The operator-owned library above those revisions: entries, inert drafts, current-revision selection, archive/restore, edit versions, and the single transactional prospective lookup. See [ADR-0031](../../adr/0031-let-operators-own-a-workflow-library-above-retained-revisions.md) |
| `registry/reviews.py` | Review status and ordered iteration history, each bound to the candidate it graded |
| `registry/ships.py` | Delivery candidates, operator authorizations, write-ahead action attempts, and reconciliation decisions |
| `registry/results.py` | Durable task results: the manifest contract and its identities, the purely syntactic selection rules, the reserved-write capture/accept/purge mutations, the connection-scoped attachment checks and consumer reference index, and metadata projections that never load a BLOB. See [ADR-0034](../../adr/0034-retain-durable-task-results-outside-the-workspace.md) and [ADR-0035](../../adr/0035-refuse-to-publish-handoff-destinations.md) |
| `registry/result_exports.py` | The checkout-export journal: approved preview documents, per-destination intent and outcome, the durable active-root reservation, and the purge and checkout-repointing guards. See [ADR-0036](../../adr/0036-install-exported-result-files-without-replacing-them.md) |
| `registry/settings.py` | Layered settings: override, then TOML, then default |

## Task lifecycle

| Module | Responsibility |
|---|---|
| `spawn.py` | The spawn pipeline: fetch, clone, branch, `inputs` for a launch with handoff inputs, workshop. Resolves nothing — every value comes off the task's accepted inputs. |
| `handoff.py` | Handoff inputs: the destination and bounds rules an attached result revision must satisfy, the read-only Git observation a launch is reviewed against, and the no-follow exclusive materialization that installs reviewed bytes into a recipient's clone. Shared by launch resolution and the spawn pipeline so the two cannot disagree. See [ADR-0035](../../adr/0035-refuse-to-publish-handoff-destinations.md) |
| `workshopadditions.py` | The bounded staging that makes the accepted Workshop additions source the one the launcher actually applies, with restoration and crash recovery. |
| `workshop.py` | Container existence checks and teardown. Status is derived on demand, never persisted. |
| `agent.py` | Agent child process lifecycle and event fan-out. |
| `rpc.py` | Stdio NDJSON transport. Correlates requests by ID while push events interleave. |
| `sessions.py` | The per-session status state machine. Every transition goes through one guarded method. |
| `workflows.py` | The packaged built-ins and the engine that carries a definition out: step execution, routing, uncertainty pauses, gates, and restart recovery. What a *name* currently means is not here — it lives in the library. |
| `taskdefinition.py` | The one resolver from a task to *its* pinned definition, and the classified readiness a task reports when that cannot be resolved. Every runtime consumer goes through here. |
| `recovery.py` | Startup recovery for sessions and interrupted operations. |

## Review and publishing

| Module | Responsibility |
|---|---|
| `delivery.py` | The protected candidate — capture, identity, its owner-private object store, and the isolated review view — plus the task-workspace ownership guard every daemon-managed writer is admitted through. |
| `review.py` | Host-side review: candidate capture, the llmvet subprocess over an isolated view, and startup interruption handling. The record lives in `registry/reviews.py`. |
| `ship.py` | Draft, plus three independently admitted trusted operations — signed commit, push, pull request — a coordinator that runs only the authorized prefix, and operation-specific reconciliation. The journal lives in `registry/ships.py`. |
| `gpg.py` | Signing-key enumeration, selection (override → config → git → auto), and non-prompting agent classification: `ready`, `locked`, `ambiguous`, `no_key`, `missing`, `agent_unavailable`, `error`, `unknown`. |
| `gh.py` | The only daemon-owned GitHub CLI boundary: configured executable discovery, non-interactive bounded execution, credential redaction, ambient identity probe, canonical upstream eligibility checks, and in-memory `gh_status` projection. |
| `prwatch.py` | Polls pull requests to a terminal state. |
| `result_exports.py` | `ResultExportManager`: the only boundary in the daemon that writes into a directory the operator owns. Create-only classification against the real checkout, a canonical approval document the daemon recomputes, staged `renameat2(RENAME_NOREPLACE)` installation, and read-only recovery that classifies an interrupted export without replaying or rolling back any effect. Distinct from `work/checkout.py`, which only inspects a checkout. See [ADR-0036](../../adr/0036-install-exported-result-files-without-replacing-them.md) |
| `results.py` | `ResultManager`: the trusted capture boundary (descriptor-relative no-follow traversal, bounded reads, source-mutation checks, encoding and credential refusals), honest provenance, integrity-checked reads, comparison, ZIP assembly, and interrupted-capture recovery. Admits through the same workspace guard as review and delivery, and grants no publication or workflow effect. The record lives in `registry/results.py`. |

## Attention

| Module | Responsibility |
|---|---|
| `notifications.py` | The status-to-tier mapping and desktop notification delivery. |
| `advisories.py` | Threshold observations that ride alongside a session without changing it. |

## Reading order

To follow one launch: `api/tasks.py` → `application/launch.py` →
`work/launch.py` → the acceptance transaction in `platform/transactions.py` →
`spawn.py`.

To follow one task end to end: `spawn.py` → `agent.py` → `rpc.py` →
`sessions.py` → `workflows.py` → `delivery.py` → `review.py` → `ship.py`.

For a task that ends in a retained result rather than a publication, the path is
`spawn.py` → `agent.py` → `results.py`, and then either `handoff.py` (into
another task's clone) or `result_exports.py` (into the operator's checkout).
Nothing in `review.py` or `ship.py` is involved on any of those paths.

To understand how clients see any of it: `oversight/tasks.py` → `events.py`
→ `api/ws.py`.
