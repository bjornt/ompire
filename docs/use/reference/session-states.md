# Session states

## Overview

A session is one named interaction with a coding agent, tracked per
`(task, session name)`. A task's workflow declares its sessions, and each is
tracked independently — one session's crash does not disturb its siblings.

Session status is what drives the attention model, the task card's pill, and
what actions a task offers. It is derived from the agent child's lifecycle and
from a small interpreted subset of agent frames.

## States and behavior

| Status | Entered when |
|---|---|
| `starting` | Child spawn, until the first validated `agent_start` |
| `working` | A validated `agent_start` arrives |
| `idle` | Only via the debounced turn-boundary rule |
| `waiting-input` | A pending request is classified as a question |
| `waiting-approval` | A pending request is classified as an approval gate |
| `stalled` | A `working` session is silent past the watchdog threshold |
| `retrying` | An automatic retry is in flight |
| `reviewing` | A review is open, on the task's primary session |
| `failed` | Any child exit, operator stop included, or a start failure |

Every transition records a `reason` naming its evidence — the frame type, the
exit code, "stopped by operator", "no frames for Nm", "review opened". The
reason is what makes an unexpected status explicable after the fact.

### One guarded transition point

All transitions for a session pass through a single guarded point, so
competing transitions resolve deterministically: a child exit, an idle
debounce, the stall watchdog, retry frames, a question being posted or
cleared, and a review opening or closing cannot interleave into a wrong
result.

**A child exit to `failed` always wins.** Any pending waiting, stall, retry,
or idle transition is discarded.

Transitions of different sessions of the same task never interfere.

### Debounced idle

A validated `agent_end` does not go straight to `idle`. The daemon waits
`session_idle_debounce` (default 2 seconds), abandoning the transition if an
`agent_start` arrives or the child exits during the wait.

After the debounce it queries the agent's state and stays `working` if the
response reports an in-flight stream or a non-zero queued-message count. Only
a quiet, empty-queue result yields `idle`.

This is why chained turns never flicker through `idle`: an agent that ends one
turn and immediately begins another was never actually idle, and a status that
flickered would produce a badge, a card change, and possibly a notification
for nothing.

If the state query itself fails, the daemon logs it and falls back to the
debounce-only result rather than hanging the tracker.

### Waiting states

`waiting-input` and `waiting-approval` are entered **only from `working`**. A
pending question raised outside an in-flight turn is logged and ignored, not
applied — a question with no turn behind it is a protocol anomaly, not a
state.

Clearing the pending question — an operator answer, turn movement, an
interrupt — returns the session to `working`.

### Stall watchdog

The daemon tracks the time of the most recent interpreted frame and arms a
watchdog whenever a session is `working`. Silence for the effective
`stall_threshold` transitions it to `stalled`, only from `working`, with a
reason naming the duration.

The next interpreted frame returns it to `working` and re-arms the watchdog.
The watchdog is cancelled when the session leaves `working` or `stalled` by
any other path.

A changed `stall_threshold` applies to watchdogs armed after the change. A
timer already sleeping keeps its original deadline.

### Retrying

A validated retry-start frame moves a session to `retrying`, allowed from
`working` or `stalled`. A retry-end frame or the next `agent_start` returns it
to `working`.

### Reviewing

`reviewing` is not driven by agent activity. The review manager enters it from
`idle` on the task's primary session, and a request to raise it from any other
status is rejected.

It leaves to `idle` on approval or abort, and to `working` when review comments
are looped back and the agent resumes. An exit while `reviewing` still lands
`failed` and tears the review down.

### Empty prompt

A session whose workflow step built an empty prompt transitions from
`starting` to `idle` when the ready handshake completes, with a reason naming
the missing prompt, rather than sitting in `starting` forever.

## Retention and recovery

Session status is **in-memory**. It survives the agent child's exit and
deregistration — a `failed` status stays visible, which is the point — and is
discarded when the task is cleaned up or purged.

After a daemon restart no prior statuses exist. On startup, status is
re-established for each recovered session: a session being resumed presents as
`starting` and lands `idle` once its agent is ready; one that cannot be
resumed presents as `failed`.

`reviewing` never survives a restart. Review state is transient, so a
recovered task's primary session presents as `starting`, `idle`, or `failed`,
and the operator re-triggers the review. The clone's Git state is restored
separately from the review's durable ref.

## Configuration

| Key | Effect |
|---|---|
| `session_idle_debounce` | Wait before an `agent_end` becomes `idle`. Default 2 seconds. |
| `stall_threshold` | Silence before `working` becomes `stalled`. Default 300 seconds. Runtime-editable. |

## Interfaces

Every transition broadcasts `status_changed` carrying the task id, the session
name, the previous status, the new status, and the reason.

The WebSocket snapshot carries a `sessions` map from task id to a per-session
map of session name to `{status, reason, since}`, plus a pending `question`
where one exists. Reconnecting clients therefore see current status without
replaying events.

Session status is never folded into task payloads. The registry describes what
a task is; this describes what its agent is doing.
