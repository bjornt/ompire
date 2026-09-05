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
`/openapi.json` is authoritative for request and response bodies. The model
profile contract, including its identifier grammar and status codes, is in
[Model profiles](../../use/reference/model-profiles.md).

Three mutation boundaries are worth knowing before adding routes near them.

Profile input schemas forbid unknown fields at every nesting level, so a
misspelled role or binding key is a `422` rather than silently ignored
configuration. A duplicate name is classified by a serialized existence check
rather than by catching `IntegrityError`, so a failure on some other
constraint can never be reported as a duplicate name.

`PUT /api/projects/{name}` distinguishes an omitted `default_model_profile`
from an explicit `null` using Pydantic's `model_fields_set`, and passes that
distinction into the registry, which resolves an omission against the stored
row inside its write transaction rather than against the route's earlier
read. This is one optional field with that behavior — project `PUT` is
otherwise still a full replacement, not a PATCH.

Task creation is a two-call boundary. `POST /api/tasks/preview` resolves the
submitted workflow, project and profile into the effective inputs and returns
them with a `preview_token`; `POST /api/tasks` requires that token back. The
token is a fingerprint of the normalized inputs, their resolved values and
source attribution, and the workflow descriptor — not a timestamp — so an
unrelated profile or project edit does not invalidate a launch, and a relevant
one does. It is a comparison value, not a credential.

Acceptance then resolves twice. The first resolution validates and is what the
Git and file-mention work runs against; the authoritative one is taken again
inside a `BEGIN IMMEDIATE` reservation, compared against the same token, and
followed by the insert before the lock is released. Nothing is awaited,
spawned, or published inside that reservation. A token that no longer matches
is a `409` carrying the current preview for review, and creates no task,
workspace, or background job — the operator re-reviews rather than the daemon
retrying under settings they never saw. What this pins is configuration, not
the future contents of a Git branch.

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
