# Daemon API

Two surfaces: REST for every state-changing operation, and WebSocket for
observation. The split is architectural, not stylistic — the WebSocket accepts
no commands at all.

This page covers the surface as a whole and the REST half. The wire format,
snapshot contents, event inventory, per-session channels, and close codes are
in [WebSocket protocol](websocket-protocol.md). The rationale for the split is
in [ADR-0004](../../adr/0004-use-rest-and-websocket-snapshot-deltas.md).

## Commands over REST

Every state-changing operation is a REST endpoint under `/api/` with a
Pydantic-validated JSON body. A body that fails validation returns `422` with
field-level detail and changes nothing.

The full endpoint inventory is in the [API
reference](../../use/reference/api.md); the generated OpenAPI schema at
`/openapi.json` is authoritative for request and response bodies.

## Observation over WebSocket

`/api/ws` carries an authoritative snapshot followed by deltas — everything a
client needs to render task, project, session, and settings state.

Per-session channels at `/api/ws/agents/{task_id}/{session}` carry one
session's raw agent events, so a dashboard watching ten tasks is not made to
receive every transcript frame of ten agents.

Both are described in full in [WebSocket
protocol](websocket-protocol.md).

## Failures and recovery

| Condition | Response |
|---|---|
| REST body fails model validation | `422` with field-level detail, nothing changed |
| REST request without a valid bearer token | `401` |
| WebSocket upgrade without the valid token | Upgrade refused |
| Agent channel for an unknown or undeclared session name | Channel closes with an error, no events sent |
| Agent channel for a session with no live agent | Closes with code `4404` |
| Token rotated while sockets are open | All closed with code `1008` |

A reconnect produces a fresh snapshot reflecting everything that changed while
the client was away. There is no replay and no cursor, so a client that missed
frames re-reads rather than reconciling.
