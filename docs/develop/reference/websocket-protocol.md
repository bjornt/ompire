# WebSocket protocol

Two kinds of WebSocket channel exist, and the split is load-bearing.

`/api/ws` is the dashboard channel: an authoritative snapshot followed by
deltas describing everything a client needs to render task, project, session,
and settings state.

Per-session channels carry raw agent events for one session, buffered. They
exist so a dashboard client is not made to receive every transcript frame of
every running agent.

The rationale is in
[ADR-0004](../../adr/0004-use-rest-and-websocket-snapshot-deltas.md).

## No commands

The WebSocket accepts nothing. Every state-changing operation is a REST call.

This is not an arbitrary style choice — it means a client's connection state
can never affect the daemon's state, so a reconnecting client cannot replay a
command, and the daemon has one path to audit for mutations.

## Authentication

The same bearer token as REST. Rotating the token closes every open socket
with code `1008` and reason `token rotated`.

## Envelope

Every frame:

```json
{
  "seq": 12,
  "ts": "2026-08-22T10:15:00+00:00",
  "type": "task_updated",
  "payload": {}
}
```

| Field | Meaning |
|---|---|
| `seq` | Monotonic per connection. Restarts at zero on reconnect. |
| `ts` | ISO-8601, UTC. |
| `type` | Event type. |
| `payload` | Type-specific. |

`seq` orders frames within one connection. It is not a resumption cursor —
there is no "give me everything after N".

## Snapshot then deltas

On connect, the daemon sends a snapshot containing the current projects,
tasks, templates, workflow step records, and effective settings. Every frame
after it is a delta.

A reconnect produces a fresh snapshot. This is the whole recovery story: a
client that missed frames does not reconcile or replay, it re-reads. That is
why the frontend can be stateless with respect to the daemon, and why
restarting the browser cannot corrupt anything.

## Event types

Published on the dashboard channel:

| Type | Fires when |
|---|---|
| `project_created` | A project is registered |
| `spawn_step` | A spawn step starts, succeeds, or fails |
| `status_changed` | A session's status transitions |
| `workflow_step` | A workflow step advances |
| `ship_draft` | A ship draft is ready |
| `ship_step` | `commit`, `push`, or `pr` starts or completes |
| `gpg_status` | The signing-key probe completes |
| `settings_changed` | Effective settings change |
| `stats` | Token, context, and cost counters, throttled per task |

`spawn_step` and `ship_step` payloads carry `status` — `started`, `ok`, or
`failed` — and a failure carries the tool's stderr.

`stats` is throttled to at most one frame per task per
`stats_throttle_interval`, so a chatty agent cannot flood a dashboard.

## Per-session channels

`/api/ws/agents/{task_id}/{session}` carries one session's raw agent events,
authenticated the same way as the main socket. It replays a ring buffer of
`agent_ring_buffer_size` events, then streams live ones in the same envelope
form.

A client on a session channel receives neither main-socket registry events nor
other sessions' frames. Sessions of the same task have independent channels; a
client interested in two opens two.

Connecting to a session with no live agent closes with code `4404`.
Connecting with a session name the task's workflow does not declare closes
with an error and sends no events.

The buffer bounds memory per session and means a client attaching mid-turn
gets recent context rather than nothing — but it also means events older than
the buffer are gone. The channel is a live view, not a transcript store.

The channel closes after `agent_exited` is delivered, so a client can tell
"the agent finished" from "the connection dropped".

Child stderr lines arrive on the same channel wrapped as `agent_stderr`
events.

## Close codes

| Code | Meaning |
|---|---|
| `1008` | Policy violation — token rotated, or authentication failed |
| `4404` | No live agent behind this session channel |
