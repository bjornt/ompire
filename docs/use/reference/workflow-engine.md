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

Two definitions ship with the daemon as read-only examples:
[`single-step`](#the-single-step-workflow) and [`bugfix`](bugfix-workflow.md).
You add your own in the [workflow library](#the-workflow-library) — no daemon
release, and no restart.

## Definitions and revisions

A definition declares:

| Part | Meaning |
|---|---|
| `format` | The document format *and* its interpretation: `1` or `2`. |
| `name` | The library entry's name; a launch selects it. Permanent — renaming means creating a separate workflow |
| `sessions` | Slug-format session names, declared up front, unique per task |
| `primary` | Session targeted by task-scoped operations |
| steps | Ordered, uniquely named, of four kinds. An `agent` step also declares the abstract model role it consumes. |

Steps fall through to the next declared step on success. A `decision` step
routes explicitly, and in format 2 a `gate` step's chosen answer routes too.

Two formats are installed and both execute. **Format 1** is frozen: a
definition retained under it is always read under its original rules, so a task
accepted years ago keeps meaning what it meant. **Format 2** adds declared
results, recorded evidence, and gates with named choices, and removes the two
places format 1 left meaning implicit. `single-step` is format 1;
[`bugfix`](bugfix-workflow.md) is format 2.

| | Format 1 | Format 2 |
|---|---|---|
| Agent result | `status: "success" \| "failed"` plus an untyped artifact bag | a [declared result](#declared-results-format-2) with required artifact fields |
| Reading prior attempts | `latest`, re-scanned on every evaluation | [evidence bound once](#evidence-format-2) at attempt entry and recorded |
| Gate | Resume, with an optional note | [named choices](#gate-steps) with declared destinations |
| Ending | falling off the last step | `{complete: true, result: <name>}` |

A format-2 definition cannot be offered as a continuation candidate for a task
whose history was recorded under format 1: those results were written under a
different contract and cannot be reinterpreted. See
[Compatibility](#compatibility-across-formats).

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

New tasks pin the library's *current* revision of the name they select.
Everything afterwards — execution, restart recovery, which sessions are
addressable, which session review and shipping attach to, and what task detail
shows — resolves through *that task's* revision. The workflow's name is looked
up in the library for exactly two prospective questions: what a new launch
would pin, and what an old task is offered as a continuation candidate.

So saving a new revision of `bugfix-of-my-own` does not change a task already
running under it. Both revisions execute at once, and both stay readable: the
retained document is kept whole, not just its identifier, and stays available
after the library moves on, after the entry is archived, and after a later
release stops shipping a built-in.

Packaged definitions are validated at **daemon startup**. A malformed built-in
prevents the daemon from serving rather than failing a task later. Your own
drafts are never parsed at startup, so a half-finished one cannot keep the
daemon from starting.

## The workflow library

**Workflows** in the navigation is the library: every workflow that exists,
which is not the same as every workflow you can launch. For the procedure —
start one, edit it, validate it, save it, launch it — see
[Write your own workflow](../how-to/spawn-a-task.md#write-your-own-workflow).

| Origin | What it is |
|---|---|
| `builtin` | A packaged example. Read-only — [duplicate](#creating-a-workflow) it to make a version you can edit |
| `custom` | Yours: a draft, an executable revision, or both |

Each entry carries three independent facts.

**Draft text** is whatever you last saved in the editor. It is inert: any UTF-8
up to 1 MiB is accepted — empty, half-finished, or invalid YAML alike — and
nothing parses or executes it. Saving a draft never changes what the workflow
would launch. Drafts survive a refresh and a daemon restart. Built-ins have no
draft; their text comes from the package.

**The current revision** is the entry's executable choice — what a new launch of
this name pins. It changes only when an executable save succeeds. An entry that
has never had one is *draft only* and cannot be launched at all.

**The edit version** is a counter that advances on every successful change,
including a comment-only draft save. It is what stops two browser tabs from
overwriting each other, and it is deliberately not the revision: comments do
not change a revision, but they are still a real edit.

### Two ways to edit one draft

An entry's draft has two editors, and they are two views of the same document
rather than two documents. **Visual** is a list of step cards with an agents
panel; **YAML** is the text. Switching between them is not a save and not a
launch.

The visual editor covers everything either format supports: named agents and
which one the task opens on, each step's kind, assigned agent, abstract model
role and run condition, instructions built from text and explicit references,
required results and their artifact fields, evidence selectors with their
sources, freshness anchor and optional flag, command arguments as separate
rows, ordered decision cases, gate questions with their named answers, and
visit bounds with the gate they reach when exhausted. Nothing supported is
hidden behind the text editor.

Two things are deliberately not editable in either view. A workflow's **name**
is the library entry's identity, so renaming means creating a separate entry.
A workflow's **format** is the rules its document is read under, so an existing
format-1 workflow stays format 1 and keeps its own outcome, history, gate, and
completion rules; new workflows are format 2.

What switching costs you:

- Opening the visual editor and going back **without changing anything** leaves
  your text exactly as you typed it, comments included.
- Once you change something visually, saving rewrites the document. That
  normalizes its layout and drops YAML comments. It does not change what the
  workflow means — download the draft first if you want to keep your own
  formatting.
- Text that cannot be parsed at all stays in the YAML editor with the line and
  reason. The visual editor refuses to open rather than showing you an empty or
  last-known-good document.
- A format or a construct this daemon does not support is named as such. It is
  kept exactly as written, is not editable in the forms, and is never silently
  converted or stripped.

A draft that parses but is not yet a workflow is normal work, not an error
state. A route with no destination, a step that names a card you deleted, a
gate with no answers — all of these can be saved as a draft and reopened. The
editor shows the daemon's reason at the field it is about, with a link that
opens the card even when it is collapsed. None of them can be saved as an
executable revision.

Renaming a step or an agent moves the routes, selectors, visit counts, and
assignments that name it. It does not touch instructions, literal values, or
result names that merely contain the same word — those are not references.
Removing a step lists what names it first; if you remove it anyway, those
references stay visible as broken ones rather than being repaired into
something you did not ask for.

### The three save operations

They are separate on purpose, and only the third changes what runs.

| Operation | What it does | What it does not do |
|---|---|---|
| **Save draft** | Persists the text as-is | Never validates, never changes the current revision |
| **Validate** | Checks that exact text and shows either a read-only reading of it or a located error | Saves nothing, and authorizes nothing later |

| **Save executable revision** | Validates the text again, retains it, makes it the current revision, and saves it as the draft | Never starts a task |

Editing after a validation marks that result out of date; validate again. In
the visual editor a save submits the edit you are looking at: if it cannot be
turned back into a document, nothing is submitted and your work stays put.
Validation is *structural*: it says the daemon can read the definition, not that
the commands it names are installed, that a model will comply, or that the run
will succeed. A failed executable save leaves the last one exactly where it was.

### Creating a workflow

Four ways in, all of which produce a draft and nothing else:

- **New workflow** opens a minimal format-2 example with one agent step and a
  named ending, which you can then build out in either editor.
- **Duplicate** copies a saved revision into a new entry under a name you
  choose. This is how you customize a built-in.
- **Import YAML** reads a local file into the editor. The file is read by your
  browser; the daemon never receives a path and never opens one.
- **Paste** into the editor.

A name is permanent and globally unique. Built-in names are refused, and an
archived name stays reserved — the tasks that ran under it are still filed
there.

### Export and import

Exporting a saved revision gives you a standalone YAML definition, verified to
load back to the same revision before it is handed to you. Comments and
formatting are not preserved: a revision is a normalized document, and your text
lives in the entry's draft, which downloads separately and is labelled a draft.

A draft download is your text. If the daemon cannot turn a visually edited
draft back into YAML — it is unreachable, say — the download is offered as JSON
instead and labelled as such. JSON is valid YAML, so the file imports again;
its layout is not what you typed.

Re-importing an unchanged export under the same name is the same procedure and
reuses the same revision. Importing it under a *different* name is a different
document, so it gets its own revision. An import whose name collides is refused
— pick another name, or edit the existing entry.

### Archive and restore

Archiving removes a workflow from launch choices. Nothing is deleted: the draft,
every saved revision, and every task that ran one stay exactly as they are, and
archived entries are still readable behind an explicit filter. An archived entry
is read-only until you restore it; restoring makes its retained current revision
eligible again, and a draft-only entry comes back draft-only.

### When an entry cannot be launched

The library shows what exists; the Spawn form offers only what can run. An entry
is unlaunchable for one of these reasons, and it says which:

| Reason | What happened | What to do |
|---|---|---|
| `draft_only` | No executable revision has ever been saved | Validate and save one |
| `archived` | You archived it | Restore it |
| `missing`, `integrity`, `invalid`, `unsupported_format` | Its current revision cannot be read back as an executable definition | Save a corrected executable revision — its draft and history are still there |
| `not_packaged` | A built-in this daemon version no longer ships | Nothing; old tasks that ran it are unaffected |

One unlaunchable entry costs you that workflow and nothing else. The rest of the
library, the launch form, and the daemon carry on.

### Editing while something is running

Editing a library entry never reaches a task that has already been accepted: it
runs the revision it pinned, across your edits, an archive, a browser reconnect,
a daemon restart, and a package upgrade.

Between a launch preview and submitting it, a *new executable revision* of the
selected workflow invalidates the preview — you reviewed a procedure, not a name
— and archiving it refuses the launch outright. Everything else you typed is
kept. Editing only the draft changes nothing about the launch. Because the steps
may have moved, a revision change clears any per-step model overrides and says
so, rather than reattaching your choices to steps you never looked at.

### Two tabs, and losing a connection

Every save submits the edit version it loaded. If someone else — or another tab
— committed a change first, the save is refused, **nothing is written**, and you
are told the entry's current version. There is no automatic merge and no way to
force an overwrite: copy or download your text, reload the saved version, and
reapply your changes.

An edit committed elsewhere updates the saved library everywhere, but never
overwrites an open editor. If the connection drops, unsaved text stays on screen
and stays marked unsaved; operations whose response never arrived are not
retried automatically. Reconnecting reloads the saved state so you can see what
committed.

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

When a step asks for a result, the prompt is suffixed with an instruction
block naming `.ompire/outcome.json` and its schema, and the daemon **unlinks
any pre-existing outcome file before sending the prompt**, so a stale file from
an earlier step can never be mistaken for this one's result.

### The outcome document

`<clone>/.ompire/outcome.json`, read host-side after the turn ends. Which
envelope is expected depends on the definition's format.

Format 1 (`expects_outcome: true`):

| Field | Required | Type |
|---|---|---|
| `version` | yes | integer, must be `1` |
| `status` | yes | `"success"` or `"failed"` |
| `summary` | yes | string |
| `artifacts` | no | string-keyed map of workflow-defined handoff values |

Format 2 (`outcome.results`):

| Field | Required | Type |
|---|---|---|
| `version` | yes | integer, must be `2` |
| `result` | yes | one of the names this step declares |
| `summary` | yes | non-blank string |
| `artifacts` | yes when the result requires fields | object satisfying that result's contract |

A missing file, unreadable JSON, or a schema violation on a step that asked for
one **pauses the run** — see [Uncertainty pauses](#uncertainty-pauses). The
attempt keeps its absent outcome and the reason it was rejected. Nothing is
guessed and nothing is synthesized.

A `"failed"` outcome, or a declared negative result like `not-reproduced`, is
not a missing one. It is a real, declared result, and it follows the
definition's own routes.

An outcome-bearing step whose prompt renders empty records a null outcome
without reading the file and without pausing *in format 1*: no outcome
instruction was given, so anything on disk is stale by definition. In format 2
a step that owes a result and rendered an empty prompt pauses instead — a
definition that cannot ask for what it requires is not a step that produced
nothing. An explicit `when: false` remains a deliberate skip in both.

### Declared results (format 2)

A format-2 agent step declares `outcome: null` — no result is asked for — or
the results it may produce and, per result, the artifact fields that result
must carry with their JSON types:

```yaml
outcome:
  results:
    reproduced:
      required: {attempts: string, script_available: boolean}
    not-reproduced:
      required: {attempts: string, missing_prerequisites: string}
```

The prompt is told exactly these names and fields. A result the step does not
declare, a missing or blank required string, a wrong type, duplicate JSON keys,
invalid UTF-8, a document over 1 MiB, or nesting deeper than 32 is **not a
result**: the attempt pauses with a reason naming the field.

What this establishes is structure and attribution — that the step declared
this result and wrote the evidence it promised. It says nothing about whether
that evidence is *true*, and nothing in an artifact is ever an instruction.

### Evidence (format 2)

A step declares which prior attempts it needs, by alias:

```yaml
evidence:
  reproduction: {steps: [reproduce, reproduce-informed]}
  rejection: {steps: [verify], after: fix, required: false}
```

Each selector is resolved **once, when the attempt opens**, and what it
selected is recorded on that attempt. The prompt, the routing decision, the
gate message, and recovery after a restart all read those same records, so
"the evidence this step was given" is a recorded fact rather than a query
re-run later against a history that has grown. `steps`, `after`, and
`with_outcome` mean what they mean for format 1's `latest`; `required` (default
true) says what happens when nothing matches.

A **required** selector that matches nothing pauses the attempt before it
prompts or routes. An **optional** one binds to explicit absence, which reads
as missing rather than as an empty value someone wrote.

Format 2 has no `latest` and format 1 has no `evidence`: there is exactly one
way to read history in each.

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

A **format-1 gate** offers one action. `POST
/api/tasks/{id}/workflow/resume` records the operator's optional note as the
gate's outcome, finishes the gate `ok`, and continues at the next declared
step. Resuming a gate that is the last declared step completes the run —
including when the gate was re-armed by restart recovery.

A **format-2 gate** asks a question with named answers
([ADR-0030](../../adr/0030-commit-human-decisions-before-advancing.md)):

```yaml
choices:
  - id: retry-diagnosis
    label: Supply information and diagnose again
    feedback_required: true
    next: {step: diagnose}
  - id: stop
    label: Stop without a fix
    next: {complete: true, result: stopped-without-fix}
```

Each choice has a static destination — a declared step or a named completion.
A choice cannot compute a route and cannot pause, and there is no generic
Resume to bypass the choices with. The same request carries `choice_id`, and
`note` becomes that choice's feedback, required when the choice says so.

The question is **persisted before anyone can answer it**: the rendered
message, the offered choices, and the evidence identities it is asking about.
That snapshot is what the UI renders and what a submitted choice is checked
against — not the definition as it stands today — so a decision stays readable
after the definition changes. The answer is recorded *beside* the question,
never over it.

An answer commits before the run moves: the decision, the gate attempt's
completion, and either the successor attempt or the run's named ending land in
one transaction. A crash before that leaves the same unanswered question; a
crash after it leaves the successor the answer already opened. A repeated or
stale submission advances nothing.

Choice edges count as routes. A loop built out of human answers needs a
declared visit bound like any other, and a retry answer whose target has spent
its budget goes to that step's exhaustion gate instead of opening another
attempt. **No answer refills a budget**, and no answer grants authority the
definition did not declare — answering a gate never starts review, signs,
pushes, or opens a pull request.

A gate waits indefinitely. Re-notify aging applies to an unanswered gate as it
does to `waiting-input`.

### Uncertainty pauses

There is no LLM judge. When the evidence a step or a route needs is missing or
unreadable, the run stops and says what it was waiting for.

A pause is not a gate, and the UI keeps them apart. A **gate** is the
definition asking a person to look — to continue, in format 1, or to choose
among declared answers, in format 2. A **pause** is the engine refusing to
guess; the action retries the step that could not be decided, and it never
continues past it.

Six things pause a run:

| Reason | What happened |
|---|---|
| `missing_outcome` | A prompted step that requires a result left none that could be read |
| `unresolved_decision` | A route could not be decided from the recorded evidence, or the definition declared a pause for this case |
| `prompt_unrenderable` | A prompt or gate message referenced a value that is missing and has no declared fallback, or a format-2 step that owes a result rendered an empty prompt |
| `condition_unresolved` | A step's own condition could not be decided |
| `missing_evidence` | A format-2 step declared a required evidence selector that matched nothing |
| `workspace_unavailable` | Another daemon-managed writer owned the task's workspace, or an unresolved privileged effect made writing to it unsafe |

None of these is a *negative result*. A `"failed"` outcome, a nonzero command
exit, and a declared no-match all follow the definition's routes normally.

`workspace_unavailable` is the only one that is not about evidence. A review or
a delivery owns the task's workspace while it runs
([ADR-0032](../../adr/0032-bind-trusted-delivery-to-retained-candidates.md)), and
an effect whose outcome is unknown makes writing unsafe until an operator
resolves it. The step is **not started** — Ompire refuses the new writer rather
than interrupting the one that already holds the workspace — so retrying it once
that work finishes re-enters the step with nothing lost.

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

### Compatibility across formats

Both formats execute, side by side, indefinitely. A task runs whatever its
pinned revision says, and nothing about a new format reaches it.

- A retained format-1 definition keeps its grammar, its canonical bytes and so
  its revision identity, its outcome protocol, its gate semantics, its routes,
  and its recovery behavior. Adding format 2 changed none of them.
- Existing accepted tasks are never rebound. A new `bugfix` launch pins the
  format-2 revision; a task already running the older one keeps running it.
- Editing a prompt, a result contract, a gate choice, or a route produces a
  different revision and invalidates the affected launch preview.
- A format version this daemon does not implement is refused rather than read
  under the newest rules it happens to know.

A task created before definitions were retained is offered the current
definition of its own workflow name as a *continuation candidate*, with a
compatibility check. Because `bugfix` is now format 2, that check **refuses**:
its history recorded results under the older success/failed envelope, which a
contract that reads results by declared name cannot reinterpret, and it ran
steps the new definition does not declare. The refusal names both reasons. No
automatic upgrade is offered, the history is left exactly as recorded, and the
task stays readable, stoppable, and cleanable.

### Terminal work results (format 2)

`workflow_status` says a run stopped. In format 2 the run also records *what
stopping meant*, as the `result` named by the destination that ended it. The
packaged `bugfix` declares `validated`, `validated-without-reproduction`,
`stopped-without-fix`, and `stopped-unvalidated`.

A format-1 run has no name for its ending and none is invented for it: the
field is null, which reads as *not recorded* rather than as an empty verdict.

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
| `gate` | Re-arm the waiting state and re-broadcast the persisted question |
| paused | Re-arm the pause exactly as persisted — no prompt, no automatic retry |

Recovery re-drives the attempt that was already open rather than closing it and
appending another. A restart is not a work attempt, so it costs nothing against
a step's declared visit bound, and it re-binds no evidence: an attempt keeps
the records it froze when it opened.

An unanswered format-2 gate is re-armed as **the same question** — the stored
snapshot is re-broadcast, not re-rendered from today's history. An answered one
is never re-armed: because the decision and its successor commit together, a
restart finds either the untouched question or the successor the answer already
opened, and never a decision to make twice.

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
nudge re-states the outcome-file instruction; in format 2 it re-states the
step's whole result contract, since the interrupted turn may never have been
told which results it may declare.

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

### Reading a run against its procedure

Task detail shows the procedure the task accepted, laid over what actually
happened. It is addressed by the task's **retained revision**, never by its
workflow name: editing or archiving the entry in the library changes neither
this flow nor anything the run recorded.

Declaration and attempt are kept apart, because conflating them is how a run
gets read as more successful than it was.

| What you see | What it means |
|---|---|
| A step with no attempts | Work the procedure allows. Not skipped, not done — never reached |
| Several attempts on one step | Every visit, in order. A rejected verification and the corrected one are both there |
| A declared result | The name the step wrote, with its summary |
| A stop without a decision | The engine refusing to guess, with the reason it gave |
| A gate's recorded answer | Who chose it, which answer, any reason they wrote, and where it went |
| An evidence alias | A link to the exact producing attempt, by step and sequence number |

The routes shown under each step are the ones the definition **declares**.
Nothing on this page evaluates a condition, so no route is presented as the one
a run took or will take. Where history recorded a destination but not the
reasoning behind it — which every run does, because the engine stores the
destination and not the clause — you see the declared conditions and the
recorded inputs, not a reconstructed verdict.

Where history is silent, it says so. An attempt from a format-1 run recorded no
evidence bindings, so what it was handed is not recoverable; that is reported as
unrecorded rather than filled in from today's definition. A visit count is the
number of visits on the record, which for a run predating step records is fewer
than it actually had.

Answering a gate and retrying a stopped step remain the controls on the waiting
attempt itself. A gate the run has not reached offers no action.

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
| `POST` | `/api/workflow-library/document` |

`resume` advances a waiting run. It names the waiting attempt's sequence
number, and the daemon decides from the waiting record which of three waits it
is: answering a gate with one of its declared choices, resuming a format-1
gate with an optional note, or retrying a paused step.

Each step start and finish broadcasts `workflow_step` carrying the task id, the
attempt's sequence number, the step name, kind, and status — with error text on
failure, the pause document when the run stopped rather than deciding, and a
gate's whole question when it is waiting on one.

The snapshot carries each task's workflow state, so reconnecting clients see
current runs without replaying events. Each task also carries its pinned
revision, whether that revision can currently be resolved, and the primary
session *its* definition declares.

`GET /api/workflows` returns the *launchable* catalog — each eligible entry's
current revision and format, its sessions, and every declared step with its
kind, session, abstract role, and whether a route or its own condition can pass
it by. Every model consumer is one of those steps; nothing is described outside
them. `GET /api/workflow-library` returns every entry instead, archived and
draft-only included. Both ride in the WebSocket snapshot, derived from one read,
and every committed change publishes one `workflow_library_updated` entry from
which the launch catalog follows.

`POST /api/workflow-library/document` translates one draft between YAML text
and structured data, and reports what it currently means. It is what the visual
editor uses. It persists nothing, retains no revision, publishes no event, runs
no command, and authorizes no later save — an executable save still validates
the exact text it is handed.

`GET /api/workflows/revisions/{revision}` reads one retained definition by
content identity — deliberately not by name, because a name says what a *new*
launch would get and this answers "what did that task accept". An unknown
revision is `404`; a stored document that cannot be read comes back as a
classified `409` rather than being executed to answer a read. Adding `/yaml`
returns the same revision as a standalone YAML definition.

A launch validates its `workflow_name` against the library; an unknown,
archived, draft-only, or unreadable entry is rejected with `422` naming the
`workflow_name` field and the reason.

The full endpoint map, including the authoring routes and their conflict and
validation errors, is in the [API reference](api.md#workflow-library).

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
