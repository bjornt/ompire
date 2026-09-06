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
command, a decision, a human gate — are shown without one. A step a route can
pass by, or that has its own condition, is marked *conditional*. It is a list
of what the run *may* do, not a promise about the path it will take. Every
model consumer is one of these rows; nothing runs a model outside them.

The preview also names the workflow **revision** it would pin: the content
identity of the exact definition, which the task then executes for its whole
life. If a definition changes between your preview and your submission — a
daemon upgrade, say — submission is refused with the current resolution rather
than accepted under a procedure you did not review. Your draft is kept.

The profile selector starts on "inherit from project". Choosing a profile
replaces that for this task, and **Reset to project default** puts it back. Your
explicit choice survives changing the project.

### Override a single step

Every row that consumes a model — that is, each agent step — has its own
profile and role selectors. They are independent: you can send one step to a
different profile while it keeps the role its workflow declares, and give
another a different role while it stays on the task's profile. Each has its own
**Reset**, and resetting one dimension leaves the other alone.

Changing the role changes the model *and* the thinking level together, because
a role names one complete pair in the profile. On `bugfix`, for example, you
might put `reproduce` on a cheaper profile, run `fix` on `plan`, and give
`verify` both a different profile and `slow`.

A row you have not touched follows the task profile: change the profile at the
top and every inherited row moves with it, while rows you chose explicitly stay
where you put them. Each row says which of its two values is inherited and
which you set. Expanding **native roles** under a row shows the complete
`smol`/`slow`/`plan` map that step's process will carry — what a `/switch smol`
inside its container would reach.

Switching workflows clears these row choices and tells you so. A step name that
also exists in another workflow is a different step, so nothing is carried
across by name or by position. Your slug, prompt, task profile, and workspace
overrides are not workflow-scoped and stay.

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

`step_overrides` carries the per-row choices, keyed by declared step name. Each
entry takes `model_profile`, `role`, or both; what you leave out is inherited:

```json
{
  "step_overrides": {
    "reproduce": {"model_profile": "economy"},
    "fix": {"role": "plan"},
    "verify": {"model_profile": "thorough", "role": "slow"}
  }
}
```

Send the same map to the preview and to acceptance. An unknown step, a step
with no model, an invalid role, or a profile that does not exist is refused
with the offending field named, and creates nothing.

`auxiliary_overrides` is retired along with the engine's judge. A request that
still names it is refused with a field-level `422` rather than having the
choice silently dropped — the model it named no longer runs at all.

`GET /api/workflows` lists what you can pass as `workflow_name`, along with the
revision each name currently resolves to.

The prompt may contain `@relative/path` mentions. List the paths a project
offers with `GET /api/projects/{name}/files?q=<query>`. A mention that cannot
become file context returns `422` and creates nothing.

Acceptance returns `202` immediately with the created task, including the
`execution_inputs` it was accepted under. A `409` with a `preview_changed`
reason means the resolution moved between the two calls; the response carries
the current one. Spawning continues in the background; watch the WebSocket or
poll `GET /api/tasks/{id}`.

## What a task keeps

Acceptance pins the whole decision onto the task: the **workflow revision**,
one complete binding for every model consumer — its source profile, its role,
which of the two you set and which was inherited, and the full four-role map
that profile bound — plus the effective base branch and preamble, the rendered
branch, and the project's checkout and publishing routing.

That is what the task runs — through the pipeline, the workflow, a restart,
review, and shipping. Editing a profile afterwards, or deleting one, changes
your next launch and nothing about this one. The same is true of the workflow
itself: upgrading Ompire changes what your *next* `bugfix` task runs, not one
already in flight. You can see exactly what a task was accepted with, per
consumer, on its detail view — including the revision, with the definition
itself readable there.

## Workflows

Two workflows ship today, and both are available to every project:

- `single-step` — one agent step. The agent works, you review, you ship.
- `bugfix` — QA tries to reproduce, a coder diagnoses, QA tries again with
  those findings if the first attempt failed, then fix and verify. Routing is
  decided by explicit rules over declared results; where the evidence does not
  decide, the run stops and asks you, offering the answers the workflow
  declares rather than a bare Resume. Its five model consumers are `reproduce`,
  `diagnose`, `reproduce-informed`, `fix`, and `verify`. See
  [the bugfix workflow](../reference/bugfix-workflow.md).

A workflow's steps name an abstract role — `default`, or one of the auxiliary
roles — never a model. Which model answers to that role is your profile's
choice, made at launch, and you can override the role itself per step.

Steps that share a session share its conversation, and they may still run under
different policies. When a step needs a different `smol`, `slow`, or `plan`
binding than the one currently in effect, Ompire restarts that session's agent
process and resumes the same native session, so the context carries over. You
may see the session read as *starting* for a moment while that happens; it is
not a new conversation and not a failure.

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
