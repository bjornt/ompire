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
| `events.py` | The in-process event hub everything publishes to. |

## Persistence

| Module | Responsibility |
|---|---|
| `db.py` | Engine, schema definition, WAL configuration. |
| `migrate.py` | Runs Alembic migrations at startup. |
| `registry/projects.py` | Projects |
| `registry/templates.py` | Templates |
| `registry/tasks.py` | Tasks and their publishing state |
| `registry/sessions.py` | Session identity, `(task_id, name)` |
| `registry/workflows.py` | Workflow runs and step records |
| `registry/settings.py` | Layered settings: override, then TOML, then default |

## Task lifecycle

| Module | Responsibility |
|---|---|
| `spawn.py` | The four-step spawn pipeline: fetch, clone, branch, workshop. |
| `projectfiles.py` | Project file search, and the `@file` mention rule: validated at submit against the checkout, resolved again against the clone before delivery. |
| `workshop.py` | Container existence checks and teardown. Status is derived on demand, never persisted. |
| `agent.py` | Agent child process lifecycle and event fan-out. |
| `rpc.py` | Stdio NDJSON transport. Correlates requests by ID while push events interleave. |
| `sessions.py` | The per-session status state machine. Every transition goes through one guarded method. |
| `workflows.py` | Workflow definitions and the engine that drives them. |
| `recovery.py` | Startup recovery for sessions and interrupted operations. |

## Review and publishing

| Module | Responsibility |
|---|---|
| `review.py` | Host-side review. Owns the `refs/ompire/review-orig` reset dance. |
| `ship.py` | Draft, signed commit, push, PR. Owns `refs/ompire/ship-orig`. |
| `gpg.py` | Signing-key state probe: `cached`, `locked`, `unknown`. |
| `prwatch.py` | Polls pull requests to a terminal state. |

## Attention

| Module | Responsibility |
|---|---|
| `notifications.py` | The status-to-tier mapping and desktop notification delivery. |
| `advisories.py` | Threshold observations that ride alongside a session without changing it. |

## Reading order

To follow one task end to end: `spawn.py` → `agent.py` → `rpc.py` →
`sessions.py` → `workflows.py` → `review.py` → `ship.py`.

To understand how clients see any of it: `events.py` → `api/ws.py`.
