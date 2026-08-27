# Task detail

## Overview

The task detail view is where an operator watches one task closely: what the
agent is doing, what it is asking, where the workflow has got to, and how to
get into the workspace by hand.

It is reached from a task's card, not from the nav.

## Using task detail

### Metadata panel

Project, branch, clone path, workshop identity with its derived status,
creation time, and elapsed time. When the task carries a template, the
"spawned" row is annotated `· template <name>`.

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

The panel updates from the main daemon stream without a reload. It distinguishes
an open review, comments returned to the agent, approval, abort, and review
error. When comments are returned, it says the primary agent is addressing
them; after that session returns to idle, **Start another review** becomes
available. It exposes the task's Ship flow link when review is approved, the
daemon has recorded ship progress, or the task has a pull request. When the
review display is approved the link reads **Continue to Ship flow**; otherwise
a recorded ship or pull-request handoff reads **Open Ship flow**. Both open
`/ship/<task-id>`.

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

While a run is `waiting` at a gate, a card renders the gate's operator
message, an optional note field, and a Resume action.

The card disappears when the run leaves `waiting`. The pending gate state is
also in the snapshot-driven task payload, so the card survives reloads and
reconnects — a gate that vanished on refresh would strand the run.

## Interfaces

The view reads from the main WebSocket snapshot and deltas, and opens a
per-session channel at `/api/ws/agents/{task_id}/{session}` for the selected
session's transcript only.

Actions post to the session-addressed endpoints described in [agent
interaction](agent-interaction.md), to `/api/tasks/{id}/workflow/resume` for
gates, and to `POST /api/tasks/{id}/review` or
`POST /api/tasks/{id}/review/cancel` for the Review panel.
