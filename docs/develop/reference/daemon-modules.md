# Daemon module map

All paths are under `daemon/src/ompire_daemon/`.

## Entry and wiring

| Module | Responsibility |
|---|---|
| `__main__.py` | `ompire-daemon` entry point. Loads config, builds the app, runs uvicorn. |
| `app.py` | Application construction and lifespan. Owns the shared state every route depends on. |
| `config.py` | `config.toml` loading, validation, and defaults. Fails startup on an unknown or invalid key. |
| `auth.py` | Bearer token generation, the REST dependency, and the WebSocket check. |
| `static.py` | Serving the built frontend, including SPA deep-link fallback. |

## API surface

| Module | Responsibility |
|---|---|
| `api/rest.py` | Every state-changing operation. The largest module, and deliberately so — commands are REST. |
| `api/ws.py` | `/api/ws`: snapshot then deltas. Accepts no commands. |
| `events.py` | The in-process event hub everything publishes to. Publishing is safe from any thread; fan-out always runs on the daemon's event loop. |

## Persistence

| Module | Responsibility |
|---|---|
| `db.py` | Engine, schema definition, WAL configuration. Note it does *not* enable `PRAGMA foreign_keys` — see [Database schema](database-schema.md#reference-safety-without-global-fk-enforcement). |
| `model_config.py` | The vocabularies every model consumer agrees on: thinking levels and the four abstract roles. Model identifiers are not validated here — the provider-qualified grammar lives with profile value validation. |
| `execution_inputs.py` | The typed launch decision pinned to a task — including its workflow revision — its JSON codec, and `ModelPolicy`, the complete native role map one omp process runs under. |
| `workflow_definitions.py` | The workflow document in both formats: immutable data model, strict format-aware YAML loader, canonicalization and content identity, verified YAML emission, the bounded three-valued evaluator, result contracts, and evidence selection. Imports nothing from the registry or the task model. See [Workflow definitions](workflow-definitions.md). |
| `migrate.py` | Runs Alembic migrations at startup. |
| `registry/projects.py` | Projects, including the guarded default-model-profile reference |
| `registry/model_profiles.py` | Model profiles: the four-role contract, provider-qualified identifier grammar, reference-guarded deletion, and the `reserved_write` SQLite write reservation both reference checks share |
| `registry/tasks.py` | Tasks and their publishing state |
| `registry/sessions.py` | Session identity, `(task_id, name)` |
| `registry/workflows.py` | Workflow runs, step records, and the atomic waiting, retry, and gate-decision transitions |
| `registry/workflow_definitions.py` | Retained revisions: append-only, verified on read, cached by content identity and never by name |
| `registry/workflow_library.py` | The operator-owned library above those revisions: entries, inert drafts, current-revision selection, archive/restore, edit versions, and the single transactional prospective lookup. See [ADR-0031](../../adr/0031-let-operators-own-a-workflow-library-above-retained-revisions.md) |
| `registry/reviews.py` | Review status and ordered iteration history, each bound to the candidate it graded |
| `registry/ships.py` | Delivery candidates, operator authorizations, write-ahead action attempts, and reconciliation decisions |
| `registry/results.py` | Durable task results: the manifest contract and its identities, the purely syntactic selection rules, the reserved-write capture/accept/purge mutations, the connection-scoped attachment checks and consumer reference index, and metadata projections that never load a BLOB. See [ADR-0034](../../adr/0034-retain-durable-task-results-outside-the-workspace.md) and [ADR-0035](../../adr/0035-refuse-to-publish-handoff-destinations.md) |
| `registry/settings.py` | Layered settings: override, then TOML, then default |
| `registry/launch.py` | Inert upgrade evidence and the operator decisions that close out a reconciliation. Nothing here is read to execute anything. |

## Task lifecycle

| Module | Responsibility |
|---|---|
| `launch.py` | Launch resolution: one set of rules shared by preview, acceptance, and legacy confirmation. Pure with respect to the world outside the database, so it can run inside a write reservation. |
| `launchconfig.py` | Startup initialization for the template retirement, plus the project and legacy-task reconciliation flows — including the workflow continuation candidate, its compatibility check, and the history boundary a confirmation records. |
| `spawn.py` | The spawn pipeline: fetch, clone, branch, `inputs` for a launch with handoff inputs, workshop. Resolves nothing — every value comes off the task's accepted inputs. |
| `handoff.py` | Handoff inputs: the destination and bounds rules an attached result revision must satisfy, the read-only Git observation a launch is reviewed against, and the no-follow exclusive materialization that installs reviewed bytes into a recipient's clone. Shared by launch resolution and the spawn pipeline so the two cannot disagree. See [ADR-0035](../../adr/0035-refuse-to-publish-handoff-destinations.md) |
| `workshopadditions.py` | The bounded staging that makes the accepted Workshop additions source the one the launcher actually applies, with restoration and crash recovery. |
| `projectcheckout.py` | Read-only inspection of a base checkout, plus the URL and remote-name validators that guard `git clone` argv. Never writes to a checkout. |
| `projectsetup.py` | `ProjectSetupManager`: the supervised clone-mode setup job, its step events, retry, and startup reconciliation of interrupted clones. |
| `projectfiles.py` | Project file search, and the `@file` mention rule: validated at submit against the checkout, resolved again against the clone before delivery. |
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
| `results.py` | `ResultManager`: the trusted capture boundary (descriptor-relative no-follow traversal, bounded reads, source-mutation checks, encoding and credential refusals), honest provenance, integrity-checked reads, comparison, ZIP assembly, and interrupted-capture recovery. Admits through the same workspace guard as review and delivery, and grants no publication or workflow effect. The record lives in `registry/results.py`. |

## Attention

| Module | Responsibility |
|---|---|
| `notifications.py` | The status-to-tier mapping and desktop notification delivery. |
| `advisories.py` | Threshold observations that ride alongside a session without changing it. |

## Reading order

To follow one task end to end: `spawn.py` → `agent.py` → `rpc.py` →
`sessions.py` → `workflows.py` → `delivery.py` → `review.py` → `ship.py`.

For a task that ends in a retained result rather than a publication, the path is
`spawn.py` → `agent.py` → `results.py`, and stops there: nothing in `review.py`
or `ship.py` is involved.

To understand how clients see any of it: `events.py` → `api/ws.py`.
