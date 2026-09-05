# HTTP and WebSocket API

The daemon serves a REST API for commands and a WebSocket for observation.
This split is deliberate: state changes go through REST, and the WebSocket
accepts no commands at all.

Base URL: `http://127.0.0.1:4173`.

## Authentication

Every request needs the bearer token from `data_dir/token`:

```sh
curl -H "Authorization: Bearer $(cat ~/.local/share/ompire/token)" ...
```

The WebSocket authenticates with the same token. Rotating it
(`POST /api/settings/token/rotate`) closes every open WebSocket with code
`1008`.

## Interactive reference

The daemon serves generated OpenAPI documentation at `/docs`, and the schema
itself at `/openapi.json`. That is authoritative for request and response
bodies; the tables below are a map, not a schema.

## Projects

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/projects` | List |
| `POST` | `/api/projects` | Create. `201`, or `409` on duplicate name. `422` when an adopted checkout is unusable or a URL is not an accepted form |
| `POST` | `/api/projects/checkout-inspect` | Look at an unregistered path read-only; returns its remotes and, on refusal, why |
| `GET` | `/api/projects/{name}` | Fetch |
| `PUT` | `/api/projects/{name}` | Update. `409` while setup is running or when repointing a cloned checkout. `422` for an unknown `default_model_profile` |
| `DELETE` | `/api/projects/{name}` | Delete. `409` if any task references it, or while setup is running |
| `POST` | `/api/projects/{name}/setup/retry` | Re-arm a failed clone. `202`, or `409` for an adopted project |
| `GET` | `/api/projects/{name}/files` | Repository paths for prompt `@` mentions. `409` if the checkout is missing or not a git repository |
| `GET` | `/api/projects/{name}/launch-reconciliation` | Configuration carried over from templates that still needs a decision, with every candidate and its source |
| `POST` | `/api/projects/{name}/launch-reconciliation` | Record the decision. `409` for a stale evidence fingerprint, `422` for a missing acknowledgement |

Create accepts `checkout_mode` (`adopt`, the default, or `clone`) and
`fetch_remote`. Clone mode derives its destination and refuses a supplied
`checkout_path`. See [Projects](projects.md#checkout-modes).

Both create and update accept an optional `default_model_profile` naming a
[model profile](model-profiles.md). On update the field is three-valued:
omitted preserves the stored reference, `null` clears it, a name selects that
profile. See [Default model
profile](projects.md#default-model-profile).

Both also accept `base_branch`, `branch_pattern`, `workshop_additions`, and
`preamble`. On update these follow the same omission rule — a body that leaves
one out preserves it — but are never null: an empty `preamble` is a value, and
an explicit null for any of the others is `422`. See [Workspace and prompt
defaults](projects.md#workspace-and-prompt-defaults).

## Model profiles

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/model-profiles` | List, sorted by name |
| `POST` | `/api/model-profiles` | Create. `201`, `409` on duplicate name, `422` for an invalid name or binding |
| `GET` | `/api/model-profiles/{name}` | Fetch |
| `PUT` | `/api/model-profiles/{name}` | Replace all four bindings. The name is immutable |
| `DELETE` | `/api/model-profiles/{name}` | Delete. `409` naming every project that still uses it as its default |

`POST` takes `{"name": "<slug>", "roles": {...}}`; `PUT` takes only
`{"roles": {...}}`. `roles` must be exactly `default`, `smol`, `slow`, and
`plan`, each `{"model": "provider/model-id", "thinking": "<level>"}` with
neither field null. Unknown fields anywhere in the body are errors. The full
contract, including the identifier grammar and what validation does *not*
check, is in [Model profiles](model-profiles.md).

## Workflows

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/workflows` | Read-only catalog of every registered built-in workflow |

Each entry carries the workflow's sessions, its primary session, every declared
step with its kind, session, abstract role (agent steps only) and whether a
decision can route past it, and the engine-reserved judge with the role it
binds. Definitions ship with the daemon, so there is no CRUD and no change
event; the same catalog rides in the WebSocket snapshot.

## Tasks

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/tasks` | List |
| `GET` | `/api/tasks/{id}` | Detail, including sessions and step records |
| `POST` | `/api/tasks/preview` | Resolve the selections without creating anything; returns the resolved bindings, every declared step, the rendered branch, and a `preview_token` |
| `POST` | `/api/tasks` | Accept the reviewed resolution. `202`; spawning continues in the background. `409` with a `preview_changed` reason and the current resolution when it moved, `422` for an unusable prompt mention or an unknown field |
| `GET` | `/api/tasks/{id}/configuration` | Known facts, candidates, and unknown inputs for a task created before launch inputs were recorded |
| `POST` | `/api/tasks/{id}/configuration/preview` | Resolve a proposed continuation configuration |
| `POST` | `/api/tasks/{id}/configuration/confirm` | Pin it, once. `409` if already pinned or the token is stale |
| `POST` | `/api/tasks/{id}/continue` | Resume a confirmed task whose run was left interrupted. `409` unless it is `running` or `waiting` |
| `POST` | `/api/tasks/{id}/cleanup` | Remove workshop, delete clone, archive |
| `DELETE` | `/api/tasks/{id}` | Purge the record |

Both launch calls take `project_name`, `workflow_name`, `slug`, `prompt`, an
optional `model_profile` (omitted inherits the project default), and an
optional `workspace_overrides` object limited to `base_branch`,
`branch_pattern`, `workshop_additions`, and `preamble`.

They also take `step_overrides` and `auxiliary_overrides`: maps keyed by
declared agent-step name and by engine-reserved consumer name (`judge`), whose
entries carry an optional `model_profile` and an optional `role`. An omitted or
null field inherits, and an entry overriding neither resolves to the same
`preview_token` as no entry. An unknown or non-agent step, an unknown auxiliary
consumer, a role outside `default`/`smol`/`slow`/`plan`, an unknown profile, and
any unknown field inside an entry are all `422` at the named field, creating
nothing.

Acceptance adds the `preview_token`, which covers every consumer's complete
four-role map — so an edit to a profile's `slow` binding invalidates the review
even though no active model changed, while an edit to an unrelated profile does
not. Unknown top-level fields — including the retired `template_name` and the
old scalar `model`/`thinking` overrides — are refused, not ignored. See
[Task spawn](task-spawn.md).

A preview's step rows carry `declared_role` and a `binding` object — the source
profile, its source, the effective role, its source, and the full role map —
identical to what acceptance stores for that consumer, or `null` for a step
with no model. A task's `execution_inputs` carries `step_bindings` and
`auxiliary_bindings` in that same shape.

## Sessions

All paths are under `/api/tasks/{id}/sessions/{session}/agent`.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `.../steer` | Redirect the agent mid-turn |
| `POST` | `.../follow-up` | Queue a follow-up instruction |
| `POST` | `.../interrupt` | Interrupt the current turn |
| `POST` | `.../answer` | Answer a pending question or approval |
| `POST` | `.../stop` | Stop the agent process |
| `GET` | `.../state` | Current session state |
| `GET` | `.../stats` | Token, context, and cost counters |

## Workflow, review, and ship

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/tasks/{id}/workflow/resume` | Resume a workflow stopped at a gate |
| `POST` | `/api/tasks/{id}/review` | Open a review |
| `POST` | `/api/tasks/{id}/review/cancel` | Cancel and restore the clone |
| `POST` | `/api/tasks/{id}/ship/draft` | Ensure one initial agent draft, or explicitly replace it with `{"replace": true}`. A new/replacement request requires a live, `idle` primary agent; an ordinary repeated request returns observed ship state without a second agent turn. |
| `POST` | `/api/tasks/{id}/ship/commit` | Sign, commit, push, open the PR |

`ship/draft` returns `404` for an unknown task and `409` for an unavailable or
non-idle primary agent, an archived or already-published task, or an explicit
replacement while a ship attempt is active. See [Ship flow](ship-flow.md) for
draft lifecycle and field behavior. `ship/commit` returns `409` when GitHub
CLI identity or target eligibility cannot be established, the GPG key is not
`ready`, a ship is already in flight, the mode is not `squash` or `retain`, or
`retain` preconditions are unmet. GitHub refusal uses
`{"detail":{"message":...,"gh":...}}`; it is safe to show but creates no
ship job or local Git mutation.

## Daemon

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/daemon/info` | Version, bind, port, config path, data dir, audit log path |
| `GET` | `/api/gpg` | Last probed signing status: state, selected key, and candidates |
| `POST` | `/api/gpg/recheck` | Re-probe and broadcast |
| `GET` | `/api/gh` | Latest safe in-memory GitHub CLI identity and target eligibility status |
| `POST` | `/api/gh/recheck` | Re-probe global identity with no body; `{"task_id": id}` additionally checks that task's registered upstream. A completed observation remains `200`; only an unknown task is `404`. |
| `GET` | `/api/settings` | Effective settings |
| `PUT` | `/api/settings` | Set runtime overrides |
| `DELETE` | `/api/settings/{key}` | Clear one override |
| `GET` | `/api/settings/token` | Current bearer token |
| `POST` | `/api/settings/token/rotate` | Rotate; closes all WebSockets |

## WebSocket

`/api/ws` sends an authoritative snapshot, then deltas. Every frame is an
envelope:

```json
{"seq": 12, "ts": "2026-08-22T10:15:00+00:00", "type": "task_updated", "payload": {}}
```

A reconnect produces a fresh snapshot, so a dropped connection loses nothing.
Raw agent transcript events are not on this socket — they use separate,
buffered per-session channels, so a dashboard client is not made to receive
every frame of every agent.

The snapshot's `model_profiles` key carries the complete sorted profile list.
`model_profile_created` and `model_profile_updated` carry a full profile;
`model_profile_deleted` carries `{"name": "<slug>"}`. Project payloads carry
`default_model_profile`.

The snapshot's optional `gh` key contains `{identity, targets}`. Every
completed GitHub probe emits `gh_status` with the same full safe status object,
so reconnecting clients need no event replay. See [States](states.md) for the
identity and target vocabularies.

The protocol is documented in full in [WebSocket
protocol](../../develop/reference/websocket-protocol.md).
