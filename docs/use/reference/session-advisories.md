# Session advisories

## Overview

Advisories are observations that decorate the UI without changing what a
session *is*. They never contribute to an attention tier and never fire a
notification.

The distinction is deliberate. "This session may be low on context" is useful
information the operator might act on. Promoting it to a status would put it
in the tier mapping and make it interrupt them — wrong for something merely
useful.

## States and behavior

### Session stats

At each turn boundary, alongside the idle-debounce state query, the daemon
samples the session's state and stats and broadcasts
`stats {task_id, session, context_pct, tokens, cost}`.

Throttled to at most one event per `stats_throttle_interval` per **session**,
so an agent taking many short turns cannot flood a dashboard.

A failed sample is logged and skipped. It never disrupts the session's idle
transition — telemetry must not be able to wedge the state machine.

### Context-high advisory

When a sampled context percentage crosses `context_advisory_threshold`, the
daemon broadcasts `advisory {task_id, session, kind: "context-high",
context_pct}` suggesting compaction or handoff.

It fires **once per crossing**, not on every sample above the threshold. When
the percentage later drops below, the advisory clears so a future crossing
fires again.

Changing the threshold applies to the next sample and clears the per-session
fired latch, so a newly lowered threshold can trigger without first waiting
for a drop below the old one.

### Maybe-waiting decoration

When a session goes `idle`, the daemon evaluates the last assistant message
with a lightweight question heuristic. A positive result broadcasts
`advisory {task_id, session, kind: "maybe-waiting"}`, which the task card
renders as "may be waiting for a reply".

It is cleared when the session's status changes away from `idle`.

This is a decoration, not a state. An agent that ended its turn with a
question is still idle — it is not blocked on anything the daemon can see, and
treating a heuristic as ground truth would put guesswork into the attention
model.

## Configuration

| Key | Default | Effect |
|---|---|---|
| `stats_throttle_interval` | `10` | Minimum seconds between `stats` events per session |
| `context_advisory_threshold` | `80` | Context percentage that fires `context-high`. Runtime-editable. |

## Interfaces

| Event | Payload |
|---|---|
| `stats` | `{task_id, session, context_pct, tokens, cost}` |
| `advisory` | `{task_id, session, kind, ...}` |

Advisory kinds are `context-high`, which carries `context_pct`, and
`maybe-waiting`.

Task cards render `stats` as a tokens and cost line, a `context-high` advisory
as an amber context ring with a compact/handoff hint, and `maybe-waiting` as a
note on an idle card.
