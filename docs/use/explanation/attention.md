# The attention model

Ompire's third product principle is that human attention is the scarce
resource. Not compute, not tokens — your ability to notice the one task that
needs you among the nine that do not.

This produces a specific design requirement: stay quiet while work progresses
safely, and be unmissable when it does not.

## The problem with the obvious approach

The naive design lets each part of the system decide when to shout. The agent
knows it asked a question; the review knows it is waiting; the UI knows a card
changed. Each is locally reasonable, and together they produce a system that
notifies constantly, which trains you to ignore it.

Ompire instead derives attention centrally, in one place, from one input.

## How it works

One daemon-owned state machine interprets agent lifecycle events and the
minimal subset of agent frames that matter — turn boundaries, questions,
approvals, tool execution, exits. It produces one status per session.

That status maps to an attention tier through a single pure function:

| Tier | What it does | Which statuses |
|---|---|---|
| `silent` | Nothing | `starting`, `working` |
| `badge` | Count in the UI | `idle`, `retrying` |
| `notify` | Desktop notification | `waiting-input`, `stalled`, `reviewing` |
| `interrupt` | Notification and sound | `waiting-approval`, `failed` |

Task attention is the highest tier across the task's sessions and any open
gate. Clients render that result. They do not compute their own.

The consequence is that the UI, the desktop notification, the tab title, and
the badge count can never disagree, because there is only one answer being
displayed four ways.

## Why the tiers are where they are

`working` is silent because a working agent is the system functioning. An
agent that works for an hour without interrupting you is the product doing its
job.

`idle` is a badge, not a notification. A finished turn is a thing to attend to
eventually, not now.

`waiting-approval` interrupts because the agent is blocked on a decision only
you can make, and the cost of a delayed approval is an agent sitting idle.

`failed` interrupts because a failure that goes unnoticed becomes a task you
think is running.

An unrecognized status defaults to `silent`. The mapping fails closed: a new
status that nobody has classified stays quiet rather than becoming noise.

## Advisories are not statuses

Some observations matter without changing what a session *is*. Context use
crossing a threshold is the current example: it fires a `context-high`
advisory that rides alongside the session.

Keeping these separate is deliberate. "May be running low on context" is a
hint you might act on. Promoting it to a status would put it in the tier
mapping and make it interrupt you, which is exactly wrong for information that
is merely useful.

`stalled` sits on the other side of that line, and is the model's weakest
point: silence past a threshold is genuinely ambiguous. A long build looks
identical to a wedged agent. It is a status rather than an advisory because a
wedged agent is common enough to be worth surfacing — but if your work
involves long legitimate silences, raise `stall_threshold` rather than
learning to ignore the notification.

## Degradation

Desktop notifications use `notify-send` and degrade in stages: missing binary,
no D-Bus session, or no `--action` support each reduce what you get, and each
is logged. The badge count and tab title keep working throughout.

This ordering is intentional. The in-UI signal is the reliable one; desktop
notifications are the enhancement.

## Re-notification

An unanswered `notify` or `interrupt` entry re-notifies at the configured
interval. Attention that expired unacknowledged is not attention that was
paid — a notification you missed while away should still be there when you
return.
