# Attention and notifications

## Overview

Ompire aggregates every session status and gate wait into one attention tier
per task, and turns that into a desktop notification, a sound, a badge count,
or nothing at all.

One pure mapping owns the classification. The session state machine carries no
tier knowledge, and clients consume the daemon's answer rather than deriving
their own from raw statuses.

## States and behavior

### Tier classification

| Tier | Statuses |
|---|---|
| `silent` | `starting`, `working` |
| `badge` | `idle`, `retrying` |
| `notify` | `waiting-input`, `stalled`, `reviewing` |
| `interrupt` | `waiting-approval`, `failed` |

A workflow run waiting at a gate classifies as `notify`.

### One entry per task

At most one active notification and one re-notify timer per task. The task's
effective tier is the **worst** tier among its sessions and any waiting gate.

A new transition supersedes the task's previous notification, cancelling the
outstanding notifier subprocess before firing the replacement, so a task never
accumulates stacked notifications.

### Desktop notifications

Entering a tier whose `desktop` preference is enabled fires a `notify-send`
notification with a summary and body naming the task and its status and
reason, and a single `Open` action that launches the task's URL.

There is deliberately **no approve or answer action on the notification**.
Approvals and answers happen in the UI, where the operator can see what they
are approving.

Preferences are read at fire time, so a change applies to the next transition
without a restart.

### Urgency and sound

A notification is raised with critical urgency when the firing tier's `sound`
preference is enabled, and normal urgency otherwise.

Critical urgency is the freedesktop-standard sound trigger, so the preference
controls sonification without the daemon playing audio itself.

### Re-notify aging

While a task stays in a desktop-firing tier and unanswered, its notification
re-fires every `renotify_interval` until the tier changes.

Any attention-tier transition — the operator answering, the turn moving, the
child exiting — cancels the pending timer. A changed interval re-arms
outstanding timers from the moment of change; setting it to `0` cancels them
outright and disables re-notification entirely.

Attention that fires once and is missed while the operator is away is
attention that was never paid. Aging is what makes the notification survive a
coffee break.

### Badge-tier entries

`badge`-tier entries are tracked and broadcast exactly like higher tiers, but
fire no desktop notification and no re-notify timer by default, because their
`desktop` preference defaults off.

## Configuration

| Setting | Default | Effect |
|---|---|---|
| `notifications_enabled` | `true` | Master toggle in `config.toml` |
| `renotify_interval` | `300` | Seconds between re-fires; `0` disables |
| `tier.<tier>.desktop` | see below | Whether the tier fires a notification |
| `tier.<tier>.sound` | see below | Whether it raises critical urgency |
| `tier.<tier>.badge` | see below | Whether it counts toward the badge |

Defaults: `desktop` on for `interrupt` and `notify`; `sound` on for
`interrupt` only; `badge` on for `interrupt`, `notify`, and `badge`.

The twelve tier preferences are runtime settings only — they cannot be set in
`config.toml`.

## Failures and recovery

Desktop notification degrades gracefully. When `notify-send` is absent, its
`--action` form is unsupported, or no D-Bus session bus is reachable, the
daemon logs a single actionable warning and continues: attention events are
still broadcast and badge counts stay intact.

A notification problem never crashes the daemon and never blocks a session
transition. The in-UI signal is the reliable one; desktop notification is the
enhancement.

## Interfaces

| Event | Fires when |
|---|---|
| `attention` | A task enters any non-`silent` tier |
| `attention_cleared` | A task returns to `silent` |

The `attention` payload carries `{task_id, session, tier, status, reason}`.
The `session` field names the session that caused the entry, or is null for a
gate wait, which is workflow-level rather than session-level.

The snapshot exposes current attention entries, so a reconnecting client sees
them without replaying events.

Clients derive the "N need you" count from these entries filtered by the
per-tier `badge` preference — not by re-deriving tiers from raw statuses.
