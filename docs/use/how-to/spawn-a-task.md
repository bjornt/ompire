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
| `branch` | Branch off `origin/<base>` using the accepted branch pattern |
| `workshop` | Launch the task's container and confirm it registered |

The clone is a local clone of your checkout, not a Git worktree. It has its
own `.git` directory, so nothing an agent does can corrupt your working
repository. The `workshop` step is allowed far more time than the Git steps,
because launching a container includes SDK installation.

Ompire also writes `.ompire/` into the clone's `.git/info/exclude`, so
structured step outcomes never appear as untracked files in the agent's view
of the tree.

## What you choose

Three things, and nothing else has to exist first:

- **a workflow** — any of the ones the daemon ships, against any ready project;
- **a project** — its checkout, remotes, base branch, branch pattern, Workshop
  additions source, and standing preamble come with it;
- **a model profile** — either the project's default, inherited, or one you
  select for this task.

You need at least one [model profile](../reference/model-profiles.md). Ompire
never picks one for you and never falls back to whatever your own `omp` is
configured to use.

## Spawn through the UI

Open the Spawn view, choose the workflow and the project, write the slug and
the prompt, and submit.

Beside the form, Ompire lists every step the workflow declares with the model
and thinking level each one would use. Steps that never reach a model — a
command, a decision, a human gate — are shown without one. A step a decision
can route past is marked *conditional*, and the workflow engine's judge appears
as a conditional row on your profile's `slow` binding. It is a list of what the
run *may* do, not a promise about the path it will take.

The profile selector starts on "inherit from project". Choosing a profile
replaces that for this task, and **Reset to project default** puts it back. Your
explicit choice survives changing the project.

**Advanced** holds the four workspace values the project supplies — base
branch, branch pattern, Workshop additions, preamble. Each shows the project's
value until you change it, and each has its own reset. Only what you changed is
sent.

If you have no profiles yet, the form says so and links to Settings. Your draft
is kept while you go and create one.

### Attach a file to the prompt

Type `@` in the prompt to search the project's repository, then pick a path
with the arrow keys and Enter, or with the mouse. The path is inserted where
you are typing, and the agent receives that file as context — so you can write
"fix the redirect in @frontend/src/lib/token.ts" instead of describing where
the code lives.

Escape closes the list and leaves what you typed alone, so an email address or
any other `@` in your prompt is never rewritten.

If a mention cannot be attached, the spawn is refused before anything is
created and the message says why. The usual reason is a file that is not on the
effective base branch: the task's clone is made from that branch, so a file you
only just created locally would not be there. Commit it to the base branch, or
drop the mention, and submit again — nothing you typed is lost.

Submitting locks the form until the launch resolves, so a second click
cannot create a second task. Pipeline progress is shown per step, and a failed
step expands its stderr in place.

When the workspace is ready, Ompire opens the task for you and you watch the
agent from its detail view. If the pipeline fails instead, you stay on the
Spawn view: read the failing step, then either open the failed task or select
**Start another task** to correct the slug or prompt and submit again. Nothing
you typed is discarded.

If something about the configuration changed while you were reading the preview
— someone edited the project, or the profile — the submission is refused and
the changed choices are shown instead. Ompire does not relaunch under settings
you have not looked at; read them and submit again.

## Spawn through the API

Two calls: preview what would run, then submit that same selection with the
token identifying what you reviewed.

```sh
TOKEN=$(cat ~/.local/share/ompire/token)
BODY='{
        "project_name": "my-project",
        "workflow_name": "bugfix",
        "slug": "fix-login-redirect",
        "prompt": "Fix the redirect loop after login."
      }'

PREVIEW=$(curl -sS -X POST http://127.0.0.1:4173/api/tasks/preview \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d "$BODY")

curl -sS -X POST http://127.0.0.1:4173/api/tasks \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d "$(jq --arg t "$(jq -r .preview_token <<<"$PREVIEW")" \
          '. + {preview_token: $t}' <<<"$BODY")"
```

`project_name`, `workflow_name`, `slug`, and `prompt` are required.
`model_profile` is optional — omit it to inherit the project's default.
`workspace_overrides` is optional and may carry any of `base_branch`,
`branch_pattern`, `workshop_additions`, and `preamble`; what you leave out is
inherited.

`GET /api/workflows` lists what you can pass as `workflow_name`.

The prompt may contain `@relative/path` mentions. List the paths a project
offers with `GET /api/projects/{name}/files?q=<query>`. A mention that cannot
become file context returns `422` and creates nothing.

Acceptance returns `202` immediately with the created task, including the
`execution_inputs` it was accepted under. A `409` with a `preview_changed`
reason means the resolution moved between the two calls; the response carries
the current one. Spawning continues in the background; watch the WebSocket or
poll `GET /api/tasks/{id}`.

## What a task keeps

Acceptance pins the whole decision onto the task: the four role bindings, which
role each step uses, the effective base branch and preamble, the rendered
branch, and the project's checkout and publishing routing.

That is what the task runs — through the pipeline, the workflow, a restart,
review, and shipping. Editing the project or the profile afterwards changes
your next launch and nothing about this one. You can see exactly what a task
was accepted with on its detail view.

## Workflows

Two workflows ship today, and both are available to every project:

- `single-step` — one agent step. The agent works, you review, you ship.
- `bugfix` — reproduce, triage, fix, validate, check, escalate. Routing between
  steps is decided by explicit rules, and an unresolved outcome stops at a
  human gate rather than being guessed.

A workflow's steps name an abstract role — `default`, or one of the auxiliary
roles — never a model. Which model answers to that role is your profile's
choice, made at launch.

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
