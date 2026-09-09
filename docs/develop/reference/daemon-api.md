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

Six mutation boundaries are worth knowing before adding routes near them.

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

The workflow the token names is resolved through the *library*, on the caller's
own connection (ADR-0031). During acceptance that is the connection holding the
write reservation, so an executable save or an archive committing alongside
cannot land between the check and the pin. An archived, draft-only, or
unreadable entry is a `workflow_name` refusal rather than a fallback to any
other revision.

A launch with [handoff inputs](../../use/reference/task-spawn.md#handoff-inputs)
does its Git reading in the same "slow work first" position as file mentions:
the target commit is resolved, its tree inspected for the attached
destinations, and each attachment's recorded base compared against it, all
outside every lock. That bounded observation is passed *into* both resolutions,
so the preview the operator read and the acceptance that recomputes it are
decided against the same immutable commit. Resolution itself stays pure with
respect to the world — it runs no Git and touches no filesystem, which is what
lets it run inside the reservation
([ADR-0035](../../adr/0035-refuse-to-publish-handoff-destinations.md)).

The token covers that commit, each attachment's manifest identity and
destinations, and the base acknowledgement, so a moved base, a replaced
revision, or a changed selection invalidates a review. Every revision's state,
acceptance and payload checksums are then re-checked on the reserved
connection, through helpers that open no second connection — a refusal must
never nest a reservation — and the task, its pinned inputs, and its result
references are inserted together. SQLite cannot freeze a Git ref, so the
pipeline verifies the cloned base still resolves to the reviewed commit and
fails the spawn if it does not; it never repins the task.

Acceptance then resolves twice. The first resolution validates and is what the
Git and file-mention work runs against; the authoritative one is taken again
inside a `BEGIN IMMEDIATE` reservation, compared against the same token, and
followed by the insert before the lock is released. Nothing is awaited,
spawned, or published inside that reservation. A token that no longer matches
is a `409` carrying the current preview for review, and creates no task,
workspace, or background job — the operator re-reviews rather than the daemon
retrying under settings they never saw. What this pins is configuration and
the procedure, not the future contents of a Git branch, model output, or tool
versions.

Workflow authoring is the fourth. `/api/workflow-library` routes separate three
operations that a single "save" would conflate: a draft save persists inert
text, a validate parses text and persists nothing, and an executable save
validates the exact text submitted to it and then — inside one reservation —
compares the entry's edit version, retains the document, and moves the current
selection. Parsing happens outside the reservation; the lock covers only the
version check, the insert, and the update. The response is the committed row
read back inside that same transaction, never a later unreserved read.

The version compared is the entry's **edit** version, not its content revision:
a revision is a digest of normalized semantics, so two tabs whose YAML differs
only in comments would produce the same revision and the second save would
silently overwrite the first. A stale submission is a `409` carrying the
entry's actual version, and writes nothing. There is no force parameter.

`/api/workflow-library/document` is deliberately outside all of that. It
translates a draft between text and structured data for an editing client and
takes no reservation, because it writes nothing: no row, no revision, no event.
It is also not a token — an executable save re-validates the exact text handed
to it regardless of what any earlier conversion or validation said.

Task detail's configuration routes carry a second, structurally identical
boundary for the tasks an upgrade left blocked.
`POST /api/tasks/{id}/configuration/preview` resolves a continuation without
writing it and returns a token covering the candidate revision, the inputs a
confirmation would write, the run's position and history boundary, and the
compatibility result; `confirm` re-resolves under the reservation and compares.
A task that only lacks a workflow revision supplies no launch fields — its
model, branch, and preamble were reviewed once — and the write is the narrowest
possible one, filling a null binding while carrying every already-pinned field
through untouched. Confirmation records the decision but starts nothing; the
existing explicit Continue action is still what resumes a run.

The compatibility result is a refusal as often as a permission. A candidate
whose format reads results differently from the ones already on record cannot
explain that history, so it is reported incompatible with the reasons named
rather than offered and silently reinterpreting them. There is no automatic
format upgrade.

Trusted delivery is the fifth, and it is the one where the boundary is
deliberately *not* in this layer at all
([ADR-0032](../../adr/0032-bind-trusted-delivery-to-retained-candidates.md)).
`/api/tasks/{id}/ship/*` parses and authenticates; every review, target, mode,
credential, exclusivity, and replay check happens inside the delivery service,
which re-loads its own contract from the durable record rather than trusting a
caller-supplied snapshot. A direct service call is admitted exactly as a REST
request is, and there is no no-review path and no tokenless path.

The shape is the same preview-then-confirm boundary as task creation, with two
differences. `ship/preview` resolves the delivery the *run's own procedure*
permits and returns the actions still to run alongside every reason it is
refused, so an operator fixes them together rather than one attempt at a time.
The ending and the mode are derived from the chain the run's answer would
authorize; a caller may state one, and a disagreement is reported rather than
obeyed. And its token covers only the inputs that chain will actually use — plus
*which decision* this is, so a confirmation prepared against one question cannot
be replayed against the next one with identical content.

`ship/commit` is the single confirmation operation. When the run is waiting at
an approval it commits the answer, the delivery authorization it produces, and
the run's move to its first action in one transaction, and the run then performs
the actions the answer authorized. `POST /api/tasks/{id}/workflow/resume` makes
the *same* call for an approving answer given from task detail; a generic resume
without a preview token is refused, because without it there is no evidence the
operator saw what they were authorizing.

`ship/push` and `ship/pr` exist for a grant made before workflows owned
publication: they continue that grant's own prefix and never start an implicit
earlier action or extend it. `ship/reconcile` records one decision about an
unresolved effect and writes nothing privileged: `retry` only makes a
proven-not-executed action eligible for a fresh preview and confirmation.

Admission itself lives in `runauthority.py`, above both managers. It reads the
pinned revision, the run's current attempt, the persisted question and its
committed decision, the review iteration that question froze, and the delivery
journal — and its refusals drive the UI projections, so a page cannot offer a
control the service would decline
([ADR-0033](../../adr/0033-scope-trusted-delivery-authority-to-the-workflow-run.md)).

Every one of these responds with the task's whole versioned delivery
projection, the same document the WebSocket publishes — so a response and its
broadcast converge through one reducer instead of racing.

Durable result capture is the sixth, and it is the one boundary that guards
*bytes* rather than authority
([ADR-0034](../../adr/0034-retain-durable-task-results-outside-the-workspace.md)).

`POST /api/tasks/{id}/results` is a two-part boundary. Admission — the task
exists, is not archived, its clone is confined to the task root, and the
workspace guard admits a new `HOST` owner — happens before any identity is
recorded. Then the request id and normalized selection commit, and only after
that does `capturing` become visible. The route returns `202` and the metadata
projection; the manager supervises the capture to `ready` or `failed`
independently of the browser connection, so a lost response is recovered from
result history rather than by capturing whatever the workspace holds now.

The request id is the replay key. The same id with the same selection returns
the original operation *including its failure*; the same id with a different
selection is `409`. Nothing here retries on the caller's behalf.

Revision-scoped commands carry the expectation they were decided under —
`expected_manifest_id` for acceptance, plus `expected_version` and an explicit
acknowledgement for purge — and are refused rather than retargeted when it no
longer holds. Every one of them responds with the task's whole versioned result
document, the same one the WebSocket publishes, so a response and its broadcast
converge through one reducer.

Status codes are narrow on purpose: `422` for a request that was never well
formed, `404` for an unknown task, revision, or file, `409` for a state or
expectation that no longer holds, and `410` for a purged revision — a real,
permanent answer with a readable record behind it, not a missing resource.

Downloads are ordinary authenticated responses with a body. They carry
`Content-Disposition: attachment`, `X-Content-Type-Options: nosniff`, and
`Cache-Control: private, no-store`; there is no token-bearing URL and no static
result directory, because a result is untrusted agent-authored content.

`POST /api/tasks/{id}/workflow/resume` carries the same shape of guard for a
*human* decision. The request names the waiting attempt, the daemon decides
from that attempt which of three waits it is, and — for a gate with declared
choices — the decision, the attempt's completion, and either the successor or
the run's named ending commit in one transaction *before* the response returns.
The parked run is notified afterwards, so an accepted answer is durable and a
repeated or stale one advances nothing
([ADR-0030](../../adr/0030-commit-human-decisions-before-advancing.md)).

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
| Result command against a revision belonging to another task | `404` — the task scope in the path is part of the authorization |
| Read or download of a purged revision | `410`, its record still readable |
| Token rotated while sockets are open | All closed with code `1008` |

A reconnect produces a fresh snapshot reflecting everything that changed while
the client was away. There is no replay and no cursor, so a client that missed
frames re-reads rather than reconciling.
