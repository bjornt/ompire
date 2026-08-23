# Agent interaction

## Overview

Once a session's agent is running, the operator can steer it mid-turn, queue a
follow-up, interrupt it, answer a question it asked, and read its live state
and cost.

Every one of these is addressed to a specific session —
`/api/tasks/{id}/sessions/{name}/agent/…` — because a task may run several
sessions and they are independent.

## Using agent interaction

### Composer actions

| Action | Effect |
|---|---|
| `steer` | Mid-turn guidance, delivered while the agent is working |
| `follow-up` | Queue a message for after the current turn |
| `interrupt` | Abort the current turn and prompt with new text |

Each accepts the operator's message text, forwards it to the session's live
agent, and returns the agent's correlated response.

### Reading state

| Endpoint | Returns |
|---|---|
| `.../agent/state` | Streaming flag, queued message count, todos, context usage, model |
| `.../agent/stats` | Token and cost figures |

The daemon passes the agent's reported fields through without reinterpreting
them. Ompire does not second-guess what the agent says about itself; it
decides what to *do* about it.

### Questions and approvals

An agent may raise a request that blocks its turn. The daemon classifies each
as either a **question** or an **approval gate**, and the session enters
`waiting-input` or `waiting-approval` accordingly.

Classification is per session and depends on whether an `ask` tool execution
is in flight on that session's stream when the request arrives:

- Request during an in-flight `ask` execution — a **question**.
- Request with no `ask` in flight — an **approval gate**, cross-checked
  against the options being exactly `["Approve", "Deny"]`.
- Cross-check fails — logged, and classified as a **question** rather than
  mislabeled.

The fallback direction matters: mislabeling a question as an approval would
present the operator with a two-button gate for something that wanted an
answer.

### The pending question payload

A normalized payload carries the question id needed to answer, the kind (`ask`
or `approval`), and for an ask, the structured questions drawn from the tool's
arguments: prompt text, options with value, label, and description, whether
multiple selections are accepted, which option is recommended, and whether a
free-text "other" answer is accepted.

At most one question is pending per **session** at a time. Different sessions
of the same task may hold independent pending questions concurrently, and
answering one leaves the others untouched.

### Answering

`POST .../agent/answer` takes the question id and the operator's answer —
selected option values, free text, or an approve/deny decision. On a match the
daemon writes the reply to the agent, clears the pending question, and returns
the session to `working`.

## States and behavior

### Pending question lifecycle

A pending question is cleared not only by an answer but whenever the turn
moves on without it:

- the `ask` execution ends;
- the agent starts or ends a turn while a question is pending;
- the operator interrupts the session;
- the session's agent process exits.

Each clear broadcasts `question_resolved`, except a process exit — that drives
the session to `failed` and takes precedence.

Clearing on turn movement is what prevents a stale question sitting on a card
demanding an answer the agent has stopped waiting for.

## Failures and recovery

| Condition | Response |
|---|---|
| Session has no live agent — never spawned, exited, or cleaned up | Client error naming the missing agent; no request is sent |
| Task id does not exist | `404` |
| Session name not declared by the task's workflow | `404` |
| Question id does not match the session's current pending question | Client error; no reply sent to the agent |

Every one of these is a clean error rather than a crash or a hang. The stale
question-id rejection matters most: it prevents an answer intended for one
question being delivered to whatever replaced it.

## Interfaces

| Method | Path |
|---|---|
| `POST` | `/api/tasks/{id}/sessions/{name}/agent/steer` |
| `POST` | `/api/tasks/{id}/sessions/{name}/agent/follow-up` |
| `POST` | `/api/tasks/{id}/sessions/{name}/agent/interrupt` |
| `POST` | `/api/tasks/{id}/sessions/{name}/agent/answer` |
| `POST` | `/api/tasks/{id}/sessions/{name}/agent/stop` |
| `GET` | `/api/tasks/{id}/sessions/{name}/agent/state` |
| `GET` | `/api/tasks/{id}/sessions/{name}/agent/stats` |

A question becoming pending broadcasts `question_posted` with the task id, the
session name, and the normalized payload. Clearing broadcasts
`question_resolved` with the task id, session name, and question id.

The snapshot's per-session entry carries the same normalized payload under a
`question` field, so a reconnecting client sees a pending question without
replaying events.
