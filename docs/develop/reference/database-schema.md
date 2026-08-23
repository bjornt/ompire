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

## `templates`

| Column | Type | Notes |
|---|---|---|
| `name` | string | Primary key |
| `project_name` | string | FK to `projects.name` |
| `base_branch` | string | Default `main` |
| `branch_pattern` | string | |
| `workflow` | string | Default `single-step` |
| `workshop_additions` | string | Default `project` |
| `model` | string, nullable | |
| `thinking` | string, nullable | |
| `preamble` | text | Prepended to every prompt |
| `created_at`, `updated_at` | string | ISO-8601 |

## `tasks`

| Column | Type | Notes |
|---|---|---|
| `id` | integer | Primary key, autoincrement |
| `project_name` | string | FK to `projects.name` |
| `template_name` | string, nullable | The template used, if any |
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

Task rows denormalize the project, template, and workflow identity resolved at
spawn time. Editing a template later does not change tasks already spawned
from it — a run's configuration is fixed when it starts.

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

## `settings`

| Column | Type | Notes |
|---|---|---|
| `key` | string | Primary key |
| `value` | text | |

Runtime overrides only, stored as JSON-encoded scalars. Fifteen keys are
recognized: `renotify_interval`, `stall_threshold`,
`context_advisory_threshold`, and twelve attention-tier preferences
(`tier.<interrupt|notify|badge|silent>.<desktop|sound|badge>`).

Resolution is override, then `config.toml`, then the built-in default. Only
the three numeric keys are seedable from `config.toml`; tier preferences are
default-only. The daemon never rewrites your TOML.

An unknown key or a wrong value type is rejected with `422` naming the key.

## What is not durable

Session status, review history, attention state, and most ship progress are
in-memory. The durable boundary is narrower than
[`VISION.md`](../../VISION.md) calls for, and widening it is an open
decision in the ADR migration rather than a settled design.

## Migrations

```sh
cd daemon
uv run alembic revision -m "add something"
uv run alembic upgrade head
```

Migrations run automatically at daemon startup, so a reviewed migration is all
that a schema change needs. Review them properly — they run on operator data
without a prompt.
