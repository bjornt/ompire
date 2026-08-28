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
| `POST` | `/api/projects` | Create. `201`, or `409` on duplicate name |
| `GET` | `/api/projects/{name}` | Fetch |
| `PUT` | `/api/projects/{name}` | Update |
| `DELETE` | `/api/projects/{name}` | Delete. `409` if tasks or templates reference it |
| `GET` | `/api/projects/{name}/files` | Repository paths for prompt `@` mentions. `409` if the checkout is missing or not a git repository |

## Templates

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/templates` | List |
| `POST` | `/api/templates` | Create |
| `GET` | `/api/templates/{name}` | Fetch |
| `PUT` | `/api/templates/{name}` | Update |
| `DELETE` | `/api/templates/{name}` | Delete |

## Tasks

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/tasks` | List |
| `GET` | `/api/tasks/{id}` | Detail, including sessions and step records |
| `POST` | `/api/tasks` | Spawn. Returns `202`; spawning continues in the background, or `422` for an unusable prompt mention |
| `POST` | `/api/tasks/{id}/cleanup` | Remove workshop, delete clone, archive |
| `DELETE` | `/api/tasks/{id}` | Purge the record |

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
`cached`, a ship is already in flight, the mode is not `squash` or `retain`, or
`retain` preconditions are unmet. GitHub refusal uses
`{"detail":{"message":...,"gh":...}}`; it is safe to show but creates no
ship job or local Git mutation.

## Daemon

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/daemon/info` | Version, bind, port, config path, data dir, audit log path |
| `GET` | `/api/gpg` | Last probed signing-key state |
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

The snapshot's optional `gh` key contains `{identity, targets}`. Every
completed GitHub probe emits `gh_status` with the same full safe status object,
so reconnecting clients need no event replay. See [States](states.md) for the
identity and target vocabularies.

The protocol is documented in full in [WebSocket
protocol](../../develop/reference/websocket-protocol.md).
