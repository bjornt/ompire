# Database schema

One SQLite database in WAL mode, owner-private, under the daemon's data
directory. Accessed through SQLAlchemy Core — not an ORM, so queries and
schema behavior stay explicit. Migrations are Alembic, reviewed, and applied
automatically at startup.

The rationale is in
[ADR-0005](../../adr/0005-persist-local-state-with-sqlite-core-and-alembic.md).


## Column ownership

The schema is one database shared by every owner; the *writer* of each
column is what ownership means today. No schema or migration change is
pending on this split — it records who writes what, so a later extraction
has a boundary to preserve:

| Owner | What it writes |
|---|---|
| Work (`work/projects.py`, `work/profiles.py`, `work/tasks.py`, `work/reconciliation.py`) | `projects.*`; `model_profiles.*`; on `tasks`: identity (`id`, `project_name`, `slug`, `branch`, `clone_path`, `prompt`), `execution_inputs_json`, and lifecycle `state` |
| Workspace lifecycle (`spawn.py`, `workshop.py` — not yet extracted) | `tasks.workshop_id`, `tasks.spawn_completed_at`, `tasks.error` on spawn failure |
| Workflows (`workflows.py`, `registry/workflows.py` — not yet extracted) | the `tasks.workflow_*` run-state fields and the step-record tables |
| Delivery (`ship.py`, `prwatch.py`, `registry/ships.py` — not yet extracted) | `tasks.pr_url`, `tasks.pr_state`, `tasks.pr_merged_at`, plus candidate/review/authorization/action history |
| Sessions and artifacts (not yet extracted) | session identity and applied-policy rows; result, reference, and export records |

The shared `tasks.updated_at` (and the error/timestamp columns) is
attributed to whichever operation updates it; these denormalized fields do
not have independent records yet. See the
[daemon module map](daemon-modules.md#ownership-at-a-glance) for the

## `projects`

| Column | Type | Notes |
|---|---|---|
| `name` | string | Primary key. Slug: lowercase, digits, hyphens. |
| `title` | string | |
| `upstream_url` | string | Pull requests target this |
| `fork_url` | string, nullable | Push target when set |
| `checkout_path` | string | Local checkout Ompire clones from |
| `checkout_mode` | string | `adopted` (the operator's) or `cloned` (created by Ompire) |
| `fetch_remote` | string | Remote to fetch **in that checkout**; default `origin` |
| `setup_state` | string | `ready`, `cloning`, or `failed` |
| `setup_error` | text, nullable | Failing step and git stderr |
| `default_model_profile` | string, nullable | FK to `model_profiles.name`, indexed. NULL means no default |
| `base_branch` | string | Default `main`. A launch default, overridable per task |
| `branch_pattern` | string | Seeded from the daemon setting at registration |
| `workshop_additions` | string | `project` or `global`; default `project` |
| `preamble` | text | Standing prompt preamble; empty means none |
| `launch_config_state` | string | `reconciled` or `needs-reconciliation`; independent of `setup_state` |

The four onboarding columns arrived with migration `0011`
([ADR-0022](../../adr/0022-create-or-adopt-base-checkouts-without-mutating-them.md)).
Rows written before it backfill to `adopted` / `origin` / `ready` / `NULL`;
the migration reaches no filesystem to decide that, because a pre-`0011` row
records only what the operator supplied.

`default_model_profile` arrived with migration `0012`
([ADR-0025](../../adr/0025-store-global-model-profiles-separately-from-launch-policy.md)).
It is purely additive: existing rows backfill to `NULL`, and nothing is
inferred from a template, a credential, omp's own settings, or the name.

The five launch-default columns arrived with migration `0013`
([ADR-0026](../../adr/0026-resolve-launch-inputs-once-and-pin-them-to-the-task.md)),
which retired templates. Where every template of a project agreed on a field,
its value was copied here; where they disagreed, the column keeps its default
and `launch_config_state` becomes `needs-reconciliation` until the operator
decides. `branch_pattern` for a project that had no templates is seeded from
the daemon's `default_branch_pattern` at startup, recorded as a decision so it
happens once.

## `model_profiles`

| Column | Type | Notes |
|---|---|---|
| `name` | string | Primary key. Slug, immutable — there is no rename path |
| `roles_json` | text | JSON object: exactly `default`, `smol`, `slow`, `plan`, each `{model, thinking}` |
| `created_at`, `updated_at` | string | ISO-8601 |

The role map is stored as one JSON document rather than eight fixed columns.
It is small, always written whole (there is no role-level update API), and
nothing queries profiles by a nested model — so it follows the same JSON-text
convention as `workflow_steps.outcome_json`. It is decoded into a typed
`ModelProfile.roles` mapping at the registry boundary; JSON text never reaches
an API caller.

### Reference safety without global FK enforcement

`projects.default_model_profile` declares a named, non-cascading foreign key,
but the runtime connection hook enables WAL and **not** `PRAGMA foreign_keys`.
The declaration is therefore schema metadata and defense for any connection
that does enable it — it is not the runtime guarantee.

The guarantee is a `BEGIN IMMEDIATE` write reservation
(`platform/transactions.reserved_write`) shared by both sides of the race:
project create/update takes it before checking that the referenced profile
exists, and profile deletion takes it before scanning for referencing
projects. Because `BEGIN IMMEDIATE` acquires SQLite's write lock up front, a
read inside the reservation cannot go stale before the matching write commits,
and competing writers serialize at the database rather than behind an
in-process lock that other connections would miss. Each mutation also reads
its committed row back inside the reservation, so a later unrelated write
cannot change the response a caller already received.

A plain `engine.begin()` is insufficient: pysqlite defers `BEGIN` until the
first DML statement, so a preflight `SELECT` would run outside the
reservation.

Turning FK enforcement on globally was deliberately not done here — it would
change enforcement for every existing table at once and can surface unrelated
legacy inconsistencies.

## `launch_migration_evidence`

Inert upgrade history from the template retirement and, since migration `0015`,
from the judge's removal. Every old template row, every task's template
attribution, any explicitly configured `judge_model`, each task's whole
pre-upgrade execution-inputs document, and each retired auxiliary binding is
copied here — nulls, empty strings and timestamps included — before the live
storage goes away.

| Column | Type | Notes |
|---|---|---|
| `id` | integer | Primary key, autoincrement |
| `kind` | string | `template`, `task-template`, `workspace-conflict`, `model-candidates`, `new-defaults`, `retired-judge-model`, `legacy-execution-inputs`, `retired-auxiliary-binding` |
| `scope_kind` | string | `project`, `task`, or `daemon` |
| `scope` | string | Project name, task id, or empty |
| `source` | string | Template name, or `config.toml` |
| `payload_json` | text | The original values, verbatim |
| `recorded_at` | string | ISO-8601 |

It is read to show the operator what used to be configured and to notice a
changed retired setting. It is never read to execute anything: there is no CRUD,
no launch selector, and no path from a row here to a running agent. That is why
it can be kept indefinitely without becoming a second source of launch policy.

## `launch_reconciliations`

Operator decisions that closed out a reconciliation.

| Column | Type | Notes |
|---|---|---|
| `scope_kind`, `scope`, `kind` | string | Composite primary key |
| `acknowledged_value` | text, nullable | The exact value acknowledged, where one applies |
| `decided_at` | string | ISO-8601 |

`acknowledged_value` is what lets an *unchanged* retired `judge_model` stay
quiet across restarts while a *changed* one reopens as new evidence.

## `tasks`

| Column | Type | Notes |
|---|---|---|
| `id` | integer | Primary key, autoincrement |
| `project_name` | string | FK to `projects.name` |
| `execution_inputs_json` | text, nullable | The launch decision this task was accepted under, as one version-tagged JSON document. NULL for a task created before pinned inputs |
| `slug` | string | |
| `branch` | string | |
| `clone_path` | string | |
| `state` | string | `created`, `failed`, `archived` |
| `prompt` | text | |
| `error` | text, nullable | Set when a spawn step fails |
| `workshop_id` | string, nullable | |
| `workflow_name` | string | Default `single-step` |
| `workflow_status` | string, nullable | |
| `workflow_step` | string, nullable | |
| `workflow_result` | string, nullable | The declared ending a finished format-2 run reached |
| `pr_url`, `pr_state`, `pr_merged_at` | string, nullable | Publishing state |
| `spawn_completed_at` | string, nullable | |
| `results_version` | integer | Monotonic version over this task's result projection; `0` for a task with no results |
| `created_at`, `updated_at` | string | ISO-8601 |

`execution_inputs_json` carries the effective workspace values with their
inheritance attribution, the rendered branch, the project-derived checkout
path, fetch remote and upstream/fork routing, and — since version 2 — one
complete binding per model consumer. Everything downstream reads it; nothing
re-resolves. Editing a project or a profile, or deleting a profile nothing
references, changes the next launch and not this task
([ADR-0026](../../adr/0026-resolve-launch-inputs-once-and-pin-them-to-the-task.md),
[ADR-0027](../../adr/0027-hand-off-model-policy-between-turns.md)).

`step_bindings` is keyed by declared agent-step name, and since version 3 that
is the whole set: the engine reserves no model consumer. Each entry holds the
source profile name, that profile's source (`step`, `task`, `project`, or
`legacy-confirmed`), the effective role, the role's source (`step` or
`workflow`), and the full four-role snapshot the profile bound. Runtime lookup
is exact and fails closed: a consumer with no entry is an error, never a fall
back to the task-wide profile. `model_profile_name` remains as the task-wide
decision unoverridden consumers inherited, not as an executable fallback.

`workflow_binding` — version 3 — names the definition revision this task
executes, whether it was `accepted` or `legacy-confirmed`, and, for a confirmed
legacy task, the `legacy_through_seq` boundary before which history ran under a
definition nobody retained plus any `interrupted_legacy_seq` that spans it. It
is **NULL** for every task that predates retained revisions, and that null is
load-bearing: see below
([ADR-0028](../../adr/0028-retain-declarative-workflow-revisions.md)).

Migration `0014` converted version-1 documents — which carried one `roles` map,
a `step_roles` map, and a `judge_role` — into version 2, using only what those
documents already stored. It re-read no profile and consulted no current
workflow definition, and it attributed every binding to the workflow, because
version 1 had no way to express a per-step choice.

Migration `0015` converted version 2 into version 3: it copied each document
verbatim, and each `auxiliary_bindings` entry separately, into
`launch_migration_evidence`, dropped the auxiliary map, and set
`workflow_binding` to NULL. It could not do otherwise — version 2 recorded a
workflow *name*, and what that name's prompts and routes said at the time is
gone, so filling the binding in from whatever ships today would claim the task
accepted a document it never saw. It retained no revision, because a migration
cannot know what the code it is upgrading from actually executed.

`result_attachments`, `source_commit`, `base_comparisons`, and
`acknowledged_base_difference` — version 4 — record the accepted result
revisions this task was launched with, the exact commit its clone was built
from, and how each attachment's recorded capture-time base compared with that
commit
([ADR-0035](../../adr/0035-refuse-to-publish-handoff-destinations.md)). Every
attachment carries a fixed `handoff-input` classification; a document holding
any other value is refused on read rather than decoded as "no protection",
because reading an unparseable policy permissively is the one failure mode the
non-publication contract cannot survive. The attachments' destinations are also
the task's protected paths, which is what candidate capture and delivery check
the proposed Git result against.

Migration `0021` converted version 3 into version 4 by adding an empty
attachment list, a null `source_commit`, no comparisons, and a false
acknowledgement. It invented no commit observation, and left every NULL
document NULL.

It follows the registry's existing JSON-text convention rather than a dozen
columns: nothing queries a task by a nested binding, and a partial update would
be a different decision, so there is no field-level write API. The source
profile name inside it is provenance, not a live foreign key.

NULL is a real state, not a value to fill in. A task written before migration
`0013` has no recoverable model, thinking level, preamble, or overrides — the
original spawn's overrides were never persisted — so it keeps its records, is
marked as needing confirmation, and everything that would need those values
refuses until the operator confirms a continuation configuration. The
confirmation writes the same document with `legacy-confirmed` provenance,
once.

## `sessions`

| Column | Type | Notes |
|---|---|---|
| `task_id` | integer | FK, part of the primary key |
| `name` | string | Part of the primary key |
| `omp_session_id` | string, nullable | The agent's own session identity |
| `spawned_at` | string | ISO-8601 |
| `applied_policy_json` | text, nullable | The complete model policy this session's child last verifiably ran |

Sessions are addressed as `(task_id, name)`. Live status is in-memory and does
not survive a restart.

`applied_policy_json` is mutable *execution state*, deliberately a different
kind of fact from the task's immutable inputs: the task says what each consumer
may run, the session says what actually took effect. It cannot be recomputed
from the task document, because two steps sharing a session can pin different
bindings and only the session knows which one applied. It holds the four-role
policy, the source profile and role, the consumer that applied it, and whether
it is `verified` or `migrated` — the latter meaning an upgrade derived a
continuation policy rather than a step having applied one, which makes no claim
about turns already taken.

It is written only after the native state was verified and always before the
turn that depends on it, so a crash between them can only lose the prompt, not
the knowledge of how the process was configured. Recovery resumes each session
on this record; a session that has none is left unresumed with the reason
stated ([ADR-0027](../../adr/0027-hand-off-model-policy-between-turns.md)).
Migration `0014` added the column and backfilled it for resumable sessions of
version-1 tasks from those tasks' own pinned map.

## `workflow_steps`

| Column | Type | Notes |
|---|---|---|
| `task_id` | integer | FK, part of the primary key |
| `seq` | integer | Part of the primary key |
| `step` | string | Step name |
| `kind` | string | `agent`, `command`, `decision`, `gate` |
| `session` | string, nullable | For agent steps |
| `status` | string | |
| `outcome_json` | text, nullable | Structured step outcome |
| `error` | text, nullable | |
| `pause_json` | text, nullable | An uncertainty pause, while this attempt is the one being waited on |
| `evidence_json` | text, nullable | The prior attempts this one bound when it opened |
| `prompted_at`, `started_at`, `finished_at` | string | ISO-8601 |

Steps are recorded repeatedly rather than mutated, so a retried step leaves
both attempts in the history. In-memory runners re-drive workflow state from
these records after a restart.

Two kinds of `waiting` live here and must not be confused. A **declared gate**
carries its question in `outcome_json`; an **uncertainty pause** sets
`pause_json` instead and keeps the attempt's own kind, its absent outcome, and
the parse or evaluation error that stopped it — nothing is written that could
later read as a result. The pause document names the reason, the blocked step,
and the step a retry re-enters.

A format-1 gate's `outcome_json` is just its message, and resuming adds the
operator's note. A format-2 gate stores a versioned **snapshot** — the rendered
message, the choices it offered with their destinations, and the evidence
identities it is asking about — written *before* anyone can answer. Answering
adds a `decision` (choice id, label as shown, exact feedback, resolved
destination, server timestamp, actor `operator`) *beside* that snapshot, never
over it: an answer is meaningless without the question it answered, and the
definition may have changed since.

`evidence_json` is `{version, bindings: {alias: {step, seq} | null}}` —
what this attempt froze at entry, with null for an optional selector that
matched nothing. NULL on the column means the attempt recorded none: a format-1
attempt, or a step declaring no evidence. That is different from binding
nothing, and the difference is why the column is nullable rather than defaulted
to an empty map. Likewise `tasks.workflow_result` is NULL for a run still going
and for every format-1 run — those have no name for their ending, and none is
invented for them.

A record and the task's run status are marked waiting in one transaction, so a
restart cannot find one without the other. Answering a format-2 gate is one
transaction too: the decision, the gate attempt's completion, and either the
successor attempt with its own frozen bindings or the run's terminal status and
named result all commit together, and the run is only notified afterwards. A
crash before that leaves the same unanswered question; a crash after it leaves
the successor the answer already opened, so a decision is never lost and never
applied twice. An operator retry is likewise one
transaction: the paused attempt is finished `failed` with its reason retained,
a new attempt of the same step is opened `running`, and the current-step
pointer moves — execution is scheduled only after that commit, so a crash in
between leaves an ordinary interrupted attempt rather than a lost
authorization.

## `workflow_revisions`

Retained workflow definitions, keyed by the content identity of their canonical
document ([ADR-0028](../../adr/0028-retain-declarative-workflow-revisions.md)).

| Column | Type | Notes |
|---|---|---|
| `revision` | string | Primary key: `sha256:<digest>` of the canonical bytes |
| `workflow_name` | string | Indexed; several revisions share a name |
| `format` | integer | The document format *and* its interpretation |
| `document_json` | text | The whole normalized document |
| `created_at` | string | ISO-8601 |

Append-only: there is no update, no delete, and no garbage collection. A task
points at a revision, and that revision must keep meaning what it meant for as
long as the task is inspectable — including after the library's current
selection for that name moves on, after the entry is archived, and after the
name leaves a later release's packaged set.

The whole document is stored, not a summary: an identifier alone would name a
definition nobody could still read. A row is decoded, re-validated, and
re-hashed back to the key it is filed under before it is executed; reads are
cached by revision and never by workflow name. Migration `0015` creates the
table empty — the daemon fills it from its packaged definitions at startup.

Migration `0016` adds `workflow_step_records.evidence_json` and
`tasks.workflow_result`, both nullable and both left NULL on every existing
row. NULL means *not recorded*, which is the truthful value: no pre-upgrade
attempt froze a binding and no pre-upgrade run declared an ending. Backfilling
an empty binding map, or a terminal result inferred from a `complete` status,
would manufacture history.

## `workflow_library`

The mutable selection over those append-only revisions
([ADR-0031](../../adr/0031-let-operators-own-a-workflow-library-above-retained-revisions.md)):
one row per workflow name, holding what an operator owns.

| Column | Type | Notes |
|---|---|---|
| `name` | string | Primary key. Permanent — renaming means a separate entry |
| `origin` | string | `builtin` (packaged, read-only) or `custom` |
| `draft_yaml` | text | The editor's raw text, exactly as submitted. NULL for a built-in, whose text is in the package |
| `current_revision` | string | FK to `workflow_revisions.revision`; NULL for a draft-only entry |
| `archived` | integer | 0/1. Out of launch choices, deleting nothing |
| `version` | integer | The **edit** version. Advances on every successful mutation |
| `created_at`, `updated_at` | string | ISO-8601 |

`draft_yaml` is inert. Any UTF-8 within the 1 MiB document limit is stored
verbatim — empty, invalid, or unsafe-looking alike — and nothing in the daemon
parses it. Startup never reads it, so a broken draft cannot keep the daemon from
starting or take a launchable workflow away.

`version` is deliberately not `current_revision`. A revision is a digest of
normalized semantics, so a comment-only edit does not move it; every mutation of
an existing entry compares this counter instead, inside the write reservation
that performs the write. As elsewhere in this schema the FK is metadata only —
the runtime guarantee is that reservation, because `PRAGMA foreign_keys` is not
enabled (see [Reference safety](#reference-safety-without-global-fk-enforcement)).

An executable save inserts the retained revision and updates
`current_revision`, `draft_yaml`, and `version` in **one** transaction, so a
restart cannot expose a retained document nothing selected or a selection
pointing at a document that was never written. Nothing here deletes a revision
row; archive and restore only flip `archived`.

Built-in rows are synchronized at startup from the definitions the running
package ships. A name the package stops shipping keeps its row and its history
with `current_revision` set to NULL — unlaunchable, not deleted. A packaged name
an operator's custom entry already owns is reported and skipped: the custom row
is left untouched, and the daemon still starts.

Migration `0017` creates the table empty. It copies nothing out of
`workflow_revisions` and invents no built-in row: a migration cannot know what
the *next* start will ship, and guessing would file a built-in under a revision
this package never contained.

## `reviews`

| Column | Type | Notes |
|---|---|---|
| `task_id` | integer | FK to `tasks.id`, primary key |
| `status` | string | `open`, `approved`, `aborted`, `error` |
| `process_started_at` | string, nullable | Write-ahead marker; ISO-8601 |
| `candidate_id` | string, nullable | The protected candidate this round is grading |
| `workflow_seq` | integer, nullable | The workflow attempt this round was launched for |
| `created_at`, `updated_at` | string | ISO-8601 |

One row per task, upserted on every start: re-review after comments reopens
the same review so the loop stays one ordered history.

`process_started_at` is stamped before llmvet is launched and cleared when the
process is observed exiting. It is not a display field — it is what lets
startup tell an interrupted reviewer from a review that is `open` only because
its comments went back to the agent. See
[Crash recovery](crash-recovery.md#review-recovery).

The reviewer's URL and port are deliberately **not** columns. They describe a
process that cannot outlive the daemon, and a restored review must not offer a
dead link.

`candidate_id` binds the review to the content it is grading
([ADR-0032](../../adr/0032-bind-trusted-delivery-to-retained-candidates.md)).
It is nullable because that is the whole upgrade story: a review recorded before
content binding keeps a `NULL` and stays readable, and nothing infers which tree
it graded. Such an approval is history, not authorization.

`workflow_seq` is written *with* the process marker, before llmvet starts, so a
restart can resolve an interrupted reviewer against the step still waiting on
it rather than against wherever the run has moved to. `NULL` is an
operator-started review, which is a different fact rather than a missing one
([ADR-0033](../../adr/0033-scope-trusted-delivery-authority-to-the-workflow-run.md)).

## `review_iterations`

| Column | Type | Notes |
|---|---|---|
| `task_id` | integer | FK, part of the primary key |
| `seq` | integer | Part of the primary key |
| `outcome` | string | `approved`, `comments`, `aborted`, `error`, `interrupted` |
| `comment_count` | integer, nullable | Cosmetic; the comment text is authoritative |
| `stderr` | text, nullable | Captured reviewer stderr |
| `candidate_id` | string, nullable | The candidate this iteration graded |
| `workflow_seq` | integer, nullable | The workflow attempt that asked for this review |
| `findings` | text, nullable | The reviewer's own report |
| `findings_state` | string, nullable | `complete`, `empty`, `truncated`, `unavailable` |
| `recorded_at` | string | ISO-8601 |

Ordered `(task_id, seq)` like `workflow_steps`, because re-review revisits the
same review. `interrupted` is iteration-only and always accompanies an
`aborted` review.

The terminal `approved` iteration's `candidate_id` is what delivery reads: it
says what an approval covers, so "is this still the reviewed content?" is a
comparison rather than an assumption.

`findings` is the reviewer's report, retained whole rather than counted, and
`findings_state` says what was kept. The two travel together on purpose: a
correction that runs automatically should require `complete`, because a
truncated capture handed to an agent as though it were the reviewer's whole
opinion is worse than no correction at all. `comment_count` stays cosmetic.

## `delivery_candidates`

| Column | Type | Notes |
|---|---|---|
| `candidate_id` | string | Primary key: SHA-256 over the normalized content below |
| `task_id` | integer | FK to `tasks.id`, indexed |
| `base_branch` | string | The base the task was accepted with |
| `base_commit` | string | Merge-base the delta is measured from |
| `original_head` | string | The HEAD the capture was taken at |
| `tree_id` | string | The full publishable candidate tree |
| `source_commits_json` | text | Ordered `{commit_id, tree_id, message, parent_ids}` for retain |
| `dirty` | integer | Whether the workspace had uncommitted publishable changes |
| `storage_path` | string, nullable | The owner-private bare repository holding its objects |
| `created_at` | string | ISO-8601 |

The identity is a hash of exactly the fields above — not the rendered preview,
not the agent's draft, not a timestamp. An unchanged workspace captures to the
same identity, and any change to what would be published captures to a different
one.

`storage_path` names a bare repository under the daemon's data directory, not
the workshop mount, holding only the base, the original head, and a commit that
makes the candidate tree reachable. It is temporary operation evidence with its
own lifecycle: it is removed once no active or unresolved delivery still needs
it, and `storage_path` becomes `NULL`. The manifest row stays as the record of
what was reviewed and signed.

## `deliveries`

| Column | Type | Notes |
|---|---|---|
| `id` | integer | Primary key |
| `task_id` | integer | FK to `tasks.id`, indexed |
| `version` | integer | Per-task monotonic projection version |
| `workflow_revision` | string, nullable | The pinned procedure this task was accepted under (ADR-0028); attribution, never policy |
| `candidate_id`, `review_candidate_id` | string, nullable | What is delivered, and what the approval named |
| `mode` | string, nullable | `squash` or `retain` |
| `ending` | string, nullable | `commit`, `push`, or `pr` |
| `commit_message`, `pr_title`, `pr_body` | text, nullable | The immutable final metadata |
| `routing_json` | text, nullable | The accepted destination |
| `identity_json` | text, nullable | Safe identity observations only |
| `authorized_at`, `authorized_by` | string, nullable | `operator` for the authenticated single-operator command |
| `request_key`, `input_fingerprint` | string, nullable | Replay identity; unique per task where set |
| `draft_json` | text, nullable | Durable publication draft and its state |
| `workflow_gate_seq` | integer, nullable | The answered gate attempt that granted this |
| `workflow_choice_id` | string, nullable | The answer that granted it |
| `review_seq` | integer, nullable | The review attempt the grant is bound to |
| `disposition` | string | `open`, `authorized`, `completed`, `blocked`, `unresolved`, `abandoned` |
| `blocked_reason` | text, nullable | Why it stopped |
| `created_at`, `updated_at` | string | ISO-8601 |

A task accumulates deliveries, but at most one is non-terminal at a time —
reserved transactionally, so "this task is already delivering" is a durable fact
rather than an in-memory flag.

`version` advances on every committed change and is what a client compares: the
projection is published whole, and an older or duplicated version is dropped
rather than applied.

`identity_json` records only safe facts — the Git author and committer, the
selected signer, and the observed GitHub host, login and credential-source
label. Never a credential value. The ambient Git transport principal is recorded
as explicitly unattributed rather than invented.

The three workflow columns say *which decision* granted the authorization. They
are written in the same transaction as the gate answer and the run's move to
its first action, so a crash cannot separate an approval from the grant it
produced. All three are `NULL` for an authorization made outside a workflow
decision, and a `NULL` there never means "granted by the workflow" — which is
why `delivery_authority_boundary` exists.

## `delivery_authority_boundary`

| Column | Type | Notes |
|---|---|---|
| `id` | integer | Primary key; always `1` |
| `max_delivery_id` | integer | The highest `deliveries.id` that existed before the upgrade |
| `max_action_id` | integer | The highest `delivery_actions.id` that existed before it |
| `recorded_at` | string | ISO-8601 |

Exactly one row, written by migration `0019` and never updated.

A `NULL` workflow link is ambiguous on its own: it is what a genuine
pre-upgrade authorization looks like, and it is also what a freshly inserted row
looks like for the instant before its links are written. Rows at or below this
boundary predate the upgrade and may be *continued* under their original grant;
anything above it must carry its own workflow authority. Nothing can move the
boundary, so no new row can ever look historical.

## `delivery_actions`

| Column | Type | Notes |
|---|---|---|
| `id` | integer | Primary key |
| `delivery_id` | integer | FK to `deliveries.id`, indexed with `seq` |
| `seq`, `kind`, `attempt` | integer/string | Ordering, `commit`/`push`/`pr`, and attempt number |
| `request_key`, `input_fingerprint` | string | Replay identity for this attempt |
| `phase` | string | `prepared`, `executing`, `succeeded`, `failed`, `needs_reconciliation` |
| `expected_json` | text, nullable | What the attempt intends to write, recorded first |
| `progress_json` | text, nullable | Per-signature progress and reconciliation evidence |
| `identity_json` | text, nullable | Safe identity facts for this attempt |
| `result_json` | text, nullable | The verified outcome |
| `error` | text, nullable | Sanitized failure detail |
| `workflow_seq` | integer, nullable | The delivery-step attempt this action belongs to |
| `created_at`, `updated_at` | string | ISO-8601 |

`phase` is the write-ahead marker. `prepared` commits before anything runs;
`executing` commits *before* the effect is launched; `succeeded` and the
eligibility it grants commit together. `failed` is reachable only when
non-execution or a verified rollback was established — everything less certain
becomes `needs_reconciliation`, which is neither success nor failure.

`expected_json` is what makes a lost response answerable: the destination ref and
source object id, the observed pre-push head, the protected ref a signature will
be written under, the correlation marker that will appear in a pull-request body.
Recovery looks for exactly that, instead of guessing from today's state.

`progress_json` records per-signature progress, so an interrupted retain rewrite
is inspectable rather than an opaque boolean.

`workflow_seq` is persisted with the intent, before the effect, and at most one
*live* action may carry it for a delivery — enforced under the same reservation
as the insert, and by a partial unique index that excludes `failed` rows. That
is what makes a re-driven step adopt its own action instead of dispatching a
second one, while still letting an operator continue after an attempt whose
non-execution was proven.

## `delivery_decisions`

| Column | Type | Notes |
|---|---|---|
| `id` | integer | Primary key |
| `delivery_id` | integer | FK to `deliveries.id`, indexed with `id` |
| `action_id` | integer, nullable | The attempt a reconciliation decided about |
| `kind` | string | `authorize`, `extend`, `recheck`, `adopt`, `retry`, `abandon`, `block` |
| `detail_json` | text, nullable | The decision's evidence |
| `note` | text, nullable | The operator's own words |
| `decided_at` | string | ISO-8601 |

Append-only. A delivery whose first action was refused has no terminal prefix to
protect, so a corrected confirmation may replace it — and that, too, is appended
as its own decision.

`extend` belongs to the pre-format-3 policy where an operator could widen a
completed ending. Existing rows keep it and stay readable; a workflow-authorized
run performs the chain its answer named and produces no new ones.

## `task_results`

| Column | Type | Notes |
|---|---|---|
| `id` | string | Primary key. Opaque capture identity (`res_<32 hex>`), independent of content |
| `task_id` | integer | FK to `tasks.id`, indexed with `started_at` |
| `request_id` | string | The caller's replay key; unique per `(task_id, request_id)` |
| `selection_fingerprint` | string | Hash of the normalized selection, so a replay under a *changed* selection is refused |
| `selection_json` | text | The normalized selection itself |
| `state` | string | `capturing`, `failed`, `ready`, `purged` |
| `error` | text, nullable | Why a capture produced no bundle |
| `unavailable_reason` | text, nullable | Classified damage found at the read boundary |
| `manifest_json` | text, nullable | The immutable canonical manifest |
| `manifest_id` | string, nullable | SHA-256 of that whole manifest — the revision binding |
| `content_id` | string, nullable | SHA-256 over the sorted path/media/length/checksum entries only |
| `predecessor_id` | string, nullable | The most recent `ready` revision at admission, frozen then |
| `workflow_seq` | integer, nullable | The format-4 capture attempt that owns this result; NULL for manual capture |
| `workflow_provenance_json` | text, nullable | Pinned workflow and declared producer-attempt context; never client supplied |
| `started_at`, `finished_at` | string, nullable | ISO-8601 |
| `accepted_at`, `accepted_by` | string, nullable | The operator's decision |
| `purged_at`, `purged_by` | string, nullable | The tombstone |

## `task_result_files`

| Column | Type | Notes |
|---|---|---|
| `result_id` | string | FK to `task_results.id`, primary key with the path |
| `relative_path` | string | Repository-relative, re-validated on read |
| `content` | blob | The exact captured bytes |

Added by migration `0020`
([ADR-0034](../../adr/0034-retain-durable-task-results-outside-the-workspace.md)).
Purely additive: no row is created for an existing task, and
`tasks.results_version` backfills to `0`. A task that ran before results existed
produced none, and reconstructing one from its outcome text, its clone, or its
last workflow step would manufacture exactly the provenance this feature exists
to keep honest.

Migration `0023` adds nullable `workflow_seq` and
`workflow_provenance_json`, with a partial unique index on
`(task_id, workflow_seq)`. The capture attempt reserves its identity before the
filesystem read, so recovery either adopts its complete ready result or records
the interrupted operation as failed; it never reads changed workspace bytes
under the same attempt. Existing and later manual captures keep both columns
NULL rather than acquiring guessed workflow attribution.

The file rows carry *only* bytes. Length, checksum and media type live in the
manifest, which is hashed into `manifest_id`, so there is no second
independently mutable description of a file to disagree with the one acceptance
was bound to.

Acceptance is not a state. A `ready` revision that later fails its integrity
check keeps `accepted_at` and gains `unavailable_reason`: the decision really
happened, even though the payload no longer matches it.

### Transaction boundaries

Every mutation runs inside `platform/transactions.reserved_write` (`BEGIN
IMMEDIATE`), and every one of them advances `tasks.results_version` in the same
transaction, so an observable change and its version cannot separate.

- **Capture admission** records the identity, the request key, and the frozen
  predecessor before a byte is read, and publishes `capturing` only once that
  commits.
- **Finalization** inserts every file row, writes the manifest, and sets `ready`
  together. There is no state in which a revision is `ready` with some of its
  files — which is why restart recovery never has to adopt a partial bundle or
  re-read a workspace that may be gone.
- **Acceptance** and **purge** are each their own transaction, comparing the
  expected manifest identity (and, for purge, the expected task result version)
  under the reservation.

No filesystem work and no `await` happens while a write reservation is held: the
whole bounded read runs on a worker thread first, and only the commit is
reserved.

## `task_result_references`

| Column | Type | Notes |
|---|---|---|
| `consumer_task_id` | integer | FK to `tasks.id`, primary key with `result_id` |
| `result_id` | string | FK to `task_results.id`, primary key with the consumer; separately indexed |
| `producer_task_id` | integer | Denormalized, so a purge refusal names the producer without joining through bytes that may be gone |
| `manifest_id` | string | The exact revision this consumer pinned |
| `created_at` | string | ISO-8601 |

Added by migration `0021`
([ADR-0035](../../adr/0035-refuse-to-publish-handoff-destinations.md)), which
also moves `tasks.execution_inputs_json` from version 3 to version 4. That
upgrade invents nothing: an existing document gains an empty attachment list, a
null `source_commit`, no base comparison, and no acknowledgement, and a NULL
document stays NULL. A task built before attachments existed was built from
*some* commit and nobody recorded which; filling it in from today's branch head
would claim a review that never happened.

This table is an index, not the contract. The consumer's immutable
`execution_inputs_json` is what execution reads and what the task's record says
it ran with; this exists so "may these bytes be purged?" and "who is holding
them?" are answerable without decoding every launch document in the database.

Foreign-key enforcement is off on these connections, so both deletions are
written explicitly — there is no cascade to rely on.

### Reference transaction boundaries

- **Admission**: the task row, its pinned inputs, its retained workflow
  revision, and every reference are inserted in the *one* `BEGIN IMMEDIATE`
  reservation that `POST /api/tasks` already held. Every revision's state,
  acceptance, manifest identity and payload checksums are checked on that same
  connection, through connection-scoped helpers that perform no writes and open
  no second connection — so a refusal never nests a reservation, and a purge
  racing the launch either loses the reservation and is refused or wins and
  leaves no consumer behind.
- **Result purge** checks references on its own reserved connection, before any
  byte is deleted.
- **Release** happens only in the transaction that deletes a consumer's task
  row, after every task-purge refusal has passed.

Both admission and release advance the *producer's* `results_version`, and the
REST layer publishes those producers' documents after the commit, so a purge
dialog open in another tab converges without a refresh.

### Logical purge and retention

Result purge deletes the file rows and leaves the `task_results` row as a
tombstone with its identity, manifest, provenance, acceptance and purge
decision. It is logical removal: SQLite may reuse the freed pages later, and the
database file need not shrink. It makes no claim about backups or copies already
downloaded.

Nothing expires. There is no automatic eviction, no garbage collection of
unaccepted revisions, and no disk-pressure collection — retained bytes go when
an operator purges them and not before.

Task purge refuses while any `ready` or `capturing` revision remains. That check
runs on the caller's own reserved connection *before* any deletion, alongside
the delivery-row deletion that used to run in its own earlier transaction — so a
refused purge leaves every child row this task owns exactly where it was.

Result purge additionally refuses while any row in `task_result_references`
names the revision, listing the consumer tasks. Retention there is a
dependency, not a preference: the consumers' own records say they ran with
those files. There is no force variant and no automatic eviction, so a database
with many failed or archived consumers keeps their references until those task
records are purged.

## `result_exports`

| Column | Type | Notes |
|---|---|---|
| `id` | string | Primary key. Opaque operation identity (`exp_<32 hex>`) |
| `task_id` | integer | FK to `tasks.id`, indexed with `confirmed_at` |
| `result_id` | string | FK to `task_results.id`, separately indexed |
| `manifest_id` | string | The exact revision this export delivered |
| `request_id` | string | The caller's replay key; unique per `(task_id, request_id)` |
| `selection_json`, `selection_fingerprint` | text, string | The approved manifest-path subset |
| `prefix` | string | The checkout-relative destination prefix; empty means the revision's own paths |
| `preview_token` | string | SHA-256 of the canonical approval document |
| `preview_json` | text | That document, stored whole |
| `project_name`, `checkout_path` | string | The registration the approval named |
| `root_device`, `root_inode` | integer | The root's filesystem identity, as observed at admission |
| `state` | string | `running`, `completed`, `incomplete`, `unresolved` |
| `error` | text, nullable | Why the operation did not complete |
| `staging_name` | string | Journalled *before* the directory is created |
| `staging_device`, `staging_inode` | integer, nullable | Recorded after creation, so cleanup and recovery can prove ownership |
| `staging_error` | text, nullable | A staging directory left behind in the checkout |
| `created_directories_json` | text, nullable | Directories this export created; effects, so they are recorded |
| `actor`, `confirmed_at` | string | Who approved it and when |
| `started_at`, `finished_at` | string, nullable | ISO-8601 |
| `acknowledged_at`, `acknowledged_by` | string, nullable | An operator closing an unresolved outcome |

## `result_export_files`

| Column | Type | Notes |
|---|---|---|
| `export_id` | string | FK to `result_exports.id`, primary key with the manifest path |
| `manifest_path` | string | The retained file being delivered |
| `seq` | integer | Installation order, indexed with `export_id` |
| `destination` | string | The checkout-relative path, prefix applied |
| `classification` | string | `create` or `identical`, as the approved preview classified it |
| `expected_length`, `expected_sha256` | integer, string | From the accepted manifest |
| `before_json` | text, nullable | The destination observation the approval was bound to |
| `staged_device`, `staged_inode` | integer, nullable | The staged file's identity, written before its rename |
| `outcome` | string | `pending`, `created`, `already-identical`, `not-installed`, `unknown` |
| `observed_json` | text, nullable | What was seen afterwards |
| `error` | text, nullable | Why an outcome is what it is |

Added by migration `0022`
([ADR-0036](../../adr/0036-install-exported-result-files-without-replacing-them.md)).
Purely additive and empty of invention: no historical export is backfilled,
because none happened. A result retained before this migration has no export
history, which is the fact — not an unknown outcome to reconcile.

No bytes live here. The content is already in `task_result_files`, and the
delivered copies are ordinary files in a directory Ompire does not own. There is
no backup of a replaced destination either, because no destination is ever
replaced.

The approval document is stored whole rather than as a token alone. A row that
remembered only a digest could say an export was approved but not *what* was
approved, which is precisely the question an interrupted operation raises.

### The durable root reservation

`uq_result_exports_active_root` is a unique index on `(root_device, root_inode)`
partial to `state IN ('running', 'unresolved')`. Keyed by filesystem identity
rather than project name, so two registrations aliasing one directory cannot
both install into it; durable in SQLite rather than in memory, so a restart or a
second daemon process does not drop it. `registry/result_exports.py` also checks
for an active row inside the admission reservation, which is what produces a
readable refusal naming the blocking export; the index is the guarantee behind
it.

None of this claims the directory cannot be replaced on disk. That is detected
separately, by comparing the reopened root's device and inode against the
approval before anything is written.

### Export transaction boundaries

Every transition runs inside `reserved_write` and advances
`tasks.results_version` in the same transaction, so export history rides the
existing result projection.

- **Admission** observes the filesystem *first*, then in one reservation
  rechecks the project registration, the revision's acceptance, identity and
  retained bytes, reserves the root, and writes the approval plus every intended
  destination. The filesystem observation is revalidated after that commit, and
  before any destination write: a database reservation proves nothing about a
  directory.
- **Replay** is keyed on `(task_id, request_id)` and is checked *before* the
  filesystem is re-observed. A completed export has itself changed the
  destinations a fresh preview would classify, so re-deriving one first would
  answer "did my request go through?" with "the checkout changed".
- **Per-file intent** — the staged file's device and inode — is committed before
  its rename, and the outcome after. That pair is what lets recovery establish a
  rename that beat its own journal entry; matching bytes alone never can.
- **Settlement** is derived from the recorded per-file outcomes, not asserted by
  the caller: any `unknown` forces `unresolved`, and `completed` requires every
  destination to be `created` or `already-identical`.
- **Acknowledgement** moves an `unresolved` operation to `incomplete` under an
  expected-version check and leaves every per-file outcome untouched.

No filesystem work and no `await` happens while a reservation is held.

### Export retention

A `running` or `unresolved` export refuses result purge and task purge, naming
itself, and refuses repointing its project's `checkout_path`. All three holds
release when it settles or is acknowledged closed — deliberately unlike
`task_result_references`, which holds forever: a consumer's record permanently
says what it ran with, while an export's product is a set of files that already
outlive everything retained here.

Result purge keeps the export rows beside the revision's tombstone. Task purge
deletes them explicitly, after every refusal above has passed; foreign keys are
not enforced on these connections, so there is no cascade to rely on. Neither
purge, nor producer cleanup, touches an exported copy in the checkout.

## `settings`

| Column | Type | Notes |
|---|---|---|
| `key` | string | Primary key |
| `value` | text | |

Scalar daemon settings only
([ADR-0013](../../adr/0013-layer-daemon-writable-settings-over-operator-configuration.md)).
Model profiles are deliberately *not* here: a named collection with its own
lifecycle, sort order, references, and deletion guard is a registry entity,
not a setting.

Runtime overrides only, stored as JSON-encoded scalars. Fifteen keys are
recognized: `renotify_interval`, `stall_threshold`,
`context_advisory_threshold`, and twelve attention-tier preferences
(`tier.<interrupt|notify|badge|silent>.<desktop|sound|badge>`).

Resolution is override, then `config.toml`, then the built-in default. Only
the three numeric keys are seedable from `config.toml`; tier preferences are
default-only. The daemon never rewrites your TOML.

An unknown key or a wrong value type is rejected with `422` naming the key.

## Reference safety across the delivery tables

The delivery tables declare named, non-cascading foreign keys for the same
reason `projects.default_model_profile` does, and with the same caveat: the
runtime connection enables WAL and **not** `PRAGMA foreign_keys`.

The guarantees are the same `BEGIN IMMEDIATE` write reservation used elsewhere —
reserving the single non-terminal delivery per task, admitting an action attempt,
and landing a result with the eligibility it grants — plus explicit child
deletion in purge, because a cascade cannot be assumed.

The workflow links added by migration `0019` are checked the same way. Two
transactions now span both registries under one reservation: a gate answer with
the delivery authorization it grants and the run's successor attempt, and a
succeeded action's journal result with the step transition it produces. Neither
pair can be half-written.

## What is not durable

Session status, attention state, and the live reviewer process (its URL and
port) are in-memory. Review status, iteration history and the reviewer's report
are durable, as are delivery authorizations, action attempts, and
reconciliation decisions — and each now names the workflow attempt that asked
for it, realizing the review and delivery slices of
[ADR-0016](../../adr/0016-persist-authority-bearing-task-history-and-provenance.md).

The durable boundary is still narrower than [`VISION.md`](../../VISION.md)
calls for: full commit lineage and transcript retention remain incomplete, so
ADR-0016 stays proposed.

### Retention

Delivery rows survive task archival: a cleaned-up task keeps the record of what
it published and under whose authorization. Only purge deletes them, and purge
returns the candidate storage paths that are now unreferenced so the caller can
remove them from disk — nothing else knows those paths once the rows are gone.

Result rows and their bytes survive task archival too, and survive task purge's
refusal. They are removed only by an explicit result purge; the tombstones that
remain travel with the rest of the task's history when it is eventually purged.

## Migrations

```sh
cd daemon
uv run alembic revision -m "add something"
uv run alembic upgrade head
```

Migrations run automatically at daemon startup, so a reviewed migration is all
that a schema change needs. Review them properly — they run on operator data
without a prompt.
