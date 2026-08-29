# Spawn a task

A task is one deliverable against a project: a bug fix, a change, an
investigation. Spawning a task creates its isolated workspace and starts its
workflow.

## What spawning does

Spawn runs four steps in order. Each publishes progress, and a failure leaves
the task in `failed` with the step name and its stderr attached.

| Step | Action |
|---|---|
| `fetch` | `git fetch` the project's [fetch remote](../reference/projects.md#fetch-remote) in its checkout, so the clone starts from current refs |
| `clone` | Local clone of the checkout into `task_dir_root/<project>/<slug>` |
| `branch` | Branch off `origin/<base>` using the template's branch pattern |
| `workshop` | Launch the task's container and confirm it registered |

The clone is a local clone of your checkout, not a Git worktree. It has its
own `.git` directory, so nothing an agent does can corrupt your working
repository. The `workshop` step is allowed far more time than the Git steps,
because launching a container includes SDK installation.

Ompire also writes `.ompire/` into the clone's `.git/info/exclude`, so
structured step outcomes never appear as untracked files in the agent's view
of the tree.

## Spawn through the UI

Open the Spawn view, pick a template, review the derived branch name, write
the prompt, and submit. The template picker shows each template's checkout,
base branch, model, and workflow; a read-only block describes the selected
template's workflow, and the branch preview updates live as you type the slug.

Model and thinking controls default to "template default (…)"; leaving them
alone sends no override.

### Attach a file to the prompt

Type `@` in the prompt to search the project's repository, then pick a path
with the arrow keys and Enter, or with the mouse. The path is inserted where
you are typing, and the agent receives that file as context — so you can write
"fix the redirect in @frontend/src/lib/token.ts" instead of describing where
the code lives.

Escape closes the list and leaves what you typed alone, so an email address or
any other `@` in your prompt is never rewritten.

If a mention cannot be attached, the spawn is refused before anything is
created and the message says why. The usual reason is a file that is not on
the template's base branch: the task's clone is made from that branch, so a
file you only just created locally would not be there. Commit it to the base
branch, or drop the mention, and submit again — nothing you typed is lost.

Submitting locks the form until the launch resolves, so a second click
cannot create a second task. Pipeline progress is shown per step, and a failed
step expands its stderr in place.

When the workspace is ready, Ompire opens the task for you and you watch the
agent from its detail view. If the pipeline fails instead, you stay on the
Spawn view: read the failing step, then either open the failed task or select
**Start another task** to correct the slug or prompt and submit again. Nothing
you typed is discarded.

## Spawn through the API

Spawning is template-driven — the request names a **template**, not a project.
The project is derived from the template. You need at least one template
before you can spawn anything.

```sh
TOKEN=$(cat ~/.local/share/ompire/token)
curl -sS -X POST http://127.0.0.1:4173/api/tasks \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
        "template_name": "my-project-default",
        "slug": "fix-login-redirect",
        "prompt": "Fix the redirect loop after login.",
        "thinking": "high"
      }'
```

`template_name`, `slug`, and `prompt` are required. `model` and `thinking` are
optional per-spawn overrides; omitting them uses the template's values, and a
template with neither set uses the agent's defaults.

The prompt may contain `@relative/path` mentions. List the paths a project
offers with `GET /api/projects/{name}/files?q=<query>`. A mention that cannot
become file context returns `422` and creates nothing.

Returns `202` immediately with the created task. An unknown `template_name`
returns `404` and creates nothing. Spawning continues in the background; watch
the WebSocket or poll `GET /api/tasks/{id}` for progress.

## Templates

A template supplies the spawn configuration a task does not state itself:

| Field | Meaning |
|---|---|
| `project_name` | The project this template belongs to |
| `base_branch` | Branch to base work on. Defaults to `main`. |
| `branch_pattern` | How the branch name is derived, e.g. `ompire/<slug>` |
| `workflow` | Which workflow the task runs. Defaults to `single-step`. |
| `workshop_additions` | Extra container setup |
| `model` | Model override, or unset for the agent's default |
| `thinking` | Thinking-effort override |
| `preamble` | Text prepended to every prompt spawned from this template |

The template is resolved once at spawn time and the values it supplied are
copied onto the task. Editing a template afterwards does not change tasks
already spawned from it — a run's configuration is fixed when it starts.

## Workflows

Two workflows ship today:

- `single-step` — one agent step. The agent works, you review, you ship.
- `bugfix` — reproduce, triage, fix, validate, check, escalate. Routing between
  steps is decided by explicit rules, and an unresolved outcome stops at a
  human gate rather than being guessed.

## While it runs

The task's sessions report status, and Ompire aggregates that into the
attention tier shown on the task card. You do not need to watch it — see [The
attention model](../explanation/attention.md) for what will and will not
interrupt you.

You can steer a running agent, send a follow-up, interrupt it, or answer a
question it asked, from the task detail view.

## Cleaning up

`POST /api/tasks/{id}/cleanup` removes the workshop and then deletes the
clone, in that order, and archives the task. Cleanup refuses any path outside
the configured task root.

## Next

[Review and ship a task](review-and-ship.md).
