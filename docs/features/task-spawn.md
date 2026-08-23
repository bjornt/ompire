# Task spawn

## Overview

Spawning builds a task's workspace: a fetched checkout, an isolated clone, a
branch, and a running container. It is the boundary between "a task record
exists" and "an agent can work".

Spawning is template-driven. `POST /api/tasks` takes a `template_name`, not a
project — the project, checkout, remotes, base branch, branch pattern, and
workflow all come from the template.

## Using spawn

`POST /api/tasks` accepts:

| Field | Required | Meaning |
|---|---|---|
| `template_name` | yes | Resolves the project and all spawn configuration |
| `slug` | yes | Task slug; the branch is derived from it |
| `prompt` | yes | The operator's instruction |
| `model` | no | Per-spawn override of the template's model |
| `thinking` | no | Per-spawn override of the template's thinking level |

The call returns `202` with the new task id and the pipeline runs
asynchronously. An unknown `template_name` returns `404` and creates nothing.

### The Spawn view

A template picker labeled "Project template", with option lines reading
`name — checkout · base <branch> · <model> · wf:<workflow>` and an "Edit
templates" link to `/settings`.

Below it, a read-only "Workflow — from template" block describing the selected
template's workflow, model and thinking override controls each defaulting to
"template default (…)", a slug field with a live branch-name preview, and a
prompt editor.

After submit, per-step pipeline progress renders inline, followed by the
workflow's own step progress in the same list. A failed step expands its
stderr or error text in place — there is no separate failure screen.

## States and behavior

### The pipeline

| Step | Action |
|---|---|
| `fetch` | `git fetch origin` in the project's checkout |
| `clone` | Local hardlink clone of the checkout to `<task_root>/<project>/<slug>` |
| `branch` | New branch from the template's pattern, off `origin/<base_branch>` |
| `workshop` | Launch the task's container in the clone |

Git runs as subprocesses with argument lists, never through a shell. Each git
step is bounded by `spawn_step_timeout`; the workshop step has its own,
larger, `workshop_step_timeout`.

The clone step also appends `.ompire/` to the clone's `.git/info/exclude`, so
structured step outcomes never appear as untracked files in the agent's view
of the tree.

Spawn completion is recorded only after the last workspace step succeeds.

### Template resolution

The template is resolved once, at pipeline start. A template deleted between
task creation and pipeline start fails the task with a clear error **before
any git command runs**.

Effective model and thinking resolve as: the spawn-time override if given,
else the template's value, else omitted. Omitted means the agent's own
default. The resolved values are carried to the workflow engine, which passes
them to every session it spawns for the task.

The template's `preamble` is *not* applied by the pipeline. Prompt
construction belongs to the workflow.

### After the pipeline

Starting the agent and delivering the prompt are not pipeline steps. Once the
workspace is complete, the task is handed to the workflow engine, which spawns
sessions lazily and delivers prompts as `agent` steps.

### Workshop launch

After the branch step, the configured my-workshop command runs as a subprocess
with the clone as working directory. On success the daemon reads the workshop
identity from `.workshop.lock` in the clone and records it on the task.

A zero exit with a missing or empty lock file is treated as a step failure —
the container may have started, but without its identity the daemon cannot
manage it later.

## Failures and recovery

| Condition | Result |
|---|---|
| Template missing at pipeline start | Task `failed` before any git command |
| Any step exits non-zero or times out | Pipeline stops, task `failed`, stderr stored on the task |
| Resolved clone path falls outside the task root | Spawn rejected before any git command |
| Target clone directory already exists | Clone step fails; the directory is never reused |

A leftover directory fails loudly on purpose. Reusing it would mean an agent
starting in a workspace whose contents nobody has accounted for.

Every failure stores the step's captured stderr or error text on the task,
reachable from its card.

## Configuration

| Key | Effect |
|---|---|
| `task_dir_root` | Parent for clone paths, and the confinement boundary |
| `spawn_step_timeout` | Bound on each git step |
| `workshop_step_timeout` | Bound on the workshop step |
| `my_workshop_command` | The container launch command |

## Interfaces

Each step broadcasts `spawn_step` carrying the task id, the step name
(`fetch`, `clone`, `branch`, `workshop`), and a status of started, ok, or
failed with stderr.

A successful run produces started/ok pairs for all four steps in order,
followed by `workflow_step` events as the run executes.
