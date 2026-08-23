# GPG signing

## Overview

Ompire reports whether the operator's signing key is usable, as one shared
condition consumed by both the UI chip and the ship commit gate.

The design constraint that shapes everything here: **the probe must never
raise a pinentry prompt.** A daemon that could trigger a passphrase dialog
would be able to do so at any moment, from a background poll, with no
operator context for why.

## States and behavior

### The probe

The daemon derives the signing key's keygrip — from the configured
`gpg_signing_key`, falling back to `git config user.signingkey` — and probes
the GPG agent with a non-asking `KEYINFO` query, interpreting the cached flag.

| State | Meaning |
|---|---|
| `cached` | Passphrase cached in the agent. The key is usable. |
| `locked` | Key exists, cache is cold. |
| `unknown` | No agent, key, or keygrip could be resolved. Carries a detail. |

The probe runs on daemon startup, on operator recheck, and once immediately
before each ship commit.

The daemon **never** supplies the passphrase and never uses a loopback
pinentry. Unlocking is the operator's action, in a terminal.

### The commit gate

Only `cached` permits a ship commit. Both `locked` and `unknown` are refused
with `409` carrying the lock detail.

`unknown` failing closed is deliberate. An unresolvable key state is not
evidence that signing would work, and finding out halfway through a rewrite is
worse than not starting.

### Unlocking

The UI surfaces a terminal-helper instruction when the key is locked, of the
form:

```sh
echo | gpg --clearsign -u <key> >/dev/null
```

Running it raises the pinentry the operator expects, in the context they
expect it, and caches the passphrase. A "Re-check key" control then forces a
fresh probe.

## Configuration

| Key | Effect |
|---|---|
| `gpg_signing_key` | The signing key. Falls back to `git config user.signingkey` when unset. |

GPG agent cache lifetime is a GPG setting, not an Ompire one —
`default-cache-ttl` and `max-cache-ttl` in `~/.gnupg/gpg-agent.conf`.

## Interfaces

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/gpg` | Current status: state, signing key, and when locked, the unlock instruction |
| `POST` | `/api/gpg/recheck` | Force a fresh probe and return the updated status |

`gpg_status` is broadcast when the status changes, and the WebSocket snapshot
carries the current status as a `gpg` entry.

The chrome chip and the ship commit gate consume that same single state, so
they can never disagree about whether shipping is possible.
