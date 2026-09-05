# Templates

## Overview

A template supplies everything spawning a task needs that the task does not
state itself: which project, which branch to base on, how to name the branch,
which workflow to run, how to configure the container and model, and what text
to prepend to every prompt.

Templates exist so that spawning a task is a slug and a prompt rather than a
form of a dozen fields.

A template is resolved once at spawn time and the values it supplied are
copied onto the task. Editing a template afterwards does not affect tasks
already spawned from it.

## Fields

| Field | Default | Meaning |
|---|---|---|
| `name` | — | Unique identifier, slug format |
| `project_name` | — | Must reference an existing project |
| `base_branch` | `main` | Branch work is based on |
| `branch_pattern` | daemon config | Exactly one `<slug>` placeholder; the rest must be git-ref-safe |
| `workflow` | `single-step` | Must name a workflow registered in the daemon |
| `workshop_additions` | `project` | `project` uses the project's `workshop.my.yaml`; `global` uses `~/.config/my-workshop/my.yaml` |
| `model` | null | Model override. Null means the agent's default. |
| `thinking` | null | Thinking level. Null means the agent's default. |
| `preamble` | empty | Text prepended to every prompt spawned from this template |

Checkout path and remotes are not template fields. They come from the
referenced [project](projects.md).

### Model and thinking

A template's `model` and `thinking` are what a spawned task actually uses,
unless the spawn overrides them. `model` is passed to the agent as written and
may be a fuzzy name such as `sonnet`; null on either field means the agent's
own default.

These are separate from [model profiles](model-profiles.md), which store
global role bindings and are selected as a project's default. Profiles are
saved configuration and do not affect task execution: a project may name a
profile while its templates continue to determine every spawned task's model
and thinking level. Template behavior is unchanged by profiles.

### Thinking levels

`off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, `auto`. Anything
else is rejected.

A template may leave thinking null; a model profile binding may not. The
vocabulary is the same, the permitted absence is not.

## Using templates

Templates are managed from the Settings view. The "Project templates" list
shows one row per template with its name and a summary line — checkout, base
branch, branch pattern, model, and `wf:<workflow>`.

Only a project whose checkout setup is `ready` can be picked; one that is
still cloning or whose setup failed is shown disabled with its state, because
a template pointing at it could not spawn. See
[Projects](projects.md#setup-state).

The editor offers a project picker (showing the picked project's checkout and
remote read-only), base branch and branch pattern fields, a workflow select
listing the daemon's registered workflows, a workshop-additions select, model
and thinking fields, a prompt preamble textarea, and a guarded removal action.

The list updates over the WebSocket without a reload.

### Preamble

The preamble is prepended to every prompt spawned from the template. It is the
place for standing instructions that apply to all work in a project — coding
conventions, what to read first, what never to touch — so operators do not
retype them into every task prompt.

## States and behavior

Templates have no lifecycle state.

**Update is unguarded.** Edits affect only future spawns, so there is nothing
to protect: tasks already spawned carry their own resolved copy.

**Removal is guarded by live tasks only.** Deleting a template fails with
`409` while any non-archived task references it, naming those tasks. Archived
tasks keep the template name as a historical annotation and do not block
removal.

This asymmetry with [projects](projects.md) — where archived tasks *do* block
— is deliberate. A project is the thing an archived task was work against; a
template is only how that task was configured.

## Failures and recovery

| Condition | Response |
|---|---|
| Name is not a valid slug | `422` |
| Name already exists | `409` |
| `project_name` does not exist | `422` |
| Branch pattern lacks `<slug>`, has more than one, or is git-ref-unsafe | `422` |
| Workflow is not registered | `422` |
| Thinking level outside the accepted vocabulary | `422` |
| Delete while a live task references it | `409` naming the tasks |

Rejections leave the registry unchanged and render inline in the editor, which
stays open.

## Configuration

`default_branch_pattern` in `config.toml` supplies the default branch pattern
for new templates. It defaults to `ompire/<slug>`.

## Interfaces

| Method | Path |
|---|---|
| `GET` | `/api/templates` |
| `POST` | `/api/templates` |
| `GET` | `/api/templates/{name}` |
| `PUT` | `/api/templates/{name}` |
| `DELETE` | `/api/templates/{name}` |

Templates appear in the WebSocket snapshot. Mutations broadcast
`template_created` and `template_updated` with the full payload, and
`template_deleted` with `{name}`.

A task's detail view annotates its "spawned" row with `· template <name>` when
the task carries one. Tasks that predate templates have a null template name
and show no annotation.
