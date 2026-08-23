# Workflow engine

## Overview

A workflow is an ordered sequence of steps executed over a task's named
sessions. It is what turns "run an agent" into "run this procedure, collect
this evidence, and stop for a human when the evidence is missing".

Workflows are trusted Python definitions registered in the daemon by name. The
current built-in-only representation boundary is recorded in
[ADR-0018](../adr/0018-keep-built-in-workflows-in-python-until-portable-versioning-is-required.md).
There is no declarative format today — [`VISION.md`](../../VISION.md) calls for
versioned declarative workflows, and ADR-0018 defines when that direction
becomes a requirement rather than current behavior.

## Definitions

A workflow declares:

| Part | Meaning |
|---|---|
| `name` | Unique registry name; templates reference it |
| `sessions` | Slug-format session names, declared up front, unique per task |
| steps | Ordered, uniquely named, of four kinds |
| `primary` | Session targeted by task-scoped operations. Defaults to the first declared. |

Steps fall through to the next declared step on success. A `decision` step
routes explicitly. Falling off the end completes the run.

Validation happens **at daemon startup**, not at task runtime: duplicate step
names, an agent step naming an undeclared session, or a workflow declaring the
reserved session name `judge` are all rejected before the daemon serves.

Two workflows are registered: [`single-step`](#the-single-step-workflow) and
[`bugfix`](bugfix-workflow.md).

## States and behavior

### Run execution

After the spawn pipeline completes the workspace, the workflow named by the
task's template executes as a single sequential run — one step at a time, in
declaration order, with `decision` routes as the only jumps. At most one step
runs at a time per task.

Persisted per task: the workflow name, the run status (`running`, `waiting`,
`complete`, `failed`), the current step name, and one history row per executed
step carrying its sequence number, name, kind, session, status, parsed
outcome, error text, and timestamps.

A completed or failed run **keeps the workspace alive**. The task stays
`created` and its sessions stay live until cleanup, so the operator can
inspect or intervene.

A step that cannot be executed at all fails the run but is not registry-fatal:
the task's state stays `created` and its sessions are left alive for manual
intervention.

### Lazy sessions

A session is spawned on first use by an `agent` step, through the same
supervised-start path as any session — resolved model and thinking flags, the
ask-timeout preflight, the ready handshake, session-identity capture — and
stays alive until cleanup.

All of a task's sessions share the task's clone and container, so **the
working tree is the primary handoff channel between steps**.

A workflow with no `agent` steps starts no agent at all.

### Agent steps

An agent step builds its prompt from the run context — the task, the resolved
template, and prior step records — so prompts can incorporate earlier
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

A missing file, unreadable JSON, or a schema violation first triggers the
[LLM judge](#the-llm-judge). If the judge does not classify confidently, the
outcome is recorded as **null with a note** — never guessed — and that by
itself does not fail the step.

Missing evidence is data. Routing decisions downstream see the absence and can
act on it.

An outcome-bearing step that built an empty prompt records a null outcome
without reading the file and without invoking the judge: no outcome
instruction was given, so anything on disk is stale by definition.

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

A decision step evaluates a deterministic route function against the run
context. A returned declared step name continues the run there; a returned
completion sentinel finishes the run `complete`. The chosen route is recorded
as the step's outcome.

If the route function returns `None`, raises, or names a step that does not
exist, the [judge](#the-llm-judge) is invoked. If that does not confidently
resolve a route, **the daemon does not guess**: the run parks in a synthesized
gate naming the decision step and the resolution failure. Resuming continues
at the step declared after the decision.

### Gate steps

A gate parks the run in the persisted `waiting` status with an operator
message, broadcasts it, and classifies in the `notify` attention tier.

`POST /api/tasks/{id}/workflow/resume` records the operator's optional note as
the gate's outcome, finishes the gate `ok`, and continues. It responds `409`
when the run is not waiting and `404` for an unknown task.

A gate waits indefinitely. Re-notify aging applies to an unanswered gate as it
does to `waiting-input`.

Resuming a gate that is the last declared step completes the run — including
when the gate was re-armed by restart recovery.

### The LLM judge

The judge is a **fallback, never the router**, invoked at exactly two points:
an outcome-bearing agent step ending with a missing or malformed outcome file,
and a decision step whose route cannot resolve.

It runs as an engine-reserved session named `judge` in the task's container
and clone, spawned lazily through the normal supervised-start path and
surfaced in the session tracker, snapshot, and UI like any other session. Its
model comes from the optional `judge_model` config key, independent of the
task's template.

Each judgment is self-contained: the prompt names the step and its purpose,
references a daemon-written transcript tail at
`.ompire/judge-transcript-<seq>.jsonl` dumped best-effort from the judged
session's ring buffer, and instructs the judge to inspect the working tree.

The judge writes a standard outcome document — carrying `artifacts.route` for
a route judgment — and is instructed to **write no file at all when it cannot
classify with confidence**.

A judged outcome is recorded with an error-field note marking it
judge-synthesized. A judged route is validated against the declared steps plus
the sentinel before being accepted.

Judgments produce no step records of their own. A judge that fails to start,
crashes, or stays uncertain degrades to the no-judgment path — the null
outcome stands, or gate escalation proceeds. **Judging never fails a run.**

The `judge` session is admitted by session-scoped endpoints like a declared
one, because its transcript is the audit trail for judge-synthesized
outcomes. Arbitrary undeclared session names still return `404`.

### The single-step workflow

Sessions `('main',)`, primary `main`. One agent step named `work`, not
outcome-bearing, whose prompt is the template's preamble prepended to the
task's stored prompt separated by a blank line.

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

The resume nudge exists because the resumed session retains its context —
restarting the prompt would duplicate work. For outcome-bearing steps the
nudge re-states the outcome-file instruction.

A run that was `complete` or `failed` is never re-driven. Its sessions are
only resumed.

### Git exclusion

The clone step appends `.ompire/` to the clone's `.git/info/exclude`,
idempotently, so outcome files and judge transcripts never appear in `git
status`, diffs, reviews, or pull requests.

## Configuration

| Key | Effect |
|---|---|
| `judge_model` | Model for the judge session. Unset means the agent's default. |

## Interfaces

| Method | Path |
|---|---|
| `POST` | `/api/tasks/{id}/workflow/resume` |

Each step start and finish broadcasts `workflow_step` carrying the task id,
step name, kind, and status, with error text on failure.

The snapshot carries each task's workflow state, so reconnecting clients see
current runs without replaying events.

Templates validate their `workflow` value against this registry; unregistered
names are rejected with `422`.
