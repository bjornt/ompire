# WebSocket protocol

Two kinds of WebSocket channel exist, and the split is load-bearing.

`/api/ws` is the dashboard channel: an authoritative snapshot followed by
deltas describing everything a client needs to render task, project, session,
and settings state.

Per-session channels carry raw agent events for one session, buffered. They
exist so a dashboard client is not made to receive every transcript frame of
every running agent.

The rationale is in
[ADR-0004](../../adr/0004-use-rest-and-websocket-snapshot-deltas.md). The REST
half of the surface, and the failure responses both halves share, are in
[Daemon API](daemon-api.md).

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

On connect, after authentication, the daemon sends a snapshot carrying the
full current registry state:

| Key | Contents |
|---|---|
| `projects` | All projects |
| `templates` | All templates |
| `model_profiles` | All model profiles, sorted by name, each with its four role bindings |
| `tasks` | All non-purged tasks, each carrying its workflow fields |
| `sessions` | Per task, a per-session map of current status |
| workflow state | Per-task run status, current step, and gate message |
| `settings` | The effective settings map |
| `gpg` | Current signing status: `state`, `selected` key, `candidates`, `cache_ttl`, `detail`, `checked_at` — public identifiers only |
| `gh` | Current in-memory GitHub CLI identity plus canonical target eligibility map; no credential value or token fragment |
| `reviews` | Per task, durable review status and iterations, plus the live reviewer's URL and port when one is running (`null` otherwise) |
| `ships` | Per task, in-memory ship status, mode, draft, commit sha, PR URL, error, and latest `last_step` projection |
| attention | Current attention entries |

Every frame after the snapshot is a delta.

A reconnect produces a fresh snapshot. This is the whole recovery story: a
client that missed frames does not reconcile or replay, it re-reads. That is
why the frontend can be stateless with respect to the daemon, and why
restarting the browser cannot corrupt anything.

## Delivery

Fan-out always runs on the daemon's event loop, whichever context published the
event. Synchronous REST routes run in FastAPI's threadpool, so `EventHub`
hands their events back to the loop rather than touching a subscriber queue
from another thread — a queue written from off the loop wakes its reader
through a non-thread-safe path, leaving the event to wait for unrelated
activity. An event is therefore never delivered late because the daemon
happened to be idle.

Each producer's events are delivered in the order it published them. Ordering
*between* concurrent producers is not defined, and no client depends on it.

## Applying a mutation's own response

A command's REST response is an authoritative daemon outcome, not just an
acknowledgement, so a client may apply it to its own state through the very
reducer path the matching event uses. The Projects view does this, which is why
a new card appears the moment the daemon answers.

This is not the reconciliation the reconnect rule above rules out. It carries
two obligations:

- **Deltas are idempotent per key.** Applying a create or update for a key
  already present replaces it rather than appending, so the response and its
  event together can never produce two entries — in either arrival order.
- **A snapshot still replaces everything.** Nothing applied from a response
  outlives the next snapshot that omits it.

A client that applies no responses is still correct; it just learns the outcome
one event later.

Model profiles follow the same contract as projects: `model_profile_created`
and `model_profile_updated` carry the full profile and are upserted by name,
`model_profile_deleted` carries `{"name": "<slug>"}` and filters, and the
snapshot's `model_profiles` replaces the collection. Applying a mutation's
response and its matching event in either order leaves exactly one row.

Only committed mutations are published. A refusal — an invalid profile
replacement, a deletion blocked by a referencing project, a project update
naming a profile that does not exist — broadcasts nothing, so no client can
observe a successful-looking change that was not written. A project's payload
carries `default_model_profile` on the events it already had; there is no
separate assignment event.

## Event types

Published on the dashboard channel:

| Type | Fires when |
|---|---|
| `project_created`, `project_updated`, `project_renamed`, `project_deleted` | Project mutations |
| `template_created`, `template_updated`, `template_deleted` | Template mutations |
| `model_profile_created`, `model_profile_updated`, `model_profile_deleted` | Model profile mutations |
| `task_created`, `task_updated`, `task_deleted` | Task mutations |
| `project_setup_step` | A clone-mode project setup step starts, succeeds, or fails |
| `spawn_step` | A spawn step starts, succeeds, or fails |
| `workflow_step` | A workflow step transitions |
| `status_changed` | A session's status transitions |
| `question_posted`, `question_resolved` | A pending question appears or clears |
| `attention`, `attention_cleared` | A task's attention tier changes |
| `stats`, `advisory` | Session telemetry and decorations |
| `review_started`, `review_iteration`, `review_finished` | Review lifecycle |
| `ship_draft` | A parsed agent draft is ready |
| `ship_step`, `ship_finished` | A `draft`, `commit`, `push`, or `pr` step starts, completes, fails, or the publication reaches a terminal state |
| `gpg_status` | The signing-key probe result changes |
| `gh_status` | A completed GitHub identity or target probe replaced the full safe `gh` projection |
| `settings_changed` | Effective settings change |

`project_setup_step` carries the project name, the step (`prepare`, `clone`,
`fork-remote`, `finalize`), and a `status` of `started`, `ok`, or `failed`,
with git's stderr on failure. It is transient and never part of the snapshot:
the durable outcome is the project's own `setup_state`/`setup_error`, which
are broadcast as `project_updated` and are what a reconnecting client renders.

`spawn_step` and `ship_step` payloads carry `status` — `started`, `ok`, or
`failed` — and a failure carries the relevant detail. Ship-step `detail` is a
string for a draft, push, or pull-request failure, a pull-request URL on a
successful `pr`, and commit metadata on a successful `commit`.

Draft lifecycle is explicit: `ship_step` with `step: "draft"` and
`status: "started"` precedes the agent request; a parsed success publishes
`ship_draft` followed by `draft`/`ok`; a timeout, transport failure, missing
text, or marker parse failure publishes `draft`/`failed`. The daemon keeps the
latest `last_step` in its in-memory `ships` snapshot, so a client that missed a
delta can render the same Draft-stage retry state after reconnect. Ship state is
not durable except for a task's pull-request URL; a restart replaces it with an
empty ship projection rather than replaying an agent request.

`gh` is environmental observation rather than durable publishing policy. Its
identity states are `unknown`, `missing`, `unauthenticated`, `ready`, and
`error`; target states are `unchecked`, `allowed`, `denied`, and `error`.
Target entries carry the canonical target and the safe host/login/source tuple
that produced them. A changed or failed identity probe clears earlier targets;
clients replace this full projection rather than replaying events.

`workflow_step` carries the task id, step name, kind, and status of `started`,
`ok`, `failed`, or `waiting` — with error text on failure and the
operator-facing gate message on waiting.

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
