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

Inert upgrade history from the template retirement. Every old template row,
every task's template attribution, and any explicitly configured `judge_model`
is copied here — nulls, empty strings and timestamps included — before the live
storage goes away.

| Column | Type | Notes |
|---|---|---|
| `id` | integer | Primary key, autoincrement |
| `kind` | string | `template`, `task-template`, `workspace-conflict`, `model-candidates`, `new-defaults`, `retired-judge-model` |
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

`execution_inputs_json` carries the four role bindings, each agent step's
abstract role, the judge binding, the effective workspace values with their
inheritance attribution, the rendered branch, and the project-derived checkout
path, fetch remote, and upstream/fork routing. Everything downstream reads it;
nothing re-resolves. Editing a project or a profile — or deleting a profile
nothing references — changes the next launch and not this task
([ADR-0026](../../adr/0026-resolve-launch-inputs-once-and-pin-them-to-the-task.md)).

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

Sessions are addressed as `(task_id, name)`. This table holds identity only —
live status is in-memory and does not survive a restart.

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
| `prompted_at`, `started_at`, `finished_at` | string | ISO-8601 |

Steps are recorded repeatedly rather than mutated, so a retried step leaves
both attempts in the history. In-memory runners re-drive workflow state from
these records after a restart.

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
