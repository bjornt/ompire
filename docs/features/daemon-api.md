# Daemon API

## Overview

Two surfaces: REST for every state-changing operation, and WebSocket for
observation. The split is architectural, not stylistic — the WebSocket accepts
no commands at all.

## States and behavior

### Commands over REST

Every state-changing operation is a REST endpoint under `/api/` with a
Pydantic-validated JSON body. A body that fails validation returns `422` with
field-level detail and changes nothing.

### The main WebSocket

On connect, after authentication, the daemon sends a snapshot containing the
full current registry state:

| Key | Contents |
|---|---|
| `projects` | All projects |
| `templates` | All templates |
| `tasks` | All non-purged tasks, each carrying its workflow fields |
| `sessions` | Per task, a per-session map of current status |
| workflow state | Per-task run status, current step, and gate message |
| `settings` | The effective settings map |
| `gpg` | Current signing-key state |
| `reviews` | Per task, review status, URL, and iterations |
| `ships` | Per task, ship status, mode, draft, commit sha, PR URL |
| attention | Current attention entries |

Every message uses the envelope `{seq, ts, type, payload}` with a
per-connection monotonically increasing `seq`.

Subsequent messages are deltas. A reconnect produces a fresh snapshot
reflecting everything that changed while the client was away — there is no
replay and no cursor.

### Per-session agent channels

`/api/ws/agents/{task_id}/{session}`, authenticated the same way, sends that
session's buffered events then live events in the same envelope form.

A client connected to a session channel receives neither main-socket registry
events nor other sessions' frames. Sessions of the same task have independent
channels; a client interested in two opens two.

The channel closes after `agent_exited` is delivered, so a client can tell "the
agent finished" from "the connection dropped".

The separation exists so a dashboard watching ten tasks is not made to receive
every transcript frame of ten agents.

## Failures and recovery

| Condition | Response |
|---|---|
| REST body fails model validation | `422` with field-level detail, nothing changed |
| REST request without a valid bearer token | `401` |
| WebSocket upgrade without the valid token | Upgrade refused |
| Agent channel for an unknown or undeclared session name | Channel closes with an error, no events sent |
| Agent channel for a session with no live agent | Closes with code `4404` |
| Token rotated while sockets are open | All closed with code `1008` |

## Interfaces

The full endpoint inventory is in the [API
reference](../use/reference/api.md); the generated OpenAPI schema at
`/openapi.json` is authoritative for bodies.

Event types on the main stream:

| Event | Fires when |
|---|---|
| `project_created`, `project_renamed` | Project mutations |
| `template_created`, `template_updated`, `template_deleted` | Template mutations |
| `task_created`, `task_updated`, `task_deleted` | Task mutations |
| `spawn_step` | A spawn step starts, succeeds, or fails |
| `workflow_step` | A workflow step transitions |
| `status_changed` | A session status transitions |
| `question_posted`, `question_resolved` | A pending question appears or clears |
| `attention`, `attention_cleared` | A task's attention tier changes |
| `stats`, `advisory` | Session telemetry and decorations |
| `review_started`, `review_iteration`, `review_finished` | Review lifecycle |
| `ship_draft`, `ship_step`, `ship_finished` | Ship lifecycle |
| `gpg_status` | The signing-key probe result changes |
| `settings_changed` | Effective settings change |

`workflow_step` carries the task id, step name, kind, and status of
`started`, `ok`, `failed`, or `waiting` — with error text on failure and the
operator-facing gate message on waiting.
