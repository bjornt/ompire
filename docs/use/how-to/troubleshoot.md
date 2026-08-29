# Troubleshoot the daemon

## The daemon will not start

Ompire refuses to start on a bad configuration file rather than ignoring what
it cannot parse. The error names the problem:

```
ompire-daemon: unknown config key(s) in /home/you/.config/ompire/config.toml: gpg_key
```

Check the key against [Configuration](../reference/configuration.md). A
misspelled key is an error, not a silently ignored line.

Malformed TOML and wrong value types fail the same way, naming the key and
what it received.

## The UI loads but shows nothing

The frontend needs the bearer token. If `localStorage` was cleared or you are
on a different browser, re-open with the token in the query string:

```sh
xdg-open "http://127.0.0.1:4173/?token=$(cat ~/.local/share/ompire/token)"
```

If the token was rotated, every open WebSocket is closed with code `1008` and
every client must re-authenticate with the new token.

## The daemon chip shows disconnected

The WebSocket dropped. The frontend reconnects on its own and receives a fresh
authoritative snapshot when it does — no state is lost by a reconnect, and
running work is unaffected. If it stays disconnected, the daemon process is
gone; check its logs.

## A task failed during spawn

The failing step and its stderr are attached to the task. The four steps fail
for characteristic reasons:

| Step | Common cause |
|---|---|
| `fetch` | The project's checkout has gone missing, or its [fetch remote](../reference/projects.md#fetch-remote) is unreachable |
| `clone` | No write access to `task_dir_root`, or the target path exists |
| `branch` | `origin/<base_branch>` does not exist — check the template's base branch |
| `workshop` | Container tooling unavailable, or the launch exceeded its timeout |

A `workshop` failure after a successful launch usually means the container
started but did not register; the lock file was missing or unreadable.

## A project will not register

Adopting a checkout validates it, so the refusal names the problem: the path
does not exist, is not the top level of a git work tree, has no remote with
the name you gave, or has no commits yet. Fix it in your own repository —
Ompire never edits a checkout it did not create — and submit again.

A URL refusal means the form is not one of `https://`, `ssh://`, or
`git@host:owner/repo`. Local paths and `git://` are deliberately not accepted.

## A project is stuck on "cloning"

It is not: the card resolves either way. If the daemon was restarted mid-clone,
the next startup marks the project `failed` with "interrupted by daemon
restart" and you can retry from the card. A clone is never resumed
automatically.

A failed clone shows git's own stderr. The usual causes are an unreachable or
private repository — the clone uses your git configuration with prompts
disabled, so anything needing a password fails immediately rather than
hanging — or no write access to the checkout root.

## Spawn refuses with "project is not ready"

The project's checkout setup has not finished, or it failed. Open the Projects
view and either wait for the clone or retry it. See
[Projects](../reference/projects.md#setup-state).

## Shipping is refused

Usually the GPG key. `GET /api/gpg` reports the current state, and anything
other than `ready` blocks a commit. The state names which problem it is —
`locked` (cold passphrase cache), `ambiguous` (several keys, none selected),
`no_key`, `missing` (GnuPG not installed), `agent_unavailable`, or `error` —
and each has a different fix. See [Configure GPG
signing](configure-gpg-signing.md) for the table.

The GitHub CLI identity is the other common cause; it is refused separately and
names the account and repository.

The other refusals are a ship already in flight for that task, an unsupported
mode, or unmet `retain` preconditions. All are reported with a reason, and all
are refused before any Git operation runs.

## A session looks stuck

A session that has been silent past the stall threshold — 300 seconds by
default — is reported as `stalled` and raised to the `notify` tier. That is a
heuristic, not a fact: a long-running build looks identical to a wedged agent.

Check the session's live output before intervening. You can steer it, send a
follow-up, or interrupt it from the task detail view.

Raise `stall_threshold` if your work legitimately involves long silences.

## No desktop notifications

Ompire uses `notify-send`. It degrades in stages and logs which stage it hit:

- `notify-send` not on `PATH` — notifications disabled, badge count still works.
- No reachable D-Bus session bus — same.
- `notify-send` without `--action` support — notifications appear without the
  Open button.

The badge count and the tab title are the reliable signal in all cases. Set
`notifications_enabled = false` to turn desktop notifications off deliberately.

## The daemon was killed mid-task

On restart, Ompire re-establishes what it can. Sessions being recovered start
as `starting` and settle into `idle` or `failed` once the resumed agent is
ready or fails. Recovery fan-out is bounded — four concurrent resumes by
default — because each one is a real container-side agent startup.

A review or ship interrupted mid-sequence is restored from its durable Git ref
at startup. If a task's clone looks wrong after a crash, check for
`refs/ompire/review-orig` or `refs/ompire/ship-orig` in it; their presence
means a restore has not completed.

Session status itself does not survive a restart — it is in-memory state that
is rebuilt, not replayed.

## Finding the details

```sh
curl -sS http://127.0.0.1:4173/api/daemon/info \
  -H "Authorization: Bearer $(cat ~/.local/share/ompire/token)"
```

Returns the version, bind address, port, config path, data directory, and the
audit log path when one exists.
