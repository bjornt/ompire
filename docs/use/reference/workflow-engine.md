# Workflow engine

## Overview

A workflow is an ordered sequence of steps executed over a task's named
sessions. It is what turns "run an agent" into "run this procedure, collect
this evidence, and stop for a human when the evidence is missing".

A workflow definition is a **document**, not code. It is a bounded YAML
subset, normalized into one canonical form and identified by the SHA-256 of
those bytes — its *revision*. A task pins one revision when it is accepted and
executes that exact revision for the rest of its life
([ADR-0028](../../adr/0028-retain-declarative-workflow-revisions.md)). The
exact grammar is a contributor reference:
[Workflow definitions](../../develop/reference/workflow-definitions.md).

Two definitions ship with the daemon: [`single-step`](#the-single-step-workflow)
and [`bugfix`](bugfix-workflow.md). This release installs only packaged
definitions — there is no library, no import, and no way to add one without a
daemon release.

## Definitions and revisions

A definition declares:

| Part | Meaning |
|---|---|
| `format` | The document format *and* its interpretation. Currently `1`. |
| `name` | Unique installed name; a launch selects it |
| `sessions` | Slug-format session names, declared up front, unique per task |
| `primary` | Session targeted by task-scoped operations |
| steps | Ordered, uniquely named, of four kinds. An `agent` step also declares the abstract model role it consumes. |

Steps fall through to the next declared step on success. A `decision` step
routes explicitly. Falling off the end completes the run.

### What a revision pins, and what it does not

The revision covers everything that decides what a run *does*: every prompt and
gate message, every route, every command and timeout, the declared sessions and
which one is primary, the outcome contracts, and the visit bounds. Change any
of them and the revision changes. YAML comments and the order of mapping keys
do not change it; the exact text of a prompt and the order of a sequence do.

It does **not** pin model responses, binaries, credentials, tool versions, or
the contents of your working tree. It pins the procedure, not the world.

A retained revision keeps its meaning. `format: 1` fixes both the grammar and
how it is interpreted, so a document retained today is read under today's rules
forever. A daemon that meets a format it does not implement refuses it visibly
rather than reading it under the newest rules.

### Where a revision comes from, and where it is used

New tasks pin the currently installed revision of the name they select.
Everything afterwards — execution, restart recovery, which sessions are
addressable, which session review and shipping attach to, and what task detail
shows — resolves through *that task's* revision. The workflow's name is looked
up in the installed catalog for exactly two prospective questions: what a new
launch would pin, and what an old task is offered as a continuation candidate.

So a daemon release that edits `bugfix` does not change a `bugfix` task that is
already running. Both revisions execute at once, and both stay readable: the
retained document is kept whole, not just its identifier, and stays available
after the packaged definition changes and after the workflow name disappears
from a later release's catalog.

Packaged definitions are validated at **daemon startup**. A malformed built-in
prevents the daemon from serving rather than failing a task later.

## States and behavior

### Run execution

After the spawn pipeline completes the workspace, the workflow the task was
accepted with executes as a single sequential run — one step at a time, in
declaration order, with `decision` routes as the only jumps. At most one step
runs at a time per task.

Persisted per task: the workflow name and its pinned revision, the run status
(`running`, `waiting`, `complete`, `failed`), the current step name, and one
history row per attempt carrying its sequence number, name, kind, session,
status, parsed outcome, error text, any uncertainty pause, and timestamps.

A completed or failed run **keeps the workspace alive**. The task stays
`created` and its sessions stay live until cleanup, so the operator can
inspect or intervene.

A step that cannot be executed at all fails the run but is not registry-fatal:
the task's state stays `created` and its sessions are left alive for manual
intervention.

### Lazy sessions

A session is spawned on first use by an `agent` step, through the same
supervised-start path as any session — the consumer's complete model policy,
the ask-timeout preflight, the ready handshake, session-identity capture — and
stays alive until cleanup, though its underlying process may be replaced to
apply a different policy (see [Model policy per
turn](#model-policy-per-turn)).

All of a task's sessions share the task's clone and container, so **the
working tree is the primary handoff channel between steps**.

A workflow with no `agent` steps starts no agent at all.

### Agent steps

An agent step builds its prompt from the run context — the task, its accepted
launch inputs, and prior step records — so prompts can incorporate earlier
outcomes. An empty built prompt sends nothing and the step completes when the
session is ready.

The step ends at the session's debounced idle transition.

When a step declares `expects_outcome`, the prompt is suffixed with a fixed
instruction block naming `.ompire/outcome.json` and its schema, and the daemon
**unlinks any pre-existing outcome file before sending the prompt**, so a
stale file from an earlier step can never be mistaken for this one's result.

### The outcome document

`<clone>/.ompire/outcome.json`, read host-side after the turn ends:

| Field | Required | Type |
|---|---|---|
| `version` | yes | integer, must be `1` |
| `status` | yes | `"success"` or `"failed"` |
| `summary` | yes | string |
| `artifacts` | no | string-keyed map of workflow-defined handoff values |

A missing file, unreadable JSON, or a schema violation on a step that asked for
one **pauses the run** — see [Uncertainty pauses](#uncertainty-pauses). The
attempt keeps its absent outcome and the reason it was rejected. Nothing is
guessed and nothing is synthesized.

A `"failed"` outcome is not a missing one. It is a real, declared result, and
it follows the definition's own routes.

An outcome-bearing step whose prompt renders empty records a null outcome
without reading the file and without pausing: no outcome instruction was
given, so anything on disk is stale by definition, and nothing was asked.

### Command steps

A command step runs its argument vector via `workshop exec` in the task's
clone — argument list, no shell — bounded by the step's timeout, recording the
exit code and a captured output tail as its outcome.

**A non-zero exit is outcome data, not a failure.** The step finishes `ok` and
routing on the exit code is a following `decision` step's job. Only the
inability to execute at all — container gone, `workshop exec` itself failing,
timeout — fails the step and the run.

Command steps must be idempotent: a step interrupted by a daemon restart is
re-run on recovery.

### Decision steps

A decision step chooses where the run goes next from the evidence already
recorded. A case naming a declared step continues the run there; a case naming
completion finishes the run `complete`. The chosen route is recorded as the
step's outcome.

It evaluates its declared cases in order and takes the first that
is **true**. Predicates are three-valued: a missing operand or a type mismatch
is *unresolved*, not false. An unresolved case stops the run right there — it is
deliberately not skipped in favour of a later case, because "this rule could
not be applied" is not "this rule does not apply". A definition may also
declare `otherwise` as a pause, which is the author saying "ask a person"
rather than inventing a destination.

Either way the run [pauses](#uncertainty-pauses) rather than routing.

### Gate steps

A gate parks the run in the persisted `waiting` status with an operator
message, broadcasts it, and classifies in the `notify` attention tier.

`POST /api/tasks/{id}/workflow/resume` records the operator's optional note as
the gate's outcome, finishes the gate `ok`, and continues. The request names
the waiting attempt's sequence number, so a stale browser tab or a double
submit is refused rather than applied to whatever the run is waiting on now. It
responds `409` when the run is not waiting or the attempt has moved on, and
`404` for an unknown task.

A gate waits indefinitely. Re-notify aging applies to an unanswered gate as it
does to `waiting-input`.

Resuming a gate that is the last declared step completes the run — including
when the gate was re-armed by restart recovery.

### Uncertainty pauses

There is no LLM judge. When the evidence a step or a route needs is missing or
unreadable, the run stops and says what it was waiting for.

A pause is not a gate, and the UI keeps them apart. A **gate** is the
definition asking a person to look; resuming finishes it and the run continues
at the gate's fall-through. A **pause** is the engine refusing to guess; the
action retries the step that could not be decided, and it never continues past
it.

Four things pause a run:

| Reason | What happened |
|---|---|
| `missing_outcome` | A prompted step that requires a result left none that could be read |
| `unresolved_decision` | A route could not be decided from the recorded evidence, or the definition declared a pause for this case |
| `prompt_unrenderable` | A prompt or gate message referenced a value that is missing and has no declared fallback |
| `condition_unresolved` | A step's own condition could not be decided |

None of these is a *negative result*. A `"failed"` outcome, a nonzero command
exit, and a declared no-match all follow the definition's routes normally.

The paused attempt keeps everything it had: its own kind, its absent outcome,
and the parse or evaluation error. Nothing is written that could later read as
a result. The run status is `waiting`, and the record and the run are marked
together so a restart cannot find one without the other.

**Retrying** opens a *new attempt of the blocked step*. It never falls through
as if the missing evidence had been accepted, and it never edits what was
recorded. If the evidence is still unreadable, the run pauses again — that is
the honest answer, not a failure of the retry.

A retried agent step is told, before its original instruction, that the
previous attempt left no valid result and that files may already have changed:
inspect the working tree and finish what is missing rather than repeat it. That
is deliberately not the restart nudge — the previous turn ran to completion.

A retried decision re-reads exactly the same recorded evidence. There is no way
to edit an outcome or a transcript to make it resolve; if it did not decide
before, it will not decide now, and the UI says so.

A pause survives a restart. Recovery re-arms it exactly as persisted: no
prompt, no automatic retry, no second attempt. Leaving it waiting indefinitely
is fine, and so is stopping the task or cleaning it up instead.

Retrying is a human decision, but it is not a way past a bound the definition
set. A retry counts against the step's declared visit bound like any other
attempt, and once that bound is spent the retry sends the run to the step's
declared exhaustion gate instead of opening another attempt.

### The single-step workflow

Sessions `('main',)`, primary `main`. One agent step named `work` on role
`default`, not outcome-bearing, whose prompt is the accepted preamble prepended
to the task's stored prompt separated by a blank line.

The preamble alone is never sent for an empty prompt; the step completes once
the session is ready and the session lands `idle`.

Operator-visible behavior matches the pre-workflow daemon exactly, including
byte-identical prompt construction. Review, ship, composer actions, and escape
hatches all operate on session `main`.

### Restart recovery

Run state survives restarts. On startup, after session resumes, a task whose
run was `running` or `waiting` resumes at its persisted current step, by kind:

| Kind | On recovery |
|---|---|
| `agent`, prompt not yet sent | Send the prompt fresh |
| `agent`, prompt sent | Send a fixed resume-nudge once, continue to the turn boundary |
| `command` | Re-run |
| `decision` | Re-evaluate against persisted records |
| `gate` | Re-arm the waiting state and re-broadcast |
| paused | Re-arm the pause exactly as persisted — no prompt, no automatic retry |

Recovery re-drives the attempt that was already open rather than closing it and
appending another. A restart is not a work attempt, so it costs nothing against
a step's declared visit bound.

The pinned definition is resolved and validated *before* any session is resumed
or any step is chosen. A revision that is absent, damaged, or written for a
format this daemon does not implement stops recovery for that task alone: no
session is resumed, no prompt is sent, and nothing is published. The task keeps
its position, its workspace, and its history, and task detail says why. Other
tasks are unaffected.

Only sessions the pinned definition declares are resumed. The retired `judge`
session in an older task is left alone — nothing will prompt it again — while
its transcript and last applied policy stay on record.

The resume nudge exists because the resumed session retains its context —
restarting the prompt would duplicate work. For outcome-bearing steps the
nudge re-states the outcome-file instruction.

Each session is resumed on **the policy it last actually ran**, recorded on the
session itself — not on the task's first step's policy, and not on today's
profiles. Two steps sharing a session can pin different bindings, so only the
session knows which one took effect. A session with no recorded policy is left
unresumed with the reason stated, rather than restored under a policy nobody
chose; the engine will spawn it fresh if the run needs it. A step interrupted
before its prompt went out is the one exception: its accepted binding is the
decision the run is about to make anyway.

A run that was `complete` or `failed` is never re-driven. Its sessions are
only resumed — on that same last-applied policy, which is what a follow-up,
review feedback, or ship drafting then continues with.

### Git exclusion

The clone step appends `.ompire/` to the clone's `.git/info/exclude`,
idempotently, so outcome files never appear in `git status`, diffs, reviews, or
pull requests. Transcripts left by the retired judge in older clones are
covered by the same exclusion.

## Configuration

The engine has no model configuration of its own, and no model consumer of its
own. Every model a run uses belongs to a declared agent step and comes from the
task's accepted bindings. The retired `judge_model` key configures nothing —
there is no judge to configure (see
[Configuration](configuration.md#retired-keys)).

## Interfaces

| Method | Path |
|---|---|
| `POST` | `/api/tasks/{id}/workflow/resume` |
| `GET` | `/api/workflows` |
| `GET` | `/api/workflows/revisions/{revision}` |

`resume` advances a waiting run. It names the waiting attempt's sequence
number, and the daemon decides from the waiting record whether that means
resuming a declared gate or retrying a paused step.

Each step start and finish broadcasts `workflow_step` carrying the task id,
step name, kind, and status, with error text on failure and the pause document
when the run stopped rather than deciding.

The snapshot carries each task's workflow state, so reconnecting clients see
current runs without replaying events. Each task also carries its pinned
revision, whether that revision can currently be resolved, and the primary
session *its* definition declares.

`GET /api/workflows` returns the installed catalog — each definition's current
revision and format, its sessions, and every declared step with its kind,
session, abstract role, and whether a route or its own condition can pass it
by. Every model consumer is one of those steps; nothing is described outside
them. The same catalog rides in the WebSocket snapshot. There is no CRUD and no
change event: installed definitions change only when the daemon does.

`GET /api/workflows/revisions/{revision}` reads one retained definition by
content identity — deliberately not by name, because a name says what a *new*
launch would get and this answers "what did that task accept". An unknown
revision is `404`; a stored document that cannot be read comes back as a
classified `409` rather than being executed to answer a read.

A launch validates its `workflow_name` against the installed catalog; an
unknown name is rejected with `422`.

### Model policy per turn

A step declares an abstract role, never a model. Which model answers to that
role is the launch's choice, pinned onto the task at acceptance — one complete
binding for every declared agent step, and none for anything else — and read
from there, including after a restart and after the source profile is edited or
deleted. Overriding a
step's profile or role is a launch-time choice; an accepted task's policy does
not change.

Every omp process receives a complete policy: the consumer's active pair as
`--model`/`--thinking`, and all three auxiliary roles as `--smol`, `--slow`,
and `--plan`, each with its own thinking level.

Because two steps can share one session and still differ, the policy is applied
**before every declared turn**, at the turn boundary the engine already owns:

| Situation | What happens |
|---|---|
| Policy unchanged | The process is kept, and its active pair is still re-asserted and read back — a cached process is not evidence that it is on the accepted model |
| Only the active pair differs | The process is kept and reconfigured over omp's acknowledged `set_model` / `set_thinking_level`, then read back and compared exactly |
| Any of `smol`, `slow`, `plan` differs | omp has no setter for those — they are start-time flags — so the session's native id is captured, the process is stopped gracefully, and a replacement is started with `--resume` and all four new pairs |

A replacement keeps the same logical session, the same native session id, and
its transcript; the conversation carries over. The session reads as *starting*
briefly and the transcript channel reconnects. It is not a new conversation and
not a failure.

A policy change never interrupts work: if the session is streaming, compacting,
holds queued messages, or has an unanswered question, the transition is refused
as a step-infrastructure failure rather than aborting the turn.

The verified policy is recorded on the session before the turn that depends on
it. A refused configuration, a timeout, a resumed session whose identity does
not match, or a failed record leaves no process that may be prompted: the step
fails with the workspace and history intact.

Repeated visits to a step use that step's accepted binding again. Follow-ups,
review feedback, and ship drafting continue on the session's last applied
policy until another declared consumer takes it over.
