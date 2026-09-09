# Spawn a task

A task is one deliverable against a project: a bug fix, a change, an
investigation. Spawning a task creates its isolated workspace and starts its
workflow.

## What spawning does

Spawn runs these steps in order. Each publishes progress, and a failure leaves
the task in `failed` with the step name and its stderr attached.

| Step | Action |
|---|---|
| `fetch` | `git fetch` the project's [fetch remote](../reference/projects.md#fetch-remote) in its checkout, so the clone starts from current refs |
| `clone` | Local clone of the checkout into `task_dir_root/<project>/<slug>` |
| `branch` | Branch off `origin/<base>` using the accepted branch pattern — or off the reviewed commit, for a launch with handoff inputs |
| `inputs` | Install the accepted [handoff inputs](../reference/task-spawn.md#handoff-inputs). Runs only for a launch that has them |
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

- **a workflow** — any launchable entry in your [library](#write-your-own-workflow),
  packaged or your own, against any ready project;
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
command, a decision, a human gate, a review, a publication action — are shown
without one. A step a route can pass by, or that has its own condition, is
marked *conditional*. It is a list of what the run *may* do, not a promise
about the path it will take. Every model consumer is one of these rows; nothing
runs a model outside them.

It also states **what this workflow could publish**: which privileged effects
it declares and which decision would have to authorize each — or that it
publishes nothing, which is worth knowing before you launch rather than after.
Launching accepts the procedure, not permission to publish; that decision is
made later, against the real content.

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

### Start a task from an accepted result

When an earlier task produced a plan you accepted — an epic, a change proposal,
a piece of research — open its Results panel and select **Start task from this
result** on the revision you want. Spawn opens with that exact revision
attached and its project selected. Nothing has started yet: choose the
workflow, model profile, slug, and prompt as usual, then submit.

The **Handoff inputs** section lists the accepted revisions this project can
offer, so you can add another or remove the one you arrived with. Each one
shows the paths it will install, labelled **Handoff input — not publishable**.
Ompire copies those files into the new task's own clone before the agent
starts, and the agent reads them there — the producing task's workspace does
not have to still exist, and its container and sessions are not involved at
all.

If the files were captured against a different base than the one this task will
be built from, Ompire says so and lists what changed since, and you have to
tick the acknowledgement before submitting. That is a statement that the plan
has not been validated against this target, not a formality: a plan written
against a tree that has moved may no longer describe the code.

If a destination is not free — already tracked on the base, occupied by a
directory, or claimed by another attachment — the launch is refused and names
the path. Nothing is overwritten or merged. Remove that attachment, choose a
different accepted bundle, or pick another base, and review again.

An incompatible selection stays visible with its reason rather than
disappearing, so you always submit the set you can see.

When you later ship the code this task produced, the handoff files cannot ride
along: Ompire refuses a delivery whose Git result carries one, and says which
path is in the way. See [Ship flow](../reference/ship-flow.md#handoff-inputs).

If the `inputs` step fails — a path turned out to be occupied, or the base moved
under the launch — the task fails before any agent runs, and the failure names
what stopped it. The clone is left as it is so you can look at it. Clean the
task up, then use **Start another task** with a new slug: you get a fresh
preview against the base as it now stands.

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

You can also mention a path from an attached handoff input. Those files are not
on the base branch, but Ompire installs them into the clone before the agent
runs, so "implement @epics/demo/PLAN.md" resolves.

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

`result_attachments` pins accepted result revisions as
[handoff inputs](../reference/task-spawn.md#handoff-inputs). Each entry names
all three of the producing task, the revision, and the manifest identity the
operator accepted — the manifest id is what makes it an exact revision rather
than a pointer that a successor capture could move:

```json
{
  "result_attachments": [
    {
      "producer_task_id": 12,
      "result_id": "res_1f0c…",
      "expected_manifest_id": "9a3e…"
    }
  ],
  "acknowledge_result_base_difference": false
}
```

Destinations come from the revision's own manifest; the request cannot choose
where files land. Send `acknowledge_result_base_difference: true` when the
preview reports a `different` or `unknown` base comparison — without it the
launch is refused. The preview echoes `source_commit` (the exact commit the
clone will be built from), `result_attachments` with their destinations, and
`base_comparisons`; the preview token covers all of it, so a changed selection,
a moved base, or a replaced revision invalidates the review.

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
itself: saving a new revision, archiving the entry, or upgrading Ompire changes
what your *next* task runs, not one already in flight. You can see exactly what a task was accepted with, per
consumer, on its detail view — including the revision, with the definition
itself readable there.

## Workflows

Two workflows ship as read-only examples, and both are available to every
project:

- `single-step` — one agent step. The agent works, you review, you ship.
- `bugfix` — QA tries to reproduce, a coder diagnoses, QA tries again with
  those findings if the first attempt failed, then fix and verify. Routing is
  decided by explicit rules over declared results; where the evidence does not
  decide, the run stops and asks you, offering the answers the workflow
  declares rather than a bare Resume. Its five model consumers are `reproduce`,
  `diagnose`, `reproduce-informed`, `fix`, and `verify`. See
  [the bugfix workflow](../reference/bugfix-workflow.md).

You add your own in **Workflows** — see below.

A workflow's steps name an abstract role — `default`, or one of the auxiliary
roles — never a model. Which model answers to that role is your profile's
choice, made at launch, and you can override the role itself per step.

Steps that share a session share its conversation, and they may still run under
different policies. When a step needs a different `smol`, `slow`, or `plan`
binding than the one currently in effect, Ompire restarts that session's agent
process and resumes the same native session, so the context carries over. You
may see the session read as *starting* for a moment while that happens; it is
not a new conversation and not a failure.

## Write your own workflow

Open **Workflows**. There is no daemon release and no restart in this loop.

1. **Start from something.** *New workflow* opens a minimal format-3 example
   that publishes nothing;
   *Duplicate* copies a saved revision — including a packaged one — under a name
   you choose; *Import YAML…* reads a local file into the editor. You can also
   paste. The name is permanent and cannot collide with a built-in or with an
   archived name.
2. **Edit it.** *Visual* gives you an agents panel and a list of step cards;
   *YAML* gives you the text. They are two views of one draft, and switching
   between them is neither a save nor a launch. Save a draft whenever you like:
   drafts take any text, valid or not, and survive a refresh and a daemon
   restart. Saving one never changes what the workflow would launch.
3. **Validate.** You get either the revision identity and a readable reading of
   the definition, or the location and reason of the problem. In the visual
   editor the reason also appears at the field it is about, with a link that
   opens the card even if it is collapsed. Editing after that marks the result
   out of date — validate again. Validation is structural: it does not say the
   commands exist or the run will succeed.
4. **Save an executable revision.** This validates the text again, retains it,
   and makes it what a new launch of this name pins. Until you do, the entry is
   *draft only* and cannot be launched at all. Saving starts nothing.
5. **Launch it.** *Launch in Spawn* carries the workflow into the ordinary
   Spawn form; you still choose the project and the profile and review the
   resolution before submitting. The preview shows the exact revision you are
   accepting and the whole procedure it declares, alongside the model each step
   would use.

### Building one without writing YAML

Everything either format supports has a form control, so you never have to open
the text editor to write a branching workflow.

- **Agents** names the conversations. A step assigned to an agent that ran
  earlier continues *that* conversation, which is how QA verifies a fix in the
  session where it reproduced the bug. Which model an agent uses is chosen at
  launch, not here.
- **Step cards** carry the kind, the assigned agent, the abstract model role,
  the instruction, the results the step must declare, the evidence it is
  handed, and what happens next. Add, move, and remove cards with buttons; the
  card order is execution order, and moving one tells you which step it now
  continues to.
- **Instructions** are built from parts: words you write, an explicit reference
  to something from the run, or a conditional section. There is no template
  language and nothing to type in an expression box.
- **Decisions** are ordered condition/destination rows with an explicit
  fallback. **Gates** are a question plus the named answers you offer, each
  with its own destination and an optional requirement that the person writes
  a reason.
- **Visit bounds** are declared with the gate they reach when they run out, so
  a loop that stops asks somebody rather than ending quietly.
- **Review** is a card with no instruction and no model: independent review of
  what the task would publish. Its verdict — approved, comments, aborted,
  error, interrupted — is evidence the steps after it route on, so you decide
  what a comments verdict does rather than the daemon deciding for you.
- **Publication** is one card per effect: a local signed commit, a push, a pull
  request. Each names the approval that can permit it and the action whose
  result it consumes.

### Ending a workflow with a publication

A workflow publishes nothing until you say how. Four cards:

1. A **review** step, reading the work.
2. A **gate** whose *delivery binding* names which of its own evidence aliases
   is that review. That binding is what makes the decision about the content
   the reviewer actually read. Here you can also write the commit message and
   pull-request text the workflow suggests — built from the run's own evidence,
   and editable by the operator before anything is published.
3. One **answer** on that gate that names the exact actions it authorizes:
   `commit`, then optionally `push`, then optionally `pr`. There must always be
   another answer that publishes nothing.
4. The **delivery** cards themselves, in that order, the last of which ends the
   run at a named result.

The editor and the daemon check the shape for you: an answer must go straight
to the first action it grants, every action must name the same approval and its
own predecessor, and nothing else in the workflow may route into the chain. If
you want two endings — say "commit locally" and "open a pull request" — write
two answers with two separate chains. An answer authorizes the actions it
names, never publication in general, and never grows into a longer ending
afterwards.

Renaming a step or an agent moves the routes, selectors, and assignments that
name it, and leaves instructions and literal values alone. Removing a step
lists what names it first, and if you remove it anyway those references stay
visible as broken ones instead of being quietly repaired.

Two things are the same in both views and editable in neither. The workflow's
**name** is the entry's identity — renaming means creating a separate workflow
— and its **format** is the rules it is read under, so an existing format-1 or
format-2 workflow keeps being read under its own format rather than being
upgraded. That includes not being able to publish: review and delivery steps
exist only in format 3, so to publish from an older workflow, duplicate it into
a new one and add them.

Once you change something visually, saving rewrites the document: its layout is
normalized and YAML comments are dropped. What it means does not change.
Download the draft first if you want to keep your own formatting. Text that
cannot be parsed at all stays in the YAML editor with its line and reason — the
visual editor refuses to open rather than replacing your work with an empty
document.

The grammar is documented in
[Workflow definitions](../../develop/reference/workflow-definitions.md), and the
library's lifecycle in
[Workflow engine](../reference/workflow-engine.md#the-workflow-library).

### Keep, share, and retire one

Export a saved revision to get a standalone YAML file, verified to load back to
the same revision. Re-importing it unchanged under the same name is the same
procedure and reuses the same revision; under a *different* name it is a
different document with its own revision. Comments and formatting are not
preserved — download the draft instead if you want your own text back.

Archive removes a workflow from launch choices without deleting its draft, its
revisions, or the tasks that ran them; Restore puts it back. Built-ins cannot be
edited or archived — duplicate them instead.

### When something goes wrong

**"was edited elsewhere"** — another tab or another browser saved first. Nothing
of yours was written and your text is still in the editor. Copy or download it,
reload the saved version, and reapply your change. There is no merge and no
overwrite: a workflow whose YAML differs from another tab's only in a comment is
still a different edit.

**"the workflow changed since it was previewed"** — you saved a new executable
revision while a Spawn preview was open. Review the new resolution and submit
again. Everything you typed is kept, and any per-step model overrides are
cleared with a notice, because the steps may have moved.

**A workflow marked unavailable in Spawn** — it was archived, has no executable
revision yet, or its saved revision cannot be read. The selection stays visible
with the reason rather than being swapped for another one. Fix it in the
library: restore it, or save a corrected executable revision. Tasks already
running under it are unaffected either way.

### Follow it once it is running

Task detail shows the procedure the task accepted with what happened laid over
it, read from the revision the task pinned rather than from what the workflow
name means today. Editing or archiving the entry afterwards changes neither.

Selecting a step shows each visit separately — a rejected verification and the
corrected one are both there — with the result it declared, the evidence it was
handed as links to the exact producing attempts, the answer somebody gave at a
gate and the reason they wrote, and a link into that step's conversation. A
step nobody reached stays visible as work the procedure allows, not as work
that succeeded, and where the run recorded nothing the page says so instead of
guessing.

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
