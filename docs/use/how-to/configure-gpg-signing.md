# Configure GPG signing

Ompire signs every published commit with your key, on the host, outside the
agent's sandbox. Shipping is blocked until the key is available, so this is
required before a task can produce a pull request.

## Set the key

Put the signing key in `~/.config/ompire/config.toml`:

```toml
gpg_signing_key = "YOUR_KEY_ID"
```

Find the identifier with:

```sh
gpg --list-secret-keys --keyid-format=long
```

Restart the daemon after editing the file. Configuration is read once at
startup.

## Key states

Ompire probes the GPG agent and reports one of three states, shown as a chip
in the UI and available from `GET /api/gpg`:

| State | Meaning |
|---|---|
| `cached` | The key's passphrase is cached in the agent. Shipping is allowed. |
| `locked` | The key exists but the agent has no cached passphrase. Shipping is refused. |
| `unknown` | The key, the agent, or the probe itself could not be resolved. Shipping is refused. |

Only `cached` permits a commit. The gate fails closed: an unknown state is
treated as unusable rather than attempted and failed halfway.

## Cache the passphrase

Sign anything once to prime the agent:

```sh
echo test | gpg --clearsign > /dev/null
```

Then re-probe from the UI, or:

```sh
curl -sS -X POST http://127.0.0.1:4173/api/gpg/recheck \
  -H "Authorization: Bearer $(cat ~/.local/share/ompire/token)"
```

The chip updates over the WebSocket as soon as the probe completes.

## When the cache expires

GPG agents drop cached passphrases after an idle timeout. A task that sat
overnight will find the key `locked` at ship time. Re-cache and re-probe; the
ship attempt is refused before anything is written, so nothing needs undoing.

To extend the window, raise `default-cache-ttl` and `max-cache-ttl` in
`~/.gnupg/gpg-agent.conf`. That is a GPG setting, not an Ompire one.

## Why the daemon signs

The agent never receives your signing key. It may draft the commit message and
pull-request text, but the daemon performs the commit, the signature, the
push, and the pull-request creation using host-side credentials. See [The trust
boundary](../explanation/trust-boundary.md).
