# GPG signing

## Overview

Ompire reports whether the operator's signing key is usable, as one shared
condition consumed by the UI chip, the Templates & settings panel, and the ship
commit gate.

The design constraint that shapes everything here: **the probe must never
raise a pinentry prompt.** A daemon that could trigger a passphrase dialog
would be able to do so at any moment, from a background poll, with no
operator context for why.

## States and behavior

### Enumeration and selection

The daemon enumerates the signing-capable secret keys in its own keyring. A key
qualifies when its own capabilities include signing, its secret half is
present, and it is not revoked, expired, disabled, or otherwise invalid. A
certify-only primary with a signing subkey yields the subkey; a primary that
signs directly yields itself.

The key that signs is resolved in this order:

```text
Settings selection  →  config.toml  →  git config user.signingkey  →  automatic
```

Automatic detection selects a key only when exactly one usable candidate
exists. With several and no selection, the daemon reports `ambiguous` and
refuses to guess — the signing identity appears on every published commit, so
choosing it is the operator's call. Clearing the Settings selection returns to
the next layer down.

A selection that no longer resolves — the key was deleted, revoked, or expired
— reports `error` and names it. The daemon never falls back to a different key,
because signing as an identity the operator did not choose is worse than
stopping.

`config.toml` and `git config` accept the identifier forms GPG accepts:
fingerprint, long or short key ID, or a user-ID substring. The Settings
selection is stored as a full fingerprint, since only a fingerprint names
exactly one key. See
[ADR-0021](../../adr/0021-admit-signing-key-selection-as-bounded-daemon-writable-setting.md).

### The states

| State | Meaning | Shipping |
|---|---|---|
| `ready` | The selected key can sign right now | Allowed |
| `locked` | Passphrase-protected key, cold agent cache | Refused |
| `ambiguous` | Several usable keys, none selected | Refused |
| `no_key` | No signing-capable secret key in the keyring | Refused |
| `missing` | `gpg` or `gpg-connect-agent` is not executable | Refused |
| `agent_unavailable` | The tools run but the agent is unreachable | Refused |
| `error` | Any other indeterminate result; carries a reason | Refused |
| `unknown` | No probe has completed yet | Refused |

Only `ready` permits a ship commit. Everything else fails closed: an
unresolvable key state is not evidence that signing would work, and finding out
halfway through a history rewrite is worse than not starting.

Each state carries its own recovery, because the fixes are not
interchangeable — a locked key and a stopped agent need different commands, and
an absent key needs neither.

### Protection, not just cache state

The agent is asked about the selected key with a non-asking `KEYINFO` query.
The daemon reads both the cache flag and the protection field:

| Protection | Cached | State |
|---|---|---|
| Passphrase-protected | yes | `ready` |
| Passphrase-protected | no | `locked` |
| Unprotected | — | `ready` |

A key with no passphrase has nothing to cache and never reports cached. It is
signing-ready regardless, and reading the cache flag alone would report it
locked forever.

A remaining cache lifetime is shown only when the agent reports one. Nothing is
inferred when it does not.

### The commit gate

The daemon signs with the resolved key named explicitly, using the signature
format and signing program from the operator's own Git configuration. The
per-task clone's Git configuration is ignored for all three: it is writable by
the agent, and a clone-local `gpg.program` would otherwise make the daemon run
an arbitrary binary on the host.

After signing and before any push, the daemon verifies that every commit it
produced carries a signature made by the selected fingerprint. A mismatch fails
the ship and restores the pre-ship state.

### Unlocking

When the key is locked, the UI surfaces a terminal-helper instruction:

```sh
echo | gpg --clearsign -u <fingerprint> >/dev/null
```

Running it raises the pinentry the operator expects, in the context they
expect it, and caches the passphrase. A **Re-check key** control then forces a
fresh probe.

When the agent itself is unreachable, the portable helper is:

```sh
gpg-connect-agent /bye
```

The daemon **never** supplies the passphrase and never uses a loopback
pinentry. Unlocking is the operator's action, in a terminal.

## Configuration

| Key | Effect |
|---|---|
| `gpg_signing_key` | The signing key. Selectable in Templates & settings, which takes precedence over this file. |

GPG agent cache lifetime is a GPG setting, not an Ompire one —
`default-cache-ttl` and `max-cache-ttl` in `~/.gnupg/gpg-agent.conf`.

## Interfaces

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/gpg` | Current status: state, selected key, candidates, detail |
| `POST` | `/api/gpg/recheck` | Force a fresh probe and return the updated status |

Selection uses the ordinary [settings interfaces](daemon-settings.md):
`PUT /api/settings` with `gpg_signing_key`, and
`DELETE /api/settings/gpg_signing_key` to return to automatic detection. Both
re-probe immediately. A fingerprint that is not a currently usable signing key
is rejected with `422` before anything is stored.

The status carries public identifiers only — fingerprint, key ID, user ID,
keygrip, validity dates. It never carries secret key material, a passphrase, or
an agent socket path.

`gpg_status` is broadcast when the status changes, and the WebSocket snapshot
carries the current status as a `gpg` entry. The chrome chip, the Settings
panel, and the ship commit gate consume that same single state, so they can
never disagree about whether shipping is possible.
