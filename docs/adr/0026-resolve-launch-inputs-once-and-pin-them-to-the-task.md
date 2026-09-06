# ADR 0026: Resolve launch inputs once and pin them to the task

- Status: Accepted
- Date: 2026-09-05

## Context

Starting a task used to require a template: a saved, mutable preset naming a
project, a base branch, a branch pattern, a workflow, a Workshop additions
source, a preamble, and an omp model and thinking level. An operator who
wanted to run the bugfix workflow against a project had to create a template
first, and running two workflows against one project meant maintaining two
presets that differed in one field. That is repeated setup standing between
the operator and the rigorous path the product exists to make fast.

Templates also owned model policy, which
[ADR-0025](0025-store-global-model-profiles-separately-from-launch-policy.md)
had already moved elsewhere in principle: a global model profile binds four
abstract roles — `default`, `smol`, `slow`, `plan` — to concrete
provider-qualified models with explicit thinking levels. Profiles existed and
were saved, but nothing consumed them; execution still read a template's
single fuzzy model name, and the workflow engine's LLM judge read a separate
`judge_model` key from `config.toml`. Three places described what model runs,
and none of them was the one the operator had configured.

Meanwhile the effective values of a running task were not durable.
[ADR-0010](0010-separate-projects-templates-and-task-snapshots.md) proposed a
task snapshot for exactly this reason and was never able to move past
`Proposed`: recovery, review, and shipping all re-read the current template,
so editing a preset could retroactively change the base branch a task in
flight would be reviewed and published against. Spawn-time model and thinking
overrides lived only in a background job's arguments and did not survive a
restart at all. Tasks that predated templates fell back to `main`.

The question this record answers is where launch policy lives once templates
are gone, and what happens to work that was already accepted.

## Decision

**A launch selects a workflow, a project, and a model profile directly.**
Every registered built-in workflow is available to every checkout-ready
project. There is no saved preset between the operator and the launch, and no
hidden generated one: the creation contract names the three things being
chosen. A project's default model profile is optional and inherited; an
explicit task profile replaces that inheritance.

**Workspace and prompt defaults belong to the project.** Base branch, branch
pattern, Workshop additions source, and standing preamble are project columns
again — as *defaults a launch inherits*, not as a saved way of working. Each
is independently overridable for one task. This is not a return to the
pre-template arrangement it superficially resembles: what made per-project
defaults wrong before was that they were the only way to describe a way of
working, which is now what the workflow and the profile do.

**A workflow step declares an abstract role, never a model.** Definitions ship
with the daemon ([ADR-0018](0018-keep-built-in-workflows-in-python-until-portable-versioning-is-required.md))
and say which of the four roles each agent step consumes; which concrete model
answers to that role is the operator's launch-time choice. The engine's judge
is not an exception with its own setting: it consumes the task profile's
`slow` binding, disclosed in the preview beside every declared step.

**Preview and acceptance are one consistency boundary.** The same module
resolves both, so what the operator reviews and what is stored cannot be two
different readings of the same selections. Acceptance re-resolves under a
`BEGIN IMMEDIATE` write reservation, compares a deterministic fingerprint of
the reviewed resolution against what the rules now produce, and inserts the
task with its inputs before releasing the lock. A change in between refuses
creation and presents the changed choices; it never retries under settings
nobody looked at. The fingerprint covers this launch's own inputs and the
workflow descriptor, not a global settings clock, so editing an unrelated
profile does not invalidate a review.

**The resolution is pinned to the task and never recomputed.** One
version-tagged JSON document on the task row carries the four role bindings,
each step's role, the judge binding, the effective workspace values with their
inheritance attribution, the rendered branch, and the project-derived checkout
path, fetch remote, and upstream/fork routing. Everything downstream — the
spawn pipeline, mention validation, the workflow engine, lazy session spawn,
the judge, restart recovery, review, and every ship entry point — reads that
document. Editing a project or a profile, or deleting a profile nothing
references, changes the next launch and nothing else. The source profile name
is retained as provenance, not as a live foreign key.

**Every native process receives the whole policy, and it is verified.** An omp
child is started with the active pair *and* all three auxiliary role pairs,
each carrying its own thinking level. A resumed process, which restores its
model settings from its own session file, has the accepted active pair
re-asserted over omp's acknowledged `set_model` / `set_thinking_level`
controls before any prompt or resume nudge. The child's active model is then
read back and compared exactly: omp fuzzy-matches `--model`, so "it started"
is not evidence that it obeyed. A mismatch kills the child and fails the step
rather than sending a turn under a substituted model.

**Missing history is stated, never invented.** The migration copies every
template row, every task's template attribution, and any explicitly configured
`judge_model` into inert evidence before the live storage goes away. Where all
of a project's templates agreed on a field, the value becomes the project
default; where they disagreed, every distinct candidate is preserved with its
source and the project cannot launch until the operator chooses. No
`model_profiles` row is invented: one old concrete pair cannot answer for four
roles, and an omp fuzzy name is not a provider-qualified identifier. A task
created before pinned inputs keeps all of its records and gains none: its
model, thinking level, preamble, and overrides were never persisted, so it is
marked as needing a confirmed continuation configuration, and everything that
would need those values — automatic recovery, prompts, review, publishing —
refuses until the operator confirms one. Confirmation pins what happens next
and makes no claim about the turns already taken. There is no `main` fallback
anywhere.

This record supersedes the template ownership proposed in ADR-0010 and takes
over its task-input invariant: no later stage may recover an existing task's
authority-bearing or execution-significant facts by re-reading mutable
configuration. ADR-0025's profile registry is unchanged; this record is the
execution handoff it deferred. [ADR-0013](0013-layer-daemon-writable-settings-over-operator-configuration.md)'s
boundary holds — `judge_model` is retired by being read and reported, never by
the daemon rewriting the operator's TOML.

## Extension: the workflow definition is a pinned input too

[ADR-0028](0028-retain-declarative-workflow-revisions.md), 2026-09-06.

This record pinned everything a launch resolves *except* the procedure itself:
the accepted document named a workflow, and a name resolves to whatever is
deployed. ADR-0028 closes that hole without changing the boundary here. The
resolved inputs now carry a workflow *revision* — the content identity of the
exact definition — resolved by the same `resolve_launch`, compared under the
same write reservation, and stored in the same document. It is covered by the
launch fingerprint, so editing a prompt or a route invalidates a reviewed
preview exactly as editing a profile does, while an unrelated change does not.

The nullable-inputs rule extends the same way. A task accepted before revisions
were retained has a null workflow binding, and that null is a real state — the
definition it ran was never recorded and cannot be reconstructed — filled in
only by an explicit operator confirmation through this record's existing
task-configuration path, never by looking the name up in today's catalog.

The engine-reserved auxiliary consumer this record described is gone. There is
no implicit judge, so there is no consumer outside the declared steps; a launch
request still naming it is refused with a field-level error rather than having
its choice dropped, and the retired `judge_model` evidence and acknowledgement
history are preserved as the record of what used to be configured.

## Consequences

Launching is three explicit choices and nothing else, and the same project
runs any workflow without setup. Model policy has one home: a profile, chosen
once and reused, with every consumer of a model — including the judge —
visible before launch instead of implied by a setting elsewhere.

A reviewed preview is worth something, because acceptance runs the same rules
and refuses when they no longer produce what was reviewed. The accepted cost
is a second resolution on every launch and a token the client must carry.

Task history becomes explainable and stable. A restart, a profile edit, or a
profile deletion cannot change what a task runs, what branch it is reviewed
against, or where it publishes. The cost is deliberate denormalization: the
role bindings and the project's routing are copied onto the task, and a new
field that affects later behavior must be classified explicitly as pinned or
proven presentation-only. Credentials, signing keys, and executable
preferences are never copied — those stay live reads at the moment of use
([ADR-0011](0011-keep-review-and-publishing-authority-outside-agent-sandbox.md),
[ADR-0015](0015-keep-agent-credentials-behind-narrow-brokers.md)).

Upgrading is not silent. An installation whose templates disagreed, or that
pinned a model, or that still sets `judge_model`, gets a project it cannot
launch until it answers a question — and a task from before this change gets a
task it cannot continue until it answers another. That friction is the point:
the alternative is a launch under values nobody chose. Unrelated projects and
tasks keep working throughout, archived history needs no confirmation, and the
evidence is kept afterwards so an unselected preamble or candidate is not lost.

Native model failures surface as failures. A model the provider does not have,
a profile naming a retired id, or an omp that will not answer its state query
now stops the step instead of quietly running something else. `auto` and `max`
still resolve to model-dependent levels, and the accepted policy is kept
distinct from the level omp reports, so normalization does not read as a lost
override.

The Workshop additions selection finally applies. my-workshop resolves
additions local-first with no source flag, so honoring an exclusive choice
requires staging the selected source at the clone's additions path around the
launcher and restoring the clone afterwards — including restoring absence. An
absent selected source is an explicitly empty additions file, which is what
prevents the launcher falling through to the source that was not chosen. The
cost is a bounded, daemon-owned staging step with crash recovery; the
alternative was a selection that silently did nothing.

Revisit this if workflows become versioned artifacts whose exact version is
retained per task, if per-step model overrides make a single "active pair"
insufficient to describe a run (the next epic child extends the same pinned
document rather than replacing it), or if an external service can provide
transactional immutable task resolution.

> **Extended by [ADR-0027](0027-hand-off-model-policy-between-turns.md).** Per-step
> overrides did arrive, and a single active pair is no longer sufficient: the
> pinned document now carries one complete binding per model consumer, and a
> session records which policy it last verifiably ran. Resolution still happens
> once, at acceptance, under the same reservation and fingerprint. Nothing in
> the record above is withdrawn.

## Alternatives considered

### Keep templates and add a profile field

Adding `model_profile` to the template would have made profiles govern
execution with far less work. It was rejected because it leaves the actual
cost in place: the operator still creates a preset before their first task,
still maintains one preset per workflow, and still has a mutable row that
recovery and shipping can re-read. It would also have preserved two answers to
"what model runs" — the template's own `model` column and the profile — which
is the ambiguity this change exists to remove.

### Resolve at execution instead of at acceptance

Reading the project and profile when each step runs would need no stored
document and would let a correction to a profile reach a task already in
flight. It was rejected because that is the same retroactivity ADR-0010
identified: a task's base branch, routing, and model would change under it
after a restart or an edit, and its history could not be explained. Making a
correction reach a running task is a deliberate operation, not a side effect
of editing reusable configuration.

### Treat the migration's candidates as executable defaults

The migration could have picked the first template's values, or synthesized a
profile from an old `model`/`thinking` pair, and left every project
immediately launchable. It was rejected because both are fabrications. The
first template is not "the" template, and one concrete pair says nothing about
the other three roles — a synthesized profile would put the operator's name on
a choice they never made. Evidence is kept inert precisely so it cannot become
a second source of launch policy.

### Let a task without pinned inputs fall back to project defaults

Recovering, reviewing, and shipping a pre-upgrade task using today's project
values would have avoided any blocked task. It was rejected because those
values are not evidence of what the task used: the operator may have set a
spawn-time override that was never persisted, and the project may have been
edited since. Reviewing or publishing against a guessed base branch is a
silent way to review the wrong diff or open the wrong pull request.

### Pass a source flag to my-workshop

Selecting the additions source on the launcher's own command line would have
avoided staging entirely. It was rejected because no such flag exists: the
launcher resolves a project-local `workshop.my.yaml` over the operator's
global file with no way to say which one is wanted. Leaving the argv alone
would silently ignore a `global` selection in any repository that ships its
own additions, and silently apply the global file whenever a `project`
selection had none.
