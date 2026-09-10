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
| `workflow_library` | Every library entry (ADR-0031): `name`, `origin`, `archived`, edit `version`, `has_draft`, `current_revision`/`current_format`, `available` with `unavailable_reason`/`unavailable_detail`, timestamps, and a `descriptor` present exactly when the entry is a valid launch choice. Raw draft text and revision history are fetched over REST, never carried here |
| `workflow_catalog` | Only the workflows a new launch may select: each one's current revision and format, its sessions, whether it declares review, the privileged effects it could perform (empty when it can perform none), and each declared step with its kind, session, abstract role, conditional flag, and — for a delivery step — its one action and the approval that can authorize it. Every model consumer is one of those steps. Derived from the same library read as `workflow_library`, so the two cannot disagree |
| `model_profiles` | All model profiles, sorted by name, each with its four role bindings |
| `tasks` | All non-purged tasks, each carrying its workflow fields, its pinned `workflow_revision` and readiness, the primary session *its* definition declares, its accepted `execution_inputs`, and `needs_configuration` |
| `sessions` | Per task, a per-session map of current status, plus the native model a live session reports |
| workflow state | Per-task run status, current step, gate message, and any uncertainty pause |
| `settings` | The effective settings map |

Every `tasks` entry — in the snapshot, in `task_created`/`task_updated`
deltas, and in REST task responses — is serialized by one canonical
projection, `oversight/tasks.py:task_payload`, so a client cannot see two
shapes for the same row. The wire behavior is unchanged; only the owner
moved. Storage (`work/tasks.py`) does not depend on it.
| `gpg` | Current signing status: `state`, `selected` key, `candidates`, `cache_ttl`, `detail`, `checked_at` — public identifiers only |
| `gh` | Current in-memory GitHub CLI identity plus canonical target eligibility map; no credential value or token fragment |
| `reviews` | Per task, durable review status and iterations, each naming the candidate it graded, the workflow attempt that asked for it, and the reviewer's own report with the state of what was retained, plus the live reviewer's URL and port when one is running (`null` otherwise) |
| `ships` | Per task, the durable delivery projection: `version`, disposition, ending and mode, candidate and review identity, draft, completed and remaining actions, concrete results, every action attempt and reconciliation decision, the delivery history, and an `authority` block saying what the run's own procedure currently permits |
| `task_results` | Per task with any capture history, the durable result document (ADR-0034): `version` and every revision's state, availability, manifest and content identities, predecessor, selection, file list with lengths/media types/checksums, workflow attempt/provenance and supplied input-result context when known, and acceptance and purge decisions. Metadata only — file text, comparisons and ZIPs are fetched for the one revision an operator selected, never broadcast |
| `retained_results` | Per task, the counts behind the Tasks index's Retained results section: total, retained, accepted, and retained bytes. Derived from the same rows as `task_results`, so the two cannot disagree |
| attention | Current attention entries |

Every frame after the snapshot is a delta.

A reconnect produces a fresh snapshot. This is the whole recovery story: a
client that missed frames does not reconcile or replay, it re-reads. That is
why the frontend can be stateless with respect to the daemon, and why
restarting the browser cannot corrupt anything.

The main socket subscribes to the event hub **before** it reads the snapshot,
and releases the subscription on every exit path. The library is editable and a
delivery can commit at any moment, so a mutation landing while the snapshot is
being assembled would otherwise fall into the gap between the read and the
subscription and never arrive.

What queued up during that overlap is then filtered: only deltas carrying a
version the client orders by — `workflow_library_updated`, `ship_updated` and
`task_results_updated` — are forwarded. Re-delivering an entry the snapshot already holds is a no-op, so
nothing is lost. Every other delta is unversioned, and one published *before*
the snapshot read but delivered after it would move the client backwards; those
are dropped, exactly as the pre-subscription gap dropped them, because the
snapshot is already newer than all of them. Adding a version to another payload
is what would let it join that set.

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
| `model_profile_created`, `model_profile_updated`, `model_profile_deleted` | Model profile mutations |
| `workflow_library_updated` | One committed library mutation — create, draft save, executable save, archive, or restore — as the **whole** entry, in the snapshot's own shape. There is no delete: archiving is an update. Ordering is carried by the entry's edit `version` rather than by delivery order, so a receiver drops an older version and treats an equal one as already applied. The launch catalog follows from the same payload: an entry arriving with `descriptor: null` leaves the catalog in the same step |
| `task_created`, `task_updated`, `task_deleted` | Task mutations |
| `project_setup_step` | A clone-mode project setup step starts, succeeds, or fails |
| `spawn_step` | A spawn step starts, succeeds, or fails |
| `workshop_additions` | Which additions source applied for a task's launch, including when the selected one was absent |
| `session_model` | A session's omp child reported the model it is running and the thinking level omp resolved. Published only after a policy was verified, so a session mid-transition never appears to have applied one |
| `workflow_step` | A workflow step transitions |
| `status_changed` | A session's status transitions |
| `question_posted`, `question_resolved` | A pending question appears or clears |
| `attention`, `attention_cleared` | A task's attention tier changes |
| `stats`, `advisory` | Session telemetry and decorations |
| `review_started`, `review_iteration`, `review_finished` | Review lifecycle |
| `ship_updated` | A task's delivery projection changed; carries the whole versioned document |
| `task_results_updated` | A task's durable results changed — a capture opened, finished or failed, a revision was accepted, found unavailable, or purged, a consumer task pinned or released it as a launch input, or a checkout export was admitted, settled, reconciled, or acknowledged. Carries the whole versioned document for that task, metadata only, and is published only after the daemon committed the change. A client drops an older `version` and treats an equal one as already applied; `task_deleted` clears the task's results |
| `gpg_status` | The signing-key probe result changes |
| `gh_status` | A completed GitHub identity or target probe replaced the full safe `gh` projection |
| `settings_changed` | Effective settings change |

Checkout export rides `task_results_updated` as an `exports` list on the
revision it delivered: state, destinations, per-file outcomes, and any created
directories
([ADR-0036](../../adr/0036-install-exported-result-files-without-replacing-them.md)).
No retained file text, no destination content, and no conflict diff is ever in
that document. Those are fetched for the one export or preview an operator asked
for, over REST, `no-store` — a conflict diff can carry the contents of the
operator's own checkout, and broadcasting it to every connected client would
publish it far more widely than the file ever was.

`project_setup_step` carries the project name, the step (`prepare`, `clone`,
`fork-remote`, `finalize`), and a `status` of `started`, `ok`, or `failed`,
with git's stderr on failure. It is transient and never part of the snapshot:
the durable outcome is the project's own `setup_state`/`setup_error`, which
are broadcast as `project_updated` and are what a reconnecting client renders.

`spawn_step` payloads carry `status` — `started`, `ok`, or `failed` — and a
failure carries the relevant detail. The step names are `fetch`, `clone`,
`branch`, `inputs`, and `workshop`; `inputs` is emitted only for a task
launched with handoff inputs, so a client keys off the events it receives
rather than off a fixed count
([ADR-0035](../../adr/0035-refuse-to-publish-handoff-destinations.md)).

A task's `authority` block is the same resolution the trusted service admits
against, projected: how authority would be established right now — an
unanswered approval, an already-authorized action, a pre-upgrade grant, or
nothing — the question and the answers it offers with what each would
authorize, the publication text the workflow suggested, and, when nothing is
possible, the reason. Both decision surfaces read it, so neither can offer a
control the service would refuse or hide one it would accept
([ADR-0033](../../adr/0033-scope-trusted-delivery-authority-to-the-workflow-run.md)).

`ship_updated` is the delivery surface, and it is deliberately not a step event
([ADR-0032](../../adr/0032-bind-trusted-delivery-to-retained-candidates.md)). It
carries the same whole document the snapshot carries, published only *after* the
daemon committed it, and every REST command response is that same document. A
client renders what it last received rather than assembling a picture from
fragments, which is what makes a lost or duplicated delta harmless.

Each projection carries a per-task monotonic `version`. Clients apply a payload
only when its version is at least the one they hold, so an out-of-order or
duplicated delivery is dropped rather than moving the client backwards. Equal
versions still apply: a command response and its broadcast carry the same
version and must converge rather than race.

Because delivery state is durable, a reconnect after a restart serves the real
current state — including a delivery that stopped with an unresolved effect,
with the action, its expected target, and the observed evidence attached. A
restart never replays an agent draft request or repeats a privileged write.

`gh` is environmental observation rather than durable publishing policy. Its
identity states are `unknown`, `missing`, `unauthenticated`, `ready`, and
`error`; target states are `unchecked`, `allowed`, `denied`, and `error`.
Target entries carry the canonical target and the safe host/login/source tuple
that produced them. A changed or failed identity probe clears earlier targets;
clients replace this full projection rather than replaying events.

`workflow_step` carries the task id, the attempt's `seq`, the step name, kind,
and a status of `started`, `ok`, `failed`, or `waiting` — with error text on
failure. **The sequence identifies the attempt**: a bounded step is visited
repeatedly under one name, so a client matching on the name alone would fold
two iterations of `fix` into a single row.

A `waiting` frame says which of three waits this is:

| Frame carries | Meaning | Operator action |
|---|---|---|
| `gate` (a versioned snapshot) | A gate with declared choices: its message, the choices offered with their destinations, and the evidence identities it asks about | Answer with one `choice_id` |
| `message` | A format-1 declared gate | Resume, with an optional note |
| `pause` | The engine stopped rather than deciding: reason, message, blocked step, and the step a retry re-enters | Retry the blocked step |

Clients must keep them apart, because the action differs — a choice takes a
declared route, a resume continues past the gate, and a retry re-enters the
blocked step without continuing past it.

The gate snapshot is **sent rather than looked up**: a client renders the
question that was actually asked instead of reconstructing choices from
today's catalog for a definition that may have changed. The `ok` frame for an
answered gate carries the same snapshot with its `decision` attached.

A result-bound gate snapshot also carries its exact `result_id`, `manifest_id`,
and capture attempt. A choice marked `requires_result_acceptance` stays
available for rendering, but the daemon refuses it until that same readable
revision is accepted; a later result projection cannot retarget the gate.

A task whose pinned definition cannot be resolved still appears in the snapshot
and still receives `task_updated`. It reports `workflow_ready: false` with a
classified reason, and its `workflow_primary_session` is `null` rather than a
guess.

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
Connecting with a session name the task's *pinned* definition does not declare
closes with an error and sends no events. That includes the retired `judge`
session on an older task: nothing prompts it any more, so it is not addressable
as a live session, and its transcript is read through the task's step history
instead.

A channel whose child is being replaced to apply a different model policy
closes with `4409` rather than `1000`
([ADR-0027](../../adr/0027-hand-off-model-policy-between-turns.md)). The
logical session is continuing, so a client must reconnect: the replacement
carries the retired child's ring buffer forward, and a reconnect replays it
from the top. Treating `4409` as terminal would end a transcript mid-session.

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
| `4404` | No live agent behind this session channel — retry, the session may be starting |
| `4409` | The child was replaced to apply a new model policy — reconnect to the replacement |
