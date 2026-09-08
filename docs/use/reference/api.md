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
| `GET` | `/api/workflows` | Read-only catalog of every *launchable* workflow |
| `GET` | `/api/workflows/revisions/{revision}` | One retained definition, by content identity |
| `GET` | `/api/workflows/revisions/{revision}/yaml` | The same revision as a standalone YAML definition |

Each catalog entry carries the workflow's current `revision` and `format`, its
sessions, its primary session, and every declared step with its kind, session,
abstract role (agent steps only) and whether a route or its own condition can
pass it by. Every model consumer is one of those steps.

The catalog holds only entries a new launch may select: not archived, and with a
current revision that reads back as an executable definition. Everything that
*exists* is under [Workflow library](#workflow-library) below. Both ride in the
WebSocket snapshot, derived from one read.

The revision endpoint returns the identity, the format, the primary session,
the sessions, and the normalized `definition` document itself. It is addressed
by content identity, not by name, because a name says what a *new* launch would
pin and this answers what a given task accepted. An unknown revision is `404`.
A retained document that cannot be read — damaged, or written for a format this
daemon does not implement — is `409` with reason `workflow_definition_unavailable`
and a specific `unavailable_reason`; it is never executed to answer a read.

The `/yaml` form emits that same retained document as a complete YAML
definition, verified to load back to the same revision before it is returned.
Comments and formatting are not preserved — a revision is a normalized document.
Its `404`/`409` responses are the ones above.

## Workflow library

Authoring lives here ([ADR-0031](../../adr/0031-let-operators-own-a-workflow-library-above-retained-revisions.md)).
Every route is authenticated, and none of them starts a task.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/workflow-library` | Every entry: draft-only, archived, and unavailable included |
| `POST` | `/api/workflow-library` | Create a custom entry from `yaml`, from `source_revision`, or from the starter |
| `POST` | `/api/workflow-library/validate` | Check `yaml` (optionally against an entry `name`). Persists nothing |
| `POST` | `/api/workflow-library/document` | Translate one draft between `yaml` and `document`, and report what it means. Persists nothing |
| `GET` | `/api/workflow-library/{name}` | One entry: summary, raw draft text, retained revision history |
| `PUT` | `/api/workflow-library/{name}/draft` | Save inert draft text |
| `POST` | `/api/workflow-library/{name}/revisions` | Validate, retain, and select an executable revision |
| `POST` | `/api/workflow-library/{name}/archive` | Remove from launch choices; deletes nothing |
| `POST` | `/api/workflow-library/{name}/restore` | Make the retained current revision eligible again |

An entry summary carries `name`, `origin` (`builtin`/`custom`), `archived`,
`version`, `has_draft`, `current_revision`, `current_format`, `available`, an
`unavailable_reason` and `unavailable_detail` when it is not, timestamps, and a
`descriptor` — present exactly when the entry is a valid launch choice, which is
what makes the launch catalog derivable from the library.

`version` is the entry's **edit** version, not its content revision. It advances
on every successful mutation, including a comment-only draft save, and every
mutation of an existing entry must submit the `expected_version` it loaded.

| Status | Reason | Meaning |
|---|---|---|
| `404` | — | No entry, or no such retained revision |
| `409` | `workflow_version_conflict` | Someone committed first. Nothing was written; the body carries `current_version` |
| `409` | `workflow_name_taken` | The name belongs to a live, archived, or built-in entry |
| `409` | `workflow_builtin_read_only` | Built-ins are packaged examples; duplicate instead |
| `409` | `workflow_archived` | Restore the entry before editing it |
| `422` | `workflow_document_invalid` | With `location`, `message`, and `line`/`column` when the parser supplied them |
| `422` | `workflow_format_unsupported` | The document declares a `format` this daemon does not implement |

### Draft conversion

`POST /api/workflow-library/document` is what the visual editor uses to move
one draft between the text the library stores and the data a form edits. Send
exactly one of `yaml` or `document`, plus an optional `name` for the same
name-match check `validate` applies; sending both, neither, or an undeclared
key is `422`.

It answers `{document, yaml, validation}`:

- `document` is the **parsed draft**, not a canonicalized definition. Unknown
  fields and incomplete values survive it, so a half-authored flow comes back
  whole rather than tidied into something the author did not write.
- `yaml` is the text form of that same draft. For `yaml` in, it is the text you
  submitted, unchanged. For `document` in, it is emitted through the same
  serializer a revision export uses and then parsed again, so the two
  representations cannot disagree.
- `validation` is either `{"ok": true, …}` — the same projection `validate`
  returns — or `{"ok": false, reason, location, message, …}`, the located
  refusal shape. A draft that parses but is not yet a workflow gets the second
  one **beside its unchanged data**, which is what lets incomplete visual work
  be saved and reopened.

A document that cannot be parsed or exceeds the loader's byte, depth, node, or
step bounds is a `422` with the usual `workflow_document_invalid` shape. A
declared `format` this daemon does not implement is reported as
`workflow_format_unsupported` in `validation` and is never converted to the
current format: syntactically safe conversion is not semantic support.

The call persists nothing, retains no revision, publishes no event, runs no
command, and authorizes nothing. Draft and executable saves still take exact
YAML and the `expected_version` the client loaded, and an executable save still
validates what it is given.

There is no force-overwrite and no automatic merge: a conflict changes nothing,
and reconciling is the client's decision. Drafts accept any UTF-8 up to 1 MiB
and are never parsed; only an executable save validates, and it validates the
exact text submitted to it — an earlier `validate` response is informative, not
an authorization. Validation and import execute no commands, fetch no URLs, and
open no path named in a document.

Every committed mutation publishes one full-entry `workflow_library_updated`
event over the WebSocket.

## Tasks

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/tasks` | List |
| `GET` | `/api/tasks/{id}` | Detail, including sessions and step records |
| `POST` | `/api/tasks/preview` | Resolve the selections without creating anything; returns the resolved bindings, every declared step, the rendered branch, and a `preview_token` |
| `POST` | `/api/tasks` | Accept the reviewed resolution. `202`; spawning continues in the background. `409` with a `preview_changed` reason and the current resolution when it moved, `422` for an unusable prompt mention or an unknown field |
| `GET` | `/api/tasks/{id}/configuration` | Known facts, candidates, unknown inputs, and the workflow continuation candidate for a task the upgrade left blocked |
| `POST` | `/api/tasks/{id}/configuration/preview` | Resolve a proposed continuation without writing it |
| `POST` | `/api/tasks/{id}/configuration/confirm` | Pin it, once. `409` if already pinned or the token is stale |
| `POST` | `/api/tasks/{id}/continue` | Resume a confirmed task whose run was left interrupted. `409` unless it is `running` or `waiting` |
| `POST` | `/api/tasks/{id}/cleanup` | Remove workshop, delete clone, archive |
| `DELETE` | `/api/tasks/{id}` | Purge the record |

Both launch calls take `project_name`, `workflow_name`, `slug`, `prompt`, an
optional `model_profile` (omitted inherits the project default), and an
optional `workspace_overrides` object limited to `base_branch`,
`branch_pattern`, `workshop_additions`, and `preamble`.

They also take `step_overrides`: a map keyed by declared agent-step name whose
entries carry an optional `model_profile` and an optional `role`. An omitted or
null field inherits, and an entry overriding neither resolves to the same
`preview_token` as no entry. An unknown or non-agent step, a role outside
`default`/`smol`/`slow`/`plan`, an unknown profile, and any unknown field
inside an entry are all `422` at the named field, creating nothing.

`auxiliary_overrides` is retired with the engine's judge. Any entry in it is
`422` at the named field rather than silently dropped: the model it names no
longer runs.

The preview also reports `workflow_revision`, `workflow_format`, and the
definition's primary session and sessions — the exact procedure acceptance
would pin.

Acceptance adds the `preview_token`, which covers the workflow revision and
every consumer's complete four-role map — so editing a prompt or a route
invalidates the review even though the step list is identical, and an edit to a
profile's `slow` binding invalidates it even though no active model changed,
while an edit to an unrelated profile or workflow does not. Unknown top-level
fields — including the retired `template_name` and the old scalar
`model`/`thinking` overrides — are refused, not ignored. See
[Task spawn](task-spawn.md).

A preview's step rows carry `declared_role` and a `binding` object — the source
profile, its source, the effective role, its source, and the full role map —
identical to what acceptance stores for that consumer, or `null` for a step
with no model. A task's `execution_inputs` carries `step_bindings` in that same
shape, plus a `workflow_binding` naming the pinned `revision`, how it was bound
(`accepted` or `legacy-confirmed`), and — for a confirmed legacy task — the
`legacy_through_seq` boundary and any `interrupted_legacy_seq` that spans it.

Every task payload also carries `workflow_revision`, `workflow_ready`, a
`workflow_readiness_reason` when it is not, and the `workflow_primary_session`
and `workflow_sessions` *that task's* definition declares. The two session
fields are `null` rather than a guess whenever the definition cannot be
resolved.

### Continuation for tasks the upgrade left blocked

`GET /api/tasks/{id}/configuration` reports `needs_configuration`,
`needs_workflow_confirmation`, a classified `workflow_readiness`, and a
`workflow_candidate`: the current definition of *that task's own* workflow
name, its revision, whether it is `compatible` with the steps and sessions
already on record, the `problems` if not, the `legacy_through_seq` boundary,
and the `uncertainty_notice` describing how waiting behavior changes.

`preview` and `confirm` take the launch fields only when the task never had
any; a task that merely predates retained definitions supplies none of them.
`confirm` requires `acknowledge_workflow`, and `acknowledge_unknown` as well
when launch inputs are also missing. A stale `preview_token` is `409` before
anything else is checked; an incompatible candidate is `422` naming the
problems. Confirmation pins the future only — it starts nothing.

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
| `POST` | `/api/tasks/{id}/workflow/resume` | Advance a waiting run: answer a gate with one of its declared choices, resume a gate that offers none, or retry a paused step |
| `POST` | `/api/tasks/{id}/review` | Open a review by hand. Refused when the task's workflow declares its own review step and the run is not at it |
| `POST` | `/api/tasks/{id}/review/cancel` | Cancel an open review |
| `GET` | `/api/tasks/{id}/ship` | The task's current delivery projection — the same document the snapshot and every command response carry |
| `POST` | `/api/tasks/{id}/ship/draft` | Ensure one agent draft, or explicitly replace it with `{"replace": true}`. Only for a workflow that declares no publication of its own; otherwise refused. A new or replacement request requires a live, `idle` primary agent; an ordinary repeated request returns the observed delivery projection without a second agent turn. |
| `PUT` | `/api/tasks/{id}/ship/draft` | Store operator-written publication text |
| `POST` | `/api/tasks/{id}/ship/preview` | Resolve the delivery this run permits, read-only. Authorizes nothing |
| `POST` | `/api/tasks/{id}/ship/commit` | Confirm one authorization — the decision, its grant, and the run's next step, together |
| `POST` | `/api/tasks/{id}/ship/push` | Continue a pre-upgrade grant at its push action |
| `POST` | `/api/tasks/{id}/ship/pr` | Continue a pre-upgrade grant at its pull-request action |
| `POST` | `/api/tasks/{id}/ship/reconcile` | Record one decision about an unresolved delivery effect |

`workflow/resume` takes a required `expected_seq` — the waiting attempt's
sequence number — plus an optional `choice_id` and `note`. **The daemon decides
which kind of wait this is from what the run is actually waiting on, never from
what the caller sent**, and there are three:

| The run is waiting at | `choice_id` | What happens |
|---|---|---|
| A gate with declared choices (format 2 onwards) | required | The named choice is recorded and its declared destination taken; `note` is that choice's feedback |
| A gate without them (format 1) | refused | `note` becomes the gate's outcome and the run continues at its fall-through |
| An uncertainty pause | refused | A new attempt of the blocked step opens; the run never continues past it |

An answer that **authorizes publication** additionally requires `request_id`
and `preview_token` from a delivery preview of that same question and answer,
plus the final publication fields. Without them the answer is refused with
`422`: a generic resume carries no evidence that the operator saw the content,
the destination, and the identities the authorization is about. Such an answer
is the same operation `ship/commit` performs, and either entry point may make
it.

Refusals separate what the operator can fix from what they cannot:

| Code | When |
|---|---|
| `404` | Unknown task |
| `422` | `choice_id` missing at a gate that needs one, or supplied where none is accepted; a choice this gate does not offer; a required reason left blank; feedback over 16 KiB; an unknown request field. The body names the offending `field`. |
| `409` | The run is not waiting, `expected_seq` names an attempt it has moved on from, or the gate has already been answered |

A choice answer is committed — the decision, the gate's completion, and either
the successor attempt or the run's named ending — *before* the response
returns, so an accepted answer is durable and a repeated or stale one advances
nothing. Answering a gate never starts review, signs, pushes, or opens a pull
request.

### Delivering

Delivery is preview-then-confirm, and every response is the task's whole
versioned delivery projection.

`ship/preview` names the *decision*: `gate_seq` and `choice_id` when the run is
waiting at an approval, plus the publication fields and a client-stable
`request_id`. The `ending` and `mode` are derived from the chain that answer
authorizes; a caller may still supply them, and a disagreement is reported
rather than obeyed. It returns which question, answer, and review attempt it
describes, the candidate and review identity, the completed and remaining
actions, the safe destination and identities, the exact pull-request body
Ompire would write, every `blockers` entry, and a `preview_token`. It captures
nothing and authorizes nothing.

`ship/commit` requires `preview_token`, `request_id`, the final fields, and —
for an approval — `gate_seq` and `choice_id`, with an optional `note` recorded
as the decision's feedback. There is no tokenless path. When the run is waiting
at an approval, this one call commits the answer, the authorization it
produces, and the run's move to its first action together; the run then
performs the actions the answer authorized.

`ship/push` and `ship/pr` exist for a delivery authorized before workflows
owned publication. They continue that grant's own prefix and never start an
implicit earlier action or extend it. For a workflow-authorized chain the run
performs its own actions, and an interrupted one is continued through
`ship/commit` against the same delivery.

`ship/reconcile` takes `delivery_id`, `action_id`, `expected_version`, a
`decision` of `recheck`, `adopt`, `retry`, or `abandon`, and an optional `note`.
None of them writes anything privileged.

| Code | When |
|---|---|
| `404` | Unknown task |
| `422` | A preview that does not name the question the run is waiting at, names a stale attempt, an unknown answer, or one that authorizes nothing; an ending or mode that disagrees with the run's declared chain; an unknown reconciliation decision |
| `409` | The preview token no longer matches, the delivery moved on, a request identifier was reused with different inputs, an action is refused, or an adoption or retry cannot be justified by what Ompire can observe |

A refused confirmation returns `{"detail":{"message":...}}`, and a blocked one
adds `blockers` — every reason at once, so they are fixed together rather than
one attempt at a time. Neither creates a delivery record or touches Git.

`ship/draft` returns `404` for an unknown task and `409` for a workflow that
declares its own publication text, an unavailable or non-idle primary agent, an
archived task, a draft already in flight, or a delivery that is already
authorized. See [Ship flow](ship-flow.md) for the
delivery contract, the blocker vocabulary, and recovery.

The same admission runs for the UI, the authenticated API, and a direct service
call: this layer parses and authenticates, and every review, target, mode,
credential, exclusivity, and replay check happens inside the delivery service.

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
