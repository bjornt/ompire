# Daemon settings

## Overview

Some settings need to change while the daemon runs — how eagerly it
re-notifies, when it calls a session stalled, which tiers make noise. Others
must not be editable from a browser at all.

Ompire layers a small set of daemon-writable settings over the operator's
configuration file, and never rewrites that file. The boundary is recorded in
[ADR-0013](../adr/0013-layer-daemon-writable-settings-over-operator-configuration.md).

## States and behavior

### Resolution order

```text
registry override  →  config.toml value  →  built-in default
```

The daemon persists overrides as JSON-encoded scalars in the registry's
`settings` table. `config.toml` stays exactly as the operator wrote it,
comments and all.

Not rewriting TOML is a deliberate constraint. A daemon that edits its own
config file can strip comments, reorder keys, and — in the worst case —
produce a file that fails to parse at the next startup, leaving the operator
with a daemon that will not boot.

### The recognized settings

Fifteen keys, in two groups.

**Numeric, also seedable from `config.toml`:**

| Key | Default |
|---|---|
| `renotify_interval` | `300` |
| `stall_threshold` | `300` |
| `context_advisory_threshold` | `80` |

**Attention-tier preferences, default-only:**

Three booleans for each of the four tiers —
`tier.<interrupt|notify|badge|silent>.<desktop|sound|badge>`.

| Tier | `desktop` | `sound` | `badge` |
|---|---|---|---|
| `interrupt` | `true` | `true` | `true` |
| `notify` | `true` | `false` | `true` |
| `badge` | `false` | `false` | `true` |
| `silent` | `false` | `false` | `false` |

Tier preferences cannot be set in `config.toml`. They are defaults overridden
at runtime or not at all.

### Applying a change live

An update is pushed to every live consumer without a restart: the notifier's
preferences and re-notify interval, the advisory sampler's threshold, and the
session tracker's stall threshold. A `settings_changed` event carrying the
effective map is broadcast.

Live application is not uniform, and the differences are deliberate:

- **Tier preferences** are read at fire time, so a change affects the next
  transition.
- **`renotify_interval`** re-arms outstanding timers from the moment of
  change; `0` cancels them outright.
- **`stall_threshold`** applies to watchdogs armed after the change. A timer
  already sleeping keeps its original deadline.
- **`context_advisory_threshold`** applies to the next sample and clears the
  per-session fired latch, so a lowered threshold can fire without waiting for
  a drop below the old one.

### What is not editable at runtime

Everything else. Ports, paths, commands, timeouts, and credentials are
`config.toml`-only and require a restart.

The line is drawn at infrastructure: a setting that could point the daemon at
a different binary, a different directory, or a different network interface is
not something a browser should be able to change.

## Failures and recovery

| Condition | Response |
|---|---|
| Unknown key | `422` naming the key |
| Wrong value type — a non-boolean tier preference, for instance | `422` naming the key |

Clearing an override falls back to `config.toml`, or to the built-in default
when the file says nothing.

## Interfaces

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/settings` | The effective settings map |
| `PUT` | `/api/settings` | Set overrides |
| `DELETE` | `/api/settings/{key}` | Clear one override |

Changes broadcast `settings_changed` with the full effective map. The
WebSocket snapshot carries the current effective settings.
