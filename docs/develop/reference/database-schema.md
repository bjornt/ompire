# Database schema

One SQLite database in WAL mode, owner-private, under the daemon's data
directory. Accessed through SQLAlchemy Core — not an ORM, so queries and
schema behavior stay explicit. Migrations are Alembic, reviewed, and applied
automatically at startup.

The rationale is in
[ADR-0005](../../adr/0005-persist-local-state-with-sqlite-core-and-alembic.md).

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
(`registry.model_profiles.reserved_write`) shared by both sides of the race:
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
| `pr_url`, `pr_state`, `pr_merged_at` | string, nullable | Publishing state |
| `spawn_completed_at` | string, nullable | |
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
| `prompted_at`, `started_at`, `finished_at` | string | ISO-8601 |

Steps are recorded repeatedly rather than mutated, so a retried step leaves
both attempts in the history. In-memory runners re-drive workflow state from
these records after a restart.

Two kinds of `waiting` live here and must not be confused. A **declared gate**
carries its operator message in `outcome_json`; resuming finishes it `ok` and
the run continues at its fall-through. An **uncertainty pause** sets
`pause_json` instead and keeps the attempt's own kind, its absent outcome, and
the parse or evaluation error that stopped it — nothing is written that could
later read as a result. The pause document names the reason, the blocked step,
and the step a retry re-enters.

A record and the task's run status are marked waiting in one transaction, so a
restart cannot find one without the other. An operator retry is likewise one
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
long as the task is inspectable — including after the packaged definition
changes and after the name leaves a later release's catalog.

The whole document is stored, not a summary: an identifier alone would name a
definition nobody could still read. A row is decoded, re-validated, and
re-hashed back to the key it is filed under before it is executed; reads are
cached by revision and never by workflow name. Migration `0015` creates the
table empty — the daemon fills it from its packaged definitions at startup.

## `reviews`

| Column | Type | Notes |
|---|---|---|
| `task_id` | integer | FK to `tasks.id`, primary key |
| `status` | string | `open`, `approved`, `aborted`, `error` |
| `process_started_at` | string, nullable | Write-ahead marker; ISO-8601 |
| `created_at`, `updated_at` | string | ISO-8601 |

One row per task, upserted on every start: re-review after comments reopens
the same review so the loop stays one ordered history.

`process_started_at` is stamped before llmvet is launched and cleared when the
process is observed exiting. It is not a display field — it is what lets
startup tell an interrupted reviewer from a review that is `open` only because
its comments went back to the agent. See
[Crash recovery](crash-recovery.md#review-and-ship-recovery).

The reviewer's URL and port are deliberately **not** columns. They describe a
process that cannot outlive the daemon, and a restored review must not offer a
dead link.

## `review_iterations`

| Column | Type | Notes |
|---|---|---|
| `task_id` | integer | FK, part of the primary key |
| `seq` | integer | Part of the primary key |
| `outcome` | string | `approved`, `comments`, `aborted`, `error`, `interrupted` |
| `comment_count` | integer, nullable | Cosmetic; the comment text is authoritative |
| `stderr` | text, nullable | Captured reviewer stderr |
| `recorded_at` | string | ISO-8601 |

Ordered `(task_id, seq)` like `workflow_steps`, because re-review revisits the
same review. `interrupted` is iteration-only and always accompanies an
`aborted` review.

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

## What is not durable

Session status, attention state, the live reviewer process (its URL and port),
and most ship progress are in-memory. Review status and iteration history are
durable, realizing the review slice of
[ADR-0016](../../adr/0016-persist-authority-bearing-task-history-and-provenance.md).

The durable boundary is still narrower than [`VISION.md`](../../VISION.md)
calls for: human decisions, publishing-operation intent records, and commit
lineage remain transient, so ADR-0016 stays proposed.

## Migrations

```sh
cd daemon
uv run alembic revision -m "add something"
uv run alembic upgrade head
```

Migrations run automatically at daemon startup, so a reviewed migration is all
that a schema change needs. Review them properly — they run on operator data
without a prompt.
