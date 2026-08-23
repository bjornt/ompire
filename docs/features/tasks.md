# Tasks

## Overview

A task is one deliverable against a project: a bug fix, a change, an
investigation. It owns an isolated clone, a branch, a container, one or more
workflow runs, its sessions, and its publishing state.

The task is the top-level unit of work. Sessions are resources a task's
workflow uses, not the thing being managed.

Tasks need not produce code or a commit.

## Fields

A task carries the project it belongs to, its slug and derived branch name,
its clone path, its state, the operator's prompt, an error text populated on
failure, the workshop identity read from the clone's lock file after a
successful launch, and created/updated timestamps.

It also denormalizes configuration and outcome:

| Field | Source |
|---|---|
| `template_name` | The template used at spawn; null for tasks predating templates |
| `workflow_name` | Copied from the template at creation |
| `workflow_status` | `running`, `waiting`, `complete`, or `failed`; null before the workspace is ready |
| `workflow_step` | Current step name; null when no run is in flight |
| `pr_url` | Set when the task ships a pull request |
| `pr_state` | `open`, `merged`, or `closed`; null until the first successful poll |
| `pr_merged_at` | Set only when merged |

Session identity is not a task field. Each spawned session records its own
identity on a per-session row, because sessions are spawned lazily by the
workflow.

### Slugs

Lowercase alphanumerics and hyphens, not starting with a hyphen, at most 64
characters.

A project/slug pair must be unique among non-archived tasks. Archiving a task
frees its slug for reuse.

## States and behavior

| State | Meaning |
|---|---|
| `created` | The task exists. Spawning has run or is running. |
| `failed` | A spawn step failed, or startup reconciliation could not resume it. The error text names the cause. |
| `archived` | Cleaned up. Clone and container are gone; the record remains. |

Task state is durable. Live session status travels separately and is never
folded into a task payload — the registry describes what the task *is*, not
what its agent is doing this second.

### Startup reconciliation

On startup the daemon reconciles every non-archived task before serving the
first snapshot, and marks as `failed` any it cannot resume:

- a task whose spawn pipeline never completed, meaning the daemon restarted
  mid-spawn;
- a spawn-completed task whose workshop container no longer exists.

A spawn-completed task whose container is present is handed to recovery
instead, whether or not any session identity has been captured. A task with no
sessions is not an error — sessions are lazy, and a command-only workflow may
never create one.

Tasks already `failed` or `archived` are left untouched.

### Cleanup and purge

Cleanup removes the workshop container first, then deletes the clone
directory, then marks the task `archived`. It refuses to delete any path that
does not resolve inside the configured task root.

Cleanup is idempotent: a task whose container and directory are already gone
still archives successfully.

Purge is separate and deletes an archived task's registry row. Purging a task
that is not archived is refused with `409`.

Both cleanup and purge clear the task's attention entry. Without an explicit
clear, an agent exit racing container removal produces an interrupt-tier
`failed` entry demanding attention for a task that no longer exists.

## Using tasks

### The Tasks view

One card per non-archived task, showing project, branch, state, clone path,
and elapsed time. Archived tasks are excluded from the default list.

The card's status pill shows live session status with attention-tier styling —
`working` animates, `failed` renders interrupt styling, `stalled` renders
amber, `reviewing` renders violet, `retrying` renders a quiet badge style. A
task with no tracked session falls back to spawn-derived presentation.

While a workflow run is in flight the pill is prefixed with the step name:

| Step kind | Pill |
|---|---|
| `agent` | `<step>: <session status>` |
| `command` | `<step>: running` |
| `gate` | `<step>: waiting` |

A completed run renders the bare session status, so a finished `single-step`
task looks exactly as it did before workflows existed. A failed run renders
failed styling with the workflow error accessible from the card, regardless of
the task's registry state.

### Card decorations

- A `stats` event drives a tokens and cost line.
- Context at or above the advisory threshold adds an amber ring with a
  compact/handoff hint.
- A `maybe-waiting` advisory decorates an `idle` card with "may be waiting for
  a reply".
- An open review links to the live review URL.
- A recorded `pr_url` renders a pull-request link.

### Inline quick answers

When a session is `waiting-input` with a pending single-select question that
fits inline — one question, not multi-select — the card renders its options
directly and selecting one answers it.

Anything that does not fit inline — multi-select, multiple questions,
free-text-only, or an approval gate — points the operator to task detail
instead. Resolving a question removes the control.

### Review action

A card offers a Review action when the task's primary session is `idle` with a
live agent and no review is open. While a review is open the card surfaces the
review link and the `reviewing` pill instead.

### The Shipped section

Below the active cards, one collapsed row per task with a `pr_url`, archived
ones included, sorted by recency with a count in the header.

Each row shows a green `shipped` pill, the `project/slug` label, and a PR link
labeled `<repo>#<number> · <state>`, rendering `open` until a poll lands.

The row carries a cleanup-state note: "awaiting merge · cleanup deferred"
while the pull request is unresolved, a ready-for-cleanup note once it is
merged or closed, and "cleaned up <elapsed>" once archived. Live rows link to
the task's Ship Flow view; archived rows are inert text.

### Filtering

The Tasks view honors a `project` query parameter, filtering both the cards
and the Shipped section and naming the filter in the header subline. A project
card's active-tasks pill navigates to `/tasks?project=<name>`.

### Confirmation

Cleanup requires explicit confirmation naming the clone path that will be
deleted and, when one is recorded, the container that will be removed. No
request is sent until the operator confirms.

## Failures and recovery

| Condition | Response |
|---|---|
| Slug contains uppercase, slashes, dots, or other invalid characters | `422`, no row and no filesystem change |
| Slug exceeds 64 characters | `422` |
| Project/slug pair already live | `409`, registry unchanged |
| Purge on a non-archived task | `409`, row retained |

A failed spawn leaves the task `failed` with the captured stderr accessible
from its card.

## Interfaces

| Method | Path |
|---|---|
| `GET` | `/api/tasks` |
| `GET` | `/api/tasks/{id}` |
| `POST` | `/api/tasks` |
| `POST` | `/api/tasks/{id}/cleanup` |
| `DELETE` | `/api/tasks/{id}` |

Mutations broadcast `task_created`, `task_updated`, and `task_deleted` — the
first two with the full payload, deletion with the id. The snapshot's `tasks`
array holds every non-purged task.
