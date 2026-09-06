# Task spawn

## Overview

Spawning builds a task's workspace: a fetched checkout, an isolated clone, a
branch, and a running container. It is the boundary between "a task record
exists" and "an agent can work".

A launch is three choices: a workflow, a project, and a model profile. Every
registered workflow is available to every ready project, and there is no saved
preset in between — see [Model profiles](model-profiles.md) for what a profile
binds and [Projects](projects.md) for the workspace defaults a launch
inherits.

Launching is two calls. `POST /api/tasks/preview` resolves the selections and
returns what would run; `POST /api/tasks` submits the same selections plus the
token identifying the resolution that was reviewed. Both use the same rules, so
what you approve is what is stored.

## Using spawn

Both calls accept:

| Field | Required | Meaning |
|---|---|---|
| `project_name` | yes | The project to work against |
| `workflow_name` | yes | Any installed workflow. The launch pins that name's current revision. |
| `slug` | yes | Task slug; the branch is derived from it |
| `prompt` | yes | The operator's instruction, including any `@file` mentions |
| `model_profile` | no | Omitted means "inherit the project default"; a name replaces that inheritance for this task |
| `workspace_overrides` | no | Task-local overrides of the project's workspace defaults |
| `step_overrides` | no | Per-agent-step profile and role choices, keyed by declared step name |
| `auxiliary_overrides` | no | Retired with the engine's judge. Any entry is `422` rather than dropped. |
| `preview_token` | on `POST /api/tasks` | The token from the preview that was reviewed |

`workspace_overrides` may carry `base_branch`, `branch_pattern`,
`workshop_additions`, and `preamble`. Leaving a key out inherits the project
value. An explicit empty `preamble` is an override to "no preamble"; an
explicit null for any of the other three is refused, because none of them has
a meaningful empty value. Any other field — including the retired
`template_name` and the old scalar `model` and `thinking` overrides — is
refused rather than ignored.

### Per-consumer overrides

Each entry of `step_overrides` is an object with two optional fields,
`model_profile` and `role`. Omitting a field — or passing
`null` — inherits that dimension; an entry that overrides neither is the same
launch as no entry at all, and resolves to the same `preview_token`.

```json
{
  "step_overrides": {
    "reproduce": {"model_profile": "economy"},
    "fix": {"role": "plan"},
    "validate-agent": {"model_profile": "thorough", "role": "slow"}
  }
}
```

Precedence per consumer:

| Dimension | Order |
|---|---|
| Profile | row override → explicit task `model_profile` → project default |
| Role | row override → the role the workflow declares for that step |

The role selects one complete `(model, thinking)` pair from the effective
profile, so changing a role changes both together. All four roles — `default`,
`smol`, `slow`, `plan` — are selectable for any consumer. Choosing an active
role never rewrites the profile's other role bindings: every process still
carries the complete native map.

The two namespaces are separate, so a decision step can never become an agent
binding by sharing a name with an auxiliary consumer. Refused at the named
field, creating nothing: an unknown step, a step with no model (a command,
decision, or gate), an unknown auxiliary consumer, a role outside the four, an
empty or unknown profile name, and any unknown field inside an entry. A
concrete `model` or `thinking` is not accepted here — those are profile
settings, deliberately not a third override hierarchy.

A task profile is still required even when every row is overridden.

The preview returns each consumer's resolved binding, the task-wide profile's
own role map, the effective workspace values with the project's own values
beside them, the rendered branch, every declared step of the workflow, and a
`preview_token`. Each step row carries `declared_role` — the role the workflow
declares — and a `binding` object identical to what acceptance stores for that
consumer, or `null` for a step with no model.

The `preview_token` covers every consumer's *complete* role map, not just its
active pair. Editing a profile's `slow` binding changes no summary line and
still invalidates the review, because it changes what a `/switch slow` inside
that step's container would reach. An edit to a profile this launch does not
use leaves the review valid.

`POST /api/tasks` returns `202` with the new task and its pinned
`execution_inputs`, and the pipeline runs asynchronously. If the resolution
changed since the preview, it returns `409` with a `preview_changed` reason and
the current resolution; nothing is created and nothing is retried under the new
settings. A prompt whose `@file` mention cannot become file context returns
`422` and also creates nothing — see [File mentions](#file-mentions).

### The Spawn view

Workflow, project, and model-profile selectors; a slug field with a live
branch-name preview; and a prompt editor that offers repository paths when `@`
is typed.

The profile selector's first option is "inherit from project — <name>", or
"inherit from project — none set" for a project with no default. Choosing a
profile replaces that inheritance and offers a **Reset to project default**
control; the explicit choice survives changing the project.

An **Advanced** section holds the four workspace defaults. Each shows the
project's value until you change it, and then offers its own **Reset to project
default**. Only the fields you actually changed are sent.

Beside the form, the preview lists every step the workflow declares, in order,
with its kind, session, abstract role, concrete model, and thinking policy. A
command, decision, or gate step is shown with no model, because it never
reaches one. A step a route can pass by, or that carries its own condition, is
marked *conditional*. Every model consumer is one of these rows. The list is
what the run *may* execute, not a prediction that it will.

The preview also names the **workflow revision** it would pin — the content
identity of the exact definition — which the `preview_token` covers, so an
edited prompt or route invalidates the review even though the step list looks
identical.

Every row that consumes a model carries its own profile and role selectors,
each with its own reset, and states separately whether its profile and its role
are inherited or set here. A row with no binding gets no controls. Expanding a
row's **native roles** shows the complete `smol`/`slow`/`plan` map its process
will carry.

The controls are rendered from the daemon's workflow catalog rather than from
the resolution, so a row whose selected profile has since been deleted stays on
screen — with that profile still shown, marked unavailable — and can be
corrected. Ompire never silently re-picks a profile for you.

Rows follow the task profile only while they are inherited: changing the task
profile moves every inherited row and leaves explicit ones alone. An explicit
choice equal to what would have been inherited is still explicit. Changing the
workflow clears every row choice and says which were cleared; nothing transfers
by position or by a coincidentally matching name, and the slug, prompt, task
profile, and workspace overrides stay.

Thinking is shown as the policy the profile states. omp resolves `auto` and
`max` to a model-specific level at run time; [task detail](task-detail.md)
shows that resolved level beside the accepted policy for a running session.

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
the selections, slug, prompt, and overrides.

A request the daemon refuses — a duplicate or invalid slug, a project that is
not ready or not reconciled, a clone path outside the task root, or a transport
failure — creates nothing. The form unlocks immediately, keeps everything that
was typed, and shows the daemon's message. A stale review is shown as a changed
resolution to look at and submit again, never as an automatic retry. If the
accepted task is deleted or purged while the form is locked, the form unlocks
and says so.

Leaving the form to create a model profile in Settings keeps the draft: coming
back restores the workflow, project, slug, prompt, and every override,
including the per-row choices.

## File mentions

A prompt may name files from the project's repository. The agent receives each
mentioned file as context, so it starts from the right file instead of
searching for it.

### Writing one

Typing `@` at the start of the prompt or after whitespace opens a suggestion
list of repository-relative paths from the selected project; the characters
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
carry several mentions. Choosing a different project closes the list and points
later lookups at that project without touching text already written.

The list offers what the repository tracks plus files present but not yet
committed, and never offers anything the repository ignores. It never shows
file contents.

The prompt is stored and displayed with the literal mention text, so the
request stays readable:
`Fix the redirect in @frontend/src/lib/token.ts`.

### Which files can be attached

The task's clone is made from the effective base branch, so only what that
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
| `fetch` | `git fetch <project fetch remote>` in the project's checkout |
| `clone` | Local hardlink clone of the checkout to `<task_root>/<project>/<slug>` |
| `branch` | New branch from the accepted pattern, off `origin/<base_branch>` |
| `workshop` | Launch the task's container in the clone |

Git runs as subprocesses with argument lists, never through a shell. Each git
step is bounded by `spawn_step_timeout`; the workshop step has its own,
larger, `workshop_step_timeout`.

The two remotes in that table are different things. `fetch` refreshes the
project's own [`fetch_remote`](projects.md#fetch-remote) in the base checkout,
which is `origin` unless the project says otherwise. `branch` then resolves
`origin/<base_branch>` inside the **task's** clone, whose `origin` always
points back at that base checkout.

The clone step also appends `.ompire/` to the clone's `.git/info/exclude`, so
structured step outcomes never appear as untracked files in the agent's view
of the tree.

Spawn completion is recorded only after the last workspace step succeeds.

### Where the pipeline's values come from

The pipeline resolves nothing. Acceptance already reviewed and pinned every
value it needs — the checkout path, the fetch remote, the base branch, the
rendered branch, and the Workshop additions source — in the same transaction
that created the task row. Editing the project between the `202` and the
pipeline cannot repoint the fetch or move the branch point.

The same is true of the workflow definition and of model policy: the task
carries the pinned revision and one complete binding per declared model
consumer, and the workflow engine applies the right one to each session's
process before every turn. A consumer with no stored binding fails the step
rather than falling back — neither to a task-wide
default nor to the agent's own. See [Workflow
engine](workflow-engine.md#model-policy-per-turn) for how a policy reaches a
session that is already live.

The preamble is *not* applied by the pipeline. Prompt construction belongs to
the workflow.

### Workshop additions

The accepted additions source is applied around the launcher. my-workshop
resolves additions local-first with no way to select a source, so the daemon
stages the selected source at the clone's `workshop.my.yaml` before the
launcher runs and restores the clone — including restoring the file's absence —
afterwards, whether the launcher succeeded or failed.

A selected source that does not exist is staged as an explicitly empty
additions file and disclosed as "no additions". That is what stops the launcher
falling back to the source you did not choose. A selected source that exists
but cannot be read fails workspace setup rather than launching with nothing
applied. The staged file is gone before any agent starts in the clone, the
registered checkout is never touched, and a crash mid-staging is undone at the
next startup before recovery resumes anything.

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
| Prompt mention is not on the effective base branch | `422`, nothing created — the clone would not contain it |
| Prompt mention stops resolving in the clone before delivery | Step fails with the path named; no prompt is sent |
| Project's checkout setup is not `ready` | `409`, nothing created — finish or retry it on the Projects view |
| Project's launch configuration is not reconciled | `409`, nothing created — resolve it on the Projects view |
| Project has no default profile and none was selected | `422`, nothing created — nothing is inferred |
| The resolution changed since the preview | `409` with `preview_changed` and the current resolution; nothing created |
| The selected Workshop additions source is unreadable | Workspace setup fails before any agent starts |
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
failed with stderr. The workshop step is preceded by a `workshop_additions`
event naming the source that applied and whether it was absent.

A successful run produces started/ok pairs for all four steps in order,
followed by `workflow_step` events as the run executes.
