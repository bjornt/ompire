# Configure GPG signing

Ompire signs every published commit with your key, on the host, outside the
agent's sandbox. Shipping is blocked until the key is usable, so this is
required before a task can produce a pull request.

## Check what Ompire sees

Open **Templates & settings → Daemon → Commit signing**. It shows the current
state, which key will sign, and how that key was chosen. The same state appears
as a `gpg` chip in the header on every page.

If the host holds exactly one usable signing key, Ompire selects it and there
is nothing to configure.

## Choose a key

When the host holds more than one signing key, Ompire will not guess — the
signing identity ends up on every published commit. The panel shows
`gpg unselected` and offers a **Signing key** selector listing each usable key
by user ID and key ID.

Pick one. The choice persists, takes effect immediately, and needs no restart.
Choosing **Detect automatically** clears it again.

To seed the choice before first run instead, put it in
`~/.config/ompire/config.toml`:

```toml
gpg_signing_key = "YOUR_KEY_ID"
```

That file is the lower layer: a selection made in Settings takes precedence
over it. Ompire also honours `git config user.signingkey` when neither says
anything.

Find the identifiers with:

```sh
gpg --list-secret-keys --keyid-format=long
```

## Cache the passphrase

A passphrase-protected key must be unlocked before Ompire can sign with it.
Sign anything once to prime the agent:

```sh
echo | gpg --clearsign -u YOUR_FINGERPRINT >/dev/null
```

Then press **Re-check key**, or:

```sh
curl -sS -X POST http://127.0.0.1:4173/api/gpg/recheck \
  -H "Authorization: Bearer $(cat ~/.local/share/ompire/token)"
```

The chip updates over the WebSocket as soon as the probe completes.

A key with no passphrase needs none of this. It has nothing to cache and is
ready as soon as it is selected.

## Fix a blocked state

Each state has its own fix. The UI shows the relevant one; this is the whole
set.

| State | What to do |
|---|---|
| `gpg unselected` | Choose a key in **Templates & settings → Commit signing**. |
| `gpg locked` | Run the unlock command above, then **Re-check key**. |
| `gpg agent` | Start the agent with `gpg-connect-agent /bye`, then re-check. |
| `gpg no key` | Generate or import a signing key for the account running the daemon. |
| `gpg missing` | Install GnuPG on the daemon's host, then restart the daemon. |
| `gpg error` | Read the reason shown in the panel. A selected key that was deleted, revoked, or expired reports here — select another. |

Shipping is refused in all of these, before any Git work starts, so nothing
needs undoing.

## When the cache expires

GPG agents drop cached passphrases after an idle timeout. A task that sat
overnight will find the key `locked` at ship time. Re-cache and re-probe; the
ship attempt is refused before anything is written.

To extend the window, raise `default-cache-ttl` and `max-cache-ttl` in
`~/.gnupg/gpg-agent.conf`. That is a GPG setting, not an Ompire one.

## Why the daemon signs

The agent never receives your signing key. It may draft the commit message and
pull-request text, but the daemon performs the commit, the signature, the
push, and the pull-request creation using host-side credentials.

For the same reason, Ompire ignores the signing configuration inside the task
clone — the agent can write there — and passes the key, signature format, and
signing program explicitly from your own configuration. It then verifies that
the commits it produced really carry your key's signature before pushing
anything. See [The trust boundary](../explanation/trust-boundary.md) and
[GPG signing states](../reference/gpg-signing.md).
