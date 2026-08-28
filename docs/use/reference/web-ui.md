# Web UI

## Overview

The frontend is presentation only. It holds no authoritative state, makes no
decisions, and can be closed and reopened at any point without affecting
running work.

Everything it renders comes from the daemon's snapshot and delta stream.
Everything it changes goes through REST.

## States and behavior

### Global chrome

Every route renders a sticky header: the logo, nav links for Tasks, Projects,
Spawn task, Ship flow, and Templates & settings, and a right-side chip group.

Task detail is deliberately absent from the nav — it is reached from a task,
not from a menu.

| Chip | Shows |
|---|---|
| "N need you" | Attention count, derived from the daemon's tier model |
| Daemon | WebSocket connection state |
| GPG | Real signing-key lock state |
| GitHub | Current daemon GitHub CLI identity state |

The GPG chip renders one label per signing state — `gpg ready` (with a
remaining cache lifetime only when the agent reports one), `gpg locked`,
`gpg unselected`, `gpg no key`, `gpg missing`, `gpg agent`, `gpg error`, or
`gpg —` — sourced from the snapshot's `gpg` entry and `gpg_status` events,
never a static placeholder. Its accessible description names the condition.

**Templates & settings → Daemon → Commit signing** shows the same state with
the selected key's fingerprint and user ID, how it was chosen, the last-check
time, the recovery action and terminal helper for the current state, and a
**Re-check key** control disabled while a request is in flight. When the host
holds more than one usable signing key it also offers a selector; choosing one
persists it and re-probes. No secret key material or passphrase appears there.

The GitHub chip renders `gh @login`, `gh missing`, `gh auth`, `gh error`, or
`gh —` from snapshot `gh` state and `gh_status` events. Its accessible
description is safe status text only. **Templates & settings** shows the same
state with the login, host, credential-source label, executable path, version,
last-check time, and sanitized failure detail. Its **Re-check GitHub** action
is disabled while a request is in flight.

### The WebSocket client

Connects, receives an authoritative snapshot, then applies deltas. On
disconnect it reconnects and receives a fresh snapshot.

A reconnect loses nothing, because the client never held anything the daemon
did not also hold. The daemon chip reflects connection state so the operator
can tell "nothing is happening" from "I am not being told what is happening".

### Shipping preflight

An actionable task's Ship flow requests a task-scoped GitHub recheck when its
registered upstream changes. The banner compares the daemon result to that
specific upstream and current identity; it never reuses an allowed result for
another target or account. It shows checking, ready, missing/authentication,
denied, and error recovery states, and **Sign & commit** requires both this
ready target result and a `ready` GPG key.

The banner says explicitly that GitHub API eligibility does not prove SSH or
HTTPS `git push` authentication. The daemon repeats every preflight; browser
state only controls presentation.
### Snapshot-gated routes

A route that needs to decide whether a task exists waits for the current
connection's first full snapshot. Socket open alone is not enough: it happens
before that message, and a reconnect replaces any previous projection. The
Ship flow index and `/ship/<task-id>` therefore render loading until a
snapshot; an unknown or non-numeric ship id after it provides recovery links
to Ship flow and Tasks. Any other unmatched application address renders a
**Page not found** surface inside the normal chrome rather than a blank view.

### Task sections

The Tasks view partitions visible tasks into three sections. A heading is
rendered only when its section is non-empty.

| Section | Contains |
|---|---|
| **Needs you** | `state` is `failed`, or an attention entry in the `interrupt` or `notify` tier |
| **Running** | An attention entry in the `silent` tier, or session status `starting`/`working` with no entry |
| **Idle/other** | All remaining non-archived, non-shipped tasks |

Shipped tasks keep their own separate section below.

### Sort order

Within **Needs you**: attention severity descending — `interrupt`, then
`notify`, then `badge` and `failed` — and `updated_at` descending within equal
severity.

Within **Running** and **Idle/other**: `updated_at` descending.

The severity-first ordering is the point of the section. The task that most
needs the operator is the top row, without them scanning for it.

### The attention chip

The "N need you" count is derived from the daemon's attention entries filtered
by the per-tier `badge` preference — not re-derived from raw session statuses.
The chip navigates to the attention-filtered Tasks view.

When attention clears, every surface updates together: the chip count, the tab
title, the section assignment, and the card styling. They cannot disagree,
because they all read one model.

### Theming

Design tokens with light and dark themes, sourced from the handoff bundle.

## Interfaces

The UI consumes the main WebSocket at `/api/ws` for registry, session,
workflow, review, ship, attention, and settings state, and per-session
channels at `/api/ws/agents/{task_id}/{session}` for transcripts.

It issues commands over REST. It sends nothing over any WebSocket.

Deep links work: any client-side route falls back to the SPA entry point when
no built file matches, so `/tasks/42` survives a reload or a pasted link.
