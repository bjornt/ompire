# Daemon settings

## Overview

Some settings need to change while the daemon runs — how eagerly it
re-notifies, when it calls a session stalled, which tiers make noise. Others
must not be editable from a browser at all.

Ompire layers a small set of daemon-writable settings over the operator's
configuration file, and never rewrites that file. The boundary is recorded in
[ADR-0013](../../adr/0013-layer-daemon-writable-settings-over-operator-configuration.md).

This page covers those layered scalar settings. Other things edited from the
same Settings view are registry entities with their own lifecycle rather than
settings — [templates](templates.md) and
[model profiles](model-profiles.md) — and are documented on their own pages.

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

Seventeen keys, in four groups.

**Numeric, also seedable from `config.toml`:**

| Key | Default |
|---|---|
| `renotify_interval` | `300` |
| `stall_threshold` | `300` |
| `context_advisory_threshold` | `80` |

**Signing-key selection, also seedable from `config.toml`:**

| Key | Default |
|---|---|
| `gpg_signing_key` | unset — auto-detect |

An override must be a full 40-character OpenPGP fingerprint naming a key the
daemon currently enumerates as usable for signing. Anything else is rejected
with `422` before any value is written. Clearing it returns to `config.toml`,
then to `git config user.signingkey`, then to automatic detection.

This is the one setting that selects an identity, and it is admitted under an
explicit bound: it can only ever name a key the operator's own host keyring
already holds, and it never carries or reaches credential material. See
[ADR-0021](../../adr/0021-admit-signing-key-selection-as-bounded-daemon-writable-setting.md)
and [GPG signing](gpg-signing.md).

**Checkout location, also seedable from `config.toml`:**

| Key | Default |
|---|---|
| `checkout_root` | `~/proj` |

The parent directory a [project's](projects.md) checkout path is derived from,
and where "clone it for me" creates one. An override must be an absolute path
once `~` is expanded, must contain no `..`, must not be the filesystem root,
and must be neither the daemon's task root nor inside it — task cleanup
deletes there, and a base checkout must never be in reach of it. Anything else
is rejected with `422` before a value is written.

This is a filesystem setting, admitted under its own explicit bound. It
chooses where an already-authorized, already-bounded operation puts its
result; it cannot make the daemon delete anything, and it cannot reach outside
the destination it derives. See
[ADR-0023](../../adr/0023-admit-checkout-root-as-bounded-daemon-writable-setting.md)
and [ADR-0022](../../adr/0022-create-or-adopt-base-checkouts-without-mutating-them.md).

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
- **`gpg_signing_key`** re-probes immediately on set and on clear, so the
  chrome chip, the Settings panel, and the ship gate follow from one broadcast.
- **`checkout_root`** is read when a project is registered, so a change
  applies to the next clone-mode registration and to the next derived adopt
  path. It never moves a checkout, rewrites a stored `checkout_path`, or
  invalidates an existing project.

### What is not editable at runtime

Everything else. Ports, commands, timeouts, credentials, and every other path
are `config.toml`-only and require a restart. The task root in particular
stays TOML-only: it is where the daemon *deletes*, and changing it while tasks
exist would strand live workspaces.

The line is drawn at infrastructure: a setting that could point the daemon at
a different binary, or a different network interface, is not something a
browser should be able to change.

Two settings are deliberate exceptions, and the shape of each exception is
what keeps the line intact:

- `gpg_signing_key` chooses among identities the host keyring already holds.
  It cannot introduce a key, reach a passphrase, or move signing off the host
  agent.
- `checkout_root` chooses where one bounded, already-authorized filesystem
  operation puts its result, within paths that cannot collide with the
  daemon's own deletion territory. It cannot choose what that operation is,
  what it runs, or what it may remove.

Admitting another identity-, credential-, or path-adjacent setting requires
its own architectural decision, not these precedents.

## Failures and recovery

| Condition | Response |
|---|---|
| Unknown key | `422` naming the key |
| Wrong value type — a non-boolean tier preference, for instance | `422` naming the key |
| A `gpg_signing_key` that is not a full fingerprint, or names no usable signing key | `422` naming the key; nothing in the update is persisted |
| A `checkout_root` that is relative, contains `..`, is the filesystem root, or is inside the task root | `422` naming the key; nothing in the update is persisted |

A multi-key update is validated in full before any value is written, so one
bad key leaves the whole update unapplied.

Clearing an override falls back to `config.toml`, or to the built-in default
when the file says nothing.

## Interfaces

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/settings` | The effective settings map |
| `PUT` | `/api/settings` | Set overrides |
| `DELETE` | `/api/settings/{key}` | Clear one override |

Selecting or clearing `gpg_signing_key` also broadcasts a fresh `gpg_status`.

Changes broadcast `settings_changed` with the full effective map. The
WebSocket snapshot carries the current effective settings.
