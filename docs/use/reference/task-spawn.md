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
| `prompt` | yes | The operator's instruction, including any `@file` mentions |
| `model` | no | Per-spawn override of the template's model |
| `thinking` | no | Per-spawn override of the template's thinking level |

The call returns `202` with the new task id and the pipeline runs
asynchronously. An unknown `template_name` returns `404` and creates nothing.
A prompt whose `@file` mention cannot become file context returns `422` and
also creates nothing — see [File mentions](#file-mentions).

### The Spawn view

A template picker labeled "Project template", with option lines reading
`name — checkout · base <branch> · <model> · wf:<workflow>` and an "Edit
templates" link to `/settings`.

Below it, a read-only "Workflow — from template" block describing the selected
template's workflow, model and thinking override controls each defaulting to
"template default (…)", a slug field with a live branch-name preview, and a
prompt editor that offers repository paths when `@` is typed.

Submitting locks the form. Every input and the submit button are disabled
from the moment the button is activated, so one activation creates at most one
task; the button reads `Creating…` until the daemon accepts the request, then
`Launching…` while the pipeline runs. The locked fields keep the submitted
values, so the request stays readable while the workspace is built.

The pipeline panel reads `Creating the task…` until the daemon accepts the
request, then renders per-step pipeline progress. A failed step expands its
stderr or error text in place — there is no separate failure screen.

The authoring form is the wider of the two panels wherever they sit side by
side, and the pipeline panel is capped: it has four steps and an optional
stderr block to show, so extra width on a large monitor goes to the prompt
rather than to the panel that is blank until something is submitted. Long
paths in a captured error wrap inside the panel instead of widening it. Below
roughly 900px the view becomes one column — the form spans the full width with
the pipeline panel beneath it — and the paired model and thinking overrides
stack whenever the form is too narrow to give both a usable width.

When the daemon records spawn completion, Ompire opens the task's detail view
at `/tasks/<task-id>`, replacing the Spawn view in browser history. It does not
wait for the agent's first turn: the transcript may open empty. The workflow
run starts at the same moment, and its step progress belongs to task detail's
workflow strip rather than the Spawn view.

A failed pipeline keeps the operator on the Spawn view with the form still
locked to that task, the failing step and its captured text visible, and two
actions: **Open failed task**, which opens `/tasks/<task-id>`, and **Start
another task**, which clears the pipeline and unlocks the form while keeping
the entered template, slug, prompt, and overrides.

A request the daemon refuses — an unknown template, a duplicate or invalid
slug, a clone path outside the task root, or a transport failure — creates
nothing. The form unlocks immediately, keeps everything that was typed, and
shows the daemon's message. If the accepted task is deleted or purged while the
form is locked, the form unlocks and says so.

## File mentions

A prompt may name files from the project's repository. The agent receives each
mentioned file as context, so it starts from the right file instead of
searching for it.

### Writing one

Typing `@` at the start of the prompt or after whitespace opens a suggestion
list of repository-relative paths from the template's project; the characters
typed after it narrow the list. `@` inside a word — an email address, a
decorator — opens nothing.

| Key | Effect |
|---|---|
| ↑ / ↓ | Move through the suggestions |
| Enter or Tab | Insert the highlighted path |
| Escape | Close the list, leaving the typed text exactly as written |

Escape keeps the list closed while you go on typing that mention. Moving off it
and starting another `@` opens suggestions again.

Selecting inserts `@` plus the path at the caret, replacing the partial token
wherever it sits in the prompt, followed by a separating space. A prompt may
carry several mentions. Choosing a different template closes the list and
points later lookups at the new template's project without touching text
already written.

The list offers what the repository tracks plus files present but not yet
committed, and never offers anything the repository ignores. It never shows
file contents.

The prompt is stored and displayed with the literal mention text, so the
request stays readable:
`Fix the redirect in @frontend/src/lib/token.ts`.

### Which files can be attached

The task's clone is made from the template's base branch, so only what that
branch carries reaches the agent. A file that exists in your checkout but is
not on the base branch — one you just created, or one committed to another
branch — is offered by the search but refused at submit, because it would not
be in the clone.

The refusal names the path and the reason, nothing is created, and the form
keeps everything you typed.

### Reaching the agent

Omp parses `@path` out of the message it is sent and resolves it against the
agent's working directory, which is the task's clone. Ompire re-checks every
mention against the clone immediately before delivering the prompt: a mention
that no longer resolves fails the step with the path named, rather than being
delivered as a reference omp would silently drop.

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
| Prompt mention is absolute, contains `..`, or resolves outside the checkout | `422`, nothing created |
| Prompt mention names a missing path or something that is not a regular file | `422`, nothing created |
| Prompt mention is not on the template's base branch | `422`, nothing created — the clone would not contain it |
| Prompt mention stops resolving in the clone before delivery | Step fails with the path named; no prompt is sent |
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
