# Task detail

## Overview

The task detail view is where an operator watches one task closely: what the
agent is doing, what it is asking, where the workflow has got to, and how to
get into the workspace by hand.

It is reached from a task's card, not from the nav.

## Using task detail

### Metadata panel

Project, branch, clone path, workshop identity with its derived status,
creation time, and elapsed time.

### Accepted configuration panel

The launch decision this task was accepted under: its workflow and the exact
**revision** it executes, the task-wide model profile and where it came from,
the effective base branch, Workshop additions source and preamble with any
task-local override marked, and one row per model consumer.

The revision is the content identity of the definition itself, and the panel
offers the normalized document for reading. That is the only honest answer to
"what procedure did this task run" once the installed definition has moved on.
A task whose continuation was confirmed after the upgrade also states its
boundary: which attempts ran under a definition that was never retained, and
which single attempt spans it.

Each consumer row — every declared agent step, and there are no others — names
its source profile, its role, the concrete model and thinking policy those
resolve to, and, separately for the profile and the role, whether that value
was inherited or set for this step. Expanding a row shows the complete
`smol`/`slow`/`plan` map that consumer's process carries.

These are the stored values, not a recomputation — a project or profile edited
or deleted since acceptance does not change what this panel shows or what the
task runs. A named profile is provenance; the snapshot beside it is what
executes.

While a session is live, a **Running now** table adds what omp reports it is
actually running: the active model, the thinking policy the profile states, and
the level omp resolved it to. Those last two differ legitimately for `auto` and
`max`, which resolve per model.

A session whose process is being replaced to apply a different policy reads as
*starting* while that happens, and reports no model until the new one is
verified. Transitioning is never shown as a successfully applied new policy,
and it is not a failure: the conversation is resumed, not restarted.

A task created before launch inputs were recorded shows a **Configuration
needed** form instead: what is known, what is unknown and unrecoverable, and
the fields to confirm for future behavior. Confirming requires an explicit
acknowledgement and changes no recorded history; an interrupted run can then be
continued explicitly. An archived task of that vintage says so and asks for
nothing. See [Tasks](tasks.md#tasks-without-recorded-launch-inputs).

When a task has no live agent, the transcript, composer, and status-strip
regions degrade to an inactive or empty state rather than disappearing. A
region that vanishes reads as a bug; one that says "nothing here" reads as an
answer.

### Review panel

Every task detail shows one Review panel for the task's **primary session**.
It does not follow the selected transcript tab or the session currently used
by a workflow step. This keeps review and the next publishing handoff attached
to the task that owns them.

Before review starts, the panel says why it is unavailable or offers **Start
review** only when the primary session is idle and has a live agent. Starting
locks the action as **Starting…** until the daemon reports the review. If the
command is refused or fails, its error remains inline and the operator can
retry when the displayed state permits it.

While independent review is open, the full llmvet URL is a keyboard-accessible
external link and **Cancel review** is available. Cancellation similarly stays
locked until the daemon reports its outcome; a failed cancellation leaves the
observed review visible and restores the valid action.

A review restored after a daemon restart keeps its status and iterations but
has no live reviewer, so the panel offers no llmvet link for it. See
[Review](review.md#retention-and-restart).

The panel updates from the main daemon stream without a reload. It distinguishes
an open review, comments returned to the agent, approval, abort, review error,
and a reviewer interrupted by a daemon restart. When comments are returned, it says the primary agent is addressing
them; after that session returns to idle, **Start another review** becomes
available. It exposes the task's Ship flow link when review is approved, the
daemon has recorded a delivery, or the task has a pull request. The link reads
**Continue to Ship flow** only when the approval still covers the content that
would be published; a superseded approval reads **Open Ship flow**, because the
next step there is another review rather than a handoff. Both open
`/ship/<task-id>`.

An approval names the content it graded. When the task has moved on since, the
panel labels it **Approved (superseded)** and says that delivering the current
content needs a fresh review. An approval recorded before content-bound review
says instead that it does not identify what it approved. Neither is deleted:
both remain visible as history. See [Review](review.md).

Every iteration is ordered from oldest to newest and records its outcome,
recorded time, optional comment count, and any captured reviewer stderr. Error
output is available in an expandable, readable disclosure.

### Escape-hatch instructions

Copyable instructions for entering the task's container by hand: change to the
clone directory, open a workshop shell, and resume the agent inside the
container.

This is the deliberate escape hatch. Ompire orchestrates the work; it does not
imprison it. Anything the operator can do through the UI they can also do
directly, and the instructions name the actual paths for this task.

### Session tabs

When a task's workflow declares more than one session, a tab bar renders above
the transcript with one tab per declared session, showing its name and a live
status dot.

Selecting a tab switches the transcript, composer, status strip, and question
card to that session.

The default selection is the session of the workflow's current step when a run
is in flight, otherwise the workflow's primary session — so opening a running
task shows the session actually doing something.

Sessions not yet spawned render inactive as "not started" and **do not open an
event channel**. The tab bar is hidden entirely for single-session workflows,
leaving the `single-step` layout unchanged.

### Transcript

Streamed from the selected session's event channel. Tool executions render as
collapsible tool cards showing the tool identity and expandable to detail,
agent thinking renders as distinct thinking blocks, and subagent activity is
grouped under the parent tool call that spawned it.

The panel occupies a bounded, viewport-relative region: its heading stays put
and the stream below it is what scrolls, rather than the page growing. Wide
tool input and output still scroll horizontally inside their own cards. A conversation of any length therefore
leaves the rest of the cockpit — metadata, review panel, status strip, workflow
strip — where the operator left it, instead of pushing it further off-screen
with every tool call. On the narrow one-column layout the panel is bounded more
tightly so the composer below it stays reachable.

The stream follows the live output. It starts at the newest output and stays
there as text, tool cards, and tool output arriving on an existing tool card
extend it. Scrolling away from the end suspends that: output keeps arriving and
the stream keeps growing, but the view stays exactly where the operator put it.
Scrolling back to the end resumes following. "At the end" is deliberately
forgiving, so a stray wheel tick does not silently strand the operator behind a
live stream.

A stream that starts over starts at the newest output again — selecting a
different session tab, opening a different task, or the event channel
reconnecting and replaying the session buffer. Expanding a tool card is a
reader action, not new output, and never moves the view.

The stream is a keyboard-focusable scroll region with its own accessible name,
so it can be reached and scrolled from the keyboard and shows a visible focus
ring. Following is instant and never animated: no motion is introduced for an
operator who has reduced motion enabled. Streamed output is not announced
continuously to assistive technology.

### Question and approval cards

The selected session's pending question renders from the normalized `question`
payload, updating live.

An `ask` question renders each prompt with its options and descriptions,
single- or multi-select per the payload, the recommended option highlighted,
and a free-text "other" input when allowed. An approval gate renders as an
approve/deny card.

Submitting calls the session's answer endpoint with the question id and the
selection. The card disappears when the question resolves.

A pending question on a session **other** than the selected one surfaces as a
marker on that session's tab, so a question raised in a background session is
not silently missed.

### Composer

Steer, follow-up, and interrupt-and-prompt modes, each sending to the matching
endpoint of the **selected** session.

Enabled state derives from that session's live agent streaming flag and
status. Invalid modes for the current state are disabled, and the whole
composer is disabled when the selected session has no live agent.

The composer stays enabled while the session is `waiting-input`, with a note
that a question is pending — the turn is still in flight, so steering is
active and follow-ups queue.

### Status strip

The selected session's state and reason, plus the agent's todos, context usage
percentage, token and cost figures, and model.

State and reason update live from `status_changed`. The metrics come from the
session's state and stats endpoints and refresh at turn boundaries.

### Workflow strip

For a task with a workflow run: one chip per executed step record in order,
carrying the step name, kind, and status coloring, with the step's outcome
summary or error available on the chip. The run's overall status —
`running`, `waiting`, `complete`, `failed` — is visible alongside.

Updates live from `workflow_step` events and task payloads.

### Gate card

While a run is `waiting`, a card renders what it is waiting on. Three different
waits land here and the card keeps them apart.

A **gate with declared choices** shows the question, the evidence it is asking
about, and every answer the workflow offers with the destination each one
leads to. Nothing is selected for you, so opening the card authorizes nothing,
and the submit action stays disabled until you choose. A choice that declares a
required reason cannot be submitted without one, and the card says which choice
is asking. There is no generic Resume to bypass the choices with.

A **gate without them** (a format-1 workflow) shows its message, an optional
note, and a Resume action.

An **uncertainty pause** shows why the engine stopped and offers a retry of the
step that could not be decided, naming that step — retrying re-enters it rather
than skipping past it.

What the card renders is the question **as it was asked**, read from the
attempt's own record rather than from the workflow's current definition. A
decision answered last month still shows the message, the options, and the
records it was about, even if the definition has since changed.

The card disappears when the run leaves `waiting`. Anything typed into it
belongs to the attempt it was typed against: if the run moves on — another tab
answered, or a restart re-armed something else — the draft is dropped rather
than carried onto a different question, and a stale submission is refused
rather than applied. The pending state is also in the snapshot-driven task
payload, so the card survives reloads and reconnects; a gate that vanished on
refresh would strand the run.

### Procedure

The whole definition the task accepted, with what happened laid over it. It is
addressed by the task's **retained revision**, so editing or archiving the
workflow in the library afterwards changes neither this flow nor anything the
run recorded.

Every step of the definition is listed, whether or not it ran, with its kind,
assigned agent, abstract role, instruction, required results, the evidence it
reads, its visit bound, and every route declared out of it. Those routes are
declarations: nothing here evaluates a condition, so no branch is shown as the
one a run took or will take.

Selecting a step shows its attempts, each one separately:

| Shown | Meaning |
|---|---|
| No attempts | Work the procedure allows. Not skipped, not done — never reached |
| Several attempts | Every visit, in order. A rejected verification and the corrected one are both there |
| A declared result | The name the step wrote, with its summary |
| A stop without a decision | The engine refusing to guess, with the reason it gave |
| A recorded answer | Who chose it, which answer, any reason they wrote, and where it went |
| An evidence alias | A link to the exact producing attempt, by step and sequence number |

Where the record is silent, it says so. A step that declares no evidence was
handed none; an attempt from a format-1 run kept no bindings, so what it was
handed is not on the record. Neither is filled in from today's definition. A
visit count is the number of visits **on the record**, which for a run
predating step records is fewer than it actually had.

Answering a gate and retrying a stopped step remain the [gate
card](#gate-card)'s controls on the waiting attempt. A step the run has not
reached offers no action.

### Step history

Each attempt records which prior attempts it was handed. A finished run can
therefore say not just what each step concluded but which reproduction a fix
was given, and which fix a verification checked — by attempt, not by recency.

A run that ended under a format-2 workflow also carries its declared ending —
`validated`, `stopped-without-fix`, and so on — beside its `complete` status.
A format-1 run has no name for its ending and shows none.

## Interfaces

The view reads from the main WebSocket snapshot and deltas, and opens a
per-session channel at `/api/ws/agents/{task_id}/{session}` for the selected
session's transcript only.

Actions post to the session-addressed endpoints described in [agent
interaction](agent-interaction.md), to `/api/tasks/{id}/workflow/resume` for
gates and pauses — carrying the waiting attempt's sequence number and, at a
gate with declared choices, the chosen `choice_id` — and to
`POST /api/tasks/{id}/review` or
`POST /api/tasks/{id}/review/cancel` for the Review panel.
