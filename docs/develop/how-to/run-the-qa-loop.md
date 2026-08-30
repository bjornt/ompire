# Run the dogfooding QA loop

The QA loop runs the complete real stack — real containers, real GitHub, real
signing — against a sandbox repository owned by a bot account. It is the only
thing that can tell you the publishing path actually works, because it is the
only place nothing is faked.

**It never runs against real repositories.** Everything below assumes the
`ompire-test` bot account and its sandbox repository. Every destructive action
in this guide targets that repository and nothing else.

For fast, deterministic, offline checks use [the local end-to-end
harness](run-local-e2e.md) instead. The two test different things; neither
replaces the other.

## The three planes

Every instruction below belongs to exactly one of three machines. Keeping them
apart is the whole security model of this procedure.

| Plane | Where it runs | What it holds | What it may do |
|---|---|---|---|
| **Management** | Your own machine | A `gh` login for the bot with `admin:public_key` and `admin:gpg_key` | Add and remove SSH/GPG keys on the bot account |
| **Agent identity** | One directory, copied to the QA host | A repo-scoped PAT, an SSH private key, a GPG private key | Clone, push, open PRs, and sign — only in the sandbox repository |
| **QA host** | A disposable VM | The agent identity, the daemon, Workshop/LXD, task agents | Everything the loop exercises |

Two rules follow, and the scripts enforce neither for you:

- **The management credential never reaches the QA host.** It lives in
  `~/.config/gh` on your machine. `setup-qa-agent.sh` reads it at run time and
  never writes it into the identity directory.
- **The QA host is assumed hostile.** It runs model-driven code with real
  credentials on disk. Treat it as disposable, and never give it anything the
  bot does not need.

Why the identity is a bot at all, and why signing and publishing stay outside
the agent sandbox:
[ADR-0017](../../adr/0017-use-dedicated-bot-as-default-publishing-identity.md)
and
[ADR-0011](../../adr/0011-keep-review-and-publishing-authority-outside-agent-sandbox.md).

## Scripts

All in `scripts/`:

| Script | Runs on | Purpose |
|---|---|---|
| `setup-qa-agent.sh` | Management machine | Bot identity lifecycle — SSH and GPG keys on GitHub |
| `setup-qa-env.sh` | QA host | The QA environment — toolchain, containers, config, daemon |
| `qa-auth-tunnel.sh` | Gateway side | Forwards the auth gateway to the QA host |

## Before you start

1. A disposable Ubuntu host you can destroy afterwards. It needs `sudo`, and it
   will end up running LXD.
2. A GitHub bot account, created once by hand, owning one sandbox repository.
3. `gh` on your own machine, logged in **as the bot**:

   ```sh
   gh auth login --web --scopes "admin:public_key,admin:gpg_key"
   ```

   If the bot is already a `gh` account:
   `gh auth refresh --user <bot> --scopes "admin:public_key,admin:gpg_key"`.
4. A fine-grained PAT for the bot, created by hand — GitHub has no API to mint
   one, and that manual step is what buys hard per-repository confinement:

   | PAT setting | Value |
   |---|---|
   | Repository access | Only the sandbox repository |
   | Repository permissions | Contents RW, Pull requests RW, Issues RW, Actions RW, Workflows RW (Metadata R implicit) |
   | Account permissions | None — key management stays on the management plane |

   PATs expire within about a year; replace one with
   `setup-qa-agent.sh rotate token`.

## 1. Create the bot identity

On the **management machine**, in a checkout of this repository:

```sh
scripts/setup-qa-agent.sh --repo <owner>/<sandbox> setup
```

It creates the bot's SSH and GPG keys, uploads their public halves to the
account, and verifies the PAT can actually reach the repository before
uploading anything. `--repo` is repeatable.

Subcommands:

```sh
scripts/setup-qa-agent.sh status          # verify both planes
scripts/setup-qa-agent.sh rotate ssh      # rotate one key type
scripts/setup-qa-agent.sh rotate all
scripts/setup-qa-agent.sh teardown        # see Teardown below
```

`status` is the command to reach for whenever something authentication-shaped
breaks. It prints the agent login, repository reachability, the management
login, local-vs-state-vs-remote key agreement for both key types, and a live
SSH authentication attempt against `git@github.com`.

## 2. Move the identity to the QA host

The identity directory is self-locating — `env.sh` resolves paths from its own
location — so copying it needs no regeneration:

```sh
rsync -a .qa-agent/ <qa-host>:~/ompire/.qa-agent/
```

Copy it over SSH; do not paste its contents anywhere, and do not route it
through a chat, an issue, or a pastebin. Confirm afterwards that the directory
is `0700` and its private files are `0600`.

Put the repository itself on the host the same way, or clone it there.

## 3. Set up the QA environment

On the **QA host**, from the repository root:

```sh
scripts/setup-qa-env.sh --check
```

It verifies the identity directory, adds `.qa-agent/` to `.gitignore`, installs
`gh`, the Node/pnpm/uv toolchain, and the Workshop/LXD stack, provisions a
headless browser, builds the frontend, writes `~/.config/ompire/config.toml`
with the bot's signing key, clones the sandbox repository to `~/proj/<name>`,
registers it as a project with a matching template, starts the daemon, and runs
smoke checks. `--check` additionally runs both test suites.

It is idempotent — re-run it freely. Useful options: `--repo <owner>/<sandbox>`
when the PAT can see more than one repository, `--skip-workshop` and
`--skip-browser` to shorten a re-run, `--dir PATH` for an identity directory
outside the repository.

`--check` aborts the run before the smoke block if either suite fails. The
daemon suite exercises real subprocesses and is sensitive to a loaded host, so
reproduce a failure with a plain `cd daemon && uv run pytest` on a quiet
machine before treating it as a real one.

The run ends with a smoke block. Every line must read `ok`; see
[Troubleshooting](#troubleshooting) for the ones that do not.

On a host where LXD was just installed, your user was added to the `lxd` group
and **that does not apply to the current session**. Log out and back in (or
start a new login shell) before spawning tasks, or Workshop launches fail.

## 4. Connect the auth gateway

Task agents get model credentials through the `pi-auth-gateway` tunnel declared
in `workshop.yaml`. They never receive the credential itself
([ADR-0015](../../adr/0015-keep-agent-credentials-behind-narrow-brokers.md));
the gateway holds it and exposes only the capability.

The gateway listens on **its own** host's `localhost:4000`, and the QA host
needs it on *its* `localhost:4000`. From the gateway side:

```sh
scripts/qa-auth-tunnel.sh ubuntu@<qa-host>
```

The loop re-establishes the forward on disconnect; Ctrl-C stops it. Without it
the environment still comes up, but every task agent fails with no model
access.

## 5. Open the UI

The daemon binds to `127.0.0.1:4173` on the QA host and is never exposed to the
network. Forward it:

```sh
ssh -N -L 4173:127.0.0.1:4173 ubuntu@<qa-host>
```

Then open the tokenized URL `setup-qa-env.sh` printed. The `?token=` stashes
itself in the browser's local storage, so it is needed once per browser
profile. To drive the UI from an agent rather than by hand, see [Drive the UI
in a browser](run-local-e2e.md#drive-the-ui-in-a-browser).

## What is secret

Everything below lives in the identity directory. It is one directory so that
it is one thing to protect, transfer, and delete.

| File | Purpose | Mode |
|---|---|---|
| `token` | The bot's fine-grained PAT | 0600 |
| `.curlrc`, `.headers` | curl config and response headers carrying the PAT | 0600 |
| `id_ed25519`, `id_ed25519.pub` | SSH key; the public half is on the account | 0600 |
| `gnupg/` | Dedicated GPG home holding the signing key | 0700 |
| `.gpg-passphrase` | Passphrase for that key | 0600 |
| `gpg-public.asc` | Armored public signing key, for reference | 0644 |
| `gitconfig` | Bot name, email, signing key, `commit.gpgsign` | 0600 |
| `known_hosts` | `github.com` host keys | 0644 |
| `env.sh` | Sourcing it makes a shell act as the bot | 0600 |
| `state.json` | Remote key ids and fingerprints, for rotation | 0600 |

To act as the bot in any shell on the QA host:

```sh
. .qa-agent/env.sh
```

The rules:

- `.qa-agent/` is in `.gitignore`, and `setup-qa-env.sh` re-adds it. Never
  commit it, and never commit a sanitized example of it either.
- **Never reproduce a value.** Not in findings, logs, issues, commit messages,
  pull requests, screenshots, or an agent transcript. Fingerprints, key ids,
  and the bot login are safe to quote; everything in the table above is not.
- Rotate on any suspicion, and cheaply: `rotate ssh`, `rotate gpg`,
  `rotate token`, or `rotate all`.
- The GPG key is passphrase-protected and its passphrase sits next to it. That
  is not for secrecy — it is because the ship gate needs a *cached* key, and
  only a protected key can be cached. The daemon wrapper warms the agent at
  startup.
- The fingerprint the daemon reports is the **signing subkey**, while
  `config.toml` and `state.json` hold the **primary** key. They are supposed to
  differ; `/api/gpg` shows the subkey as `selected.fingerprint` and the primary
  as `candidates[].primary_fingerprint`.

## The loop

Work through this in order. Each item names the observation that proves it —
an action that ran is not evidence that it worked.

| # | Step | Proof |
|---|---|---|
| 1 | The sandbox project is registered | It appears in Projects with its checkout path and upstream |
| 2 | Spawn a task from the sandbox template | The task reaches its own Workshop container and the transcript streams |
| 3 | Answer the agent, if it asks | The session leaves `needs you` and resumes working |
| 4 | Approve the gate, if the workflow has one | The workflow advances past the gate. `single-step` has no gate; the bugfix workflow does |
| 5 | Start Review | llmvet opens; the review URL is reachable from the task detail page |
| 6 | Drive review to approval | Review state reads approved on both task detail and Ship flow |
| 7 | Ship flow drafts commit message and PR text | All three fields populate without pressing anything |
| 8 | Check the task's GitHub banner | `@ompire-test` and the sandbox repository, ready |
| 9 | Sign and commit | The commit exists in the task clone and `git log --show-signature` reports a good signature |
| 10 | The same action pushes and opens the pull request | Ship flow shows `commit`, `push`, and `pr` steps; the PR exists on the sandbox repository |
| 11 | **The commit shows Verified on GitHub** | The badge on the commit page, not the daemon's own report |
| 12 | Merge the pull request | Merge polling moves the task to a terminal state |
| 13 | Clean up the task | The Workshop container and the task clone are gone |
| 14 | Restart the daemon | Tasks, pull-request state, and review history come back. Draft text is not durable yet, so re-drafting after a restart is expected, not a fault |

Step 11 is the one that matters most. An unsigned or badly signed commit means
the publishing path is broken no matter how green everything else looked.

## Verify GitHub identity preflight

Only on the disposable bot-owned sandbox, verify the failure and recovery path
before a normal ship:

1. Spawn or select a sandbox task and record its clone `HEAD`, worktree state,
   and absence of `refs/ompire/ship-orig`.
2. Start the daemon once with a deliberately invalid `GH_TOKEN` in its launch
   environment. In the browser, confirm the chrome and Settings show `gh auth`
   without token material and Ship flow disables **Sign & commit**.
3. Send the ordinary ship-commit request. It must return safe `409` GitHub
   status before a ship job, commit event, `HEAD` change, worktree change, or
   `refs/ompire/ship-orig` change.
4. Stop that daemon and restart the normal QA wrapper, which sources
   `.qa-agent/env.sh`. Re-check GitHub in Settings and the task Ship flow;
   both must report `gh @ompire-test` and the canonical sandbox target ready.
5. Complete the normal signed ship and confirm the pull request exists on the
   sandbox repository. Keep the GitHub API eligibility result distinct from
   SSH or HTTPS push authentication.

Capture only safe UI/API/event evidence. Never copy the invalid token, bot
token, authorization header, or credential-bearing URL into a recording.

## Troubleshooting

Keyed to what the scripts actually print.

| What you see | What it means | Fix |
|---|---|---|
| `missing .qa-agent/<file> — run setup-qa-agent.sh setup first` | The identity never arrived, or arrived partially | Re-copy the whole directory; a partial `rsync` of dotfiles is the usual cause |
| `env.sh produced an empty GH_TOKEN` | `token` is unreadable or empty | Check the mode and ownership on the QA host |
| `GNUPGHOME (...) does not exist — stale absolute paths?` | An `env.sh` from before it became relocatable | Regenerate it with `setup-qa-agent.sh setup` on the management machine |
| `the PAT sees no repos` / `sees multiple repos` | Repository selection is wrong or ambiguous | Fix the PAT's selection, or pass `--repo <owner>/<sandbox>` |
| `added <user> to group lxd — takes effect on next login` | LXD was just installed | Start a new login shell before spawning; the current one cannot reach LXD |
| `daemon died during startup — see .../daemon.log` | The daemon crashed before readiness | Read `~/.local/share/ompire/daemon.log`; the pid is in `daemon.pid` |
| `agent PAT returned '<login>'` | The PAT belongs to another account | Re-issue it from the bot account |
| `daemon GitHub identity: state=..., login=...` | The daemon's launch environment resolves a different identity than the PAT | Restart it through `~/.config/ompire/qa-daemon.sh`, which sources `env.sh` |
| `ssh ls-remote <repo>` fails | The bot's SSH key is gone from the account | `setup-qa-agent.sh status`, then `rotate ssh` |
| `daemon GPG probe: locked` | The key is cold | Warm it with the command in `env.sh`'s header, then restart the daemon |
| `daemon GPG probe: agent_unavailable` | No `gpg-agent` on the host | `gpg-connect-agent /bye`, then re-check |
| `daemon GPG probe: no_key` / `ambiguous` | The configured key is missing, or several are usable | Set the right fingerprint in `~/.config/ompire/config.toml`, or choose one in Settings |
| `commits show Unverified (bad_email)` | The bot's noreply address is inactive | Enable *Keep my email addresses private* in the bot's email settings, then push again |
| `daemon serves the placeholder page` | `frontend/dist` is missing | Re-run without `--skip-browser`; the build step produces it |
| `auth gateway NOT reachable on :4000` | No tunnel | Start `qa-auth-tunnel.sh` from the gateway side; until then agents have no model access |
| No browser for a UI check | The host has no Chrome | [Drive the UI in a browser](run-local-e2e.md#drive-the-ui-in-a-browser) |

## Recording findings

Findings go into the active change's artifacts under `changes/<name>/`. They
are working notes, not durable documentation — when the change is finished,
anything durable is reconciled into feature documentation or an ADR and the
directory is deleted.

See [The change workflow](../explanation/change-workflow.md).

## Teardown

Two halves with different consequences. The first is repeatable; the second
touches a live GitHub account.

### The environment

On the QA host:

```sh
kill "$(cat ~/.local/share/ompire/daemon.pid)"   # stop the daemon
lxc list --project workshop.ubuntu               # leftover task containers, named qa-agent-*
rm -rf ~/tasks/<project> ~/proj/<name>           # task clones and the sandbox checkout
rm -rf ~/.local/share/ompire ~/.config/ompire    # daemon state (including db/) and config
```

Prefer cleaning tasks through the UI first — it removes each Workshop container
along with its clone, so `lxc list` should come back empty. `workshop list` is
not the command for this: it only works inside a project directory and reports
that project's own workshops, not the task agents'. Destroying the whole host is
also a legitimate teardown, and the cheapest one.

### The identity

From the management machine:

```sh
scripts/setup-qa-agent.sh teardown                  # remove the keys from GitHub
scripts/setup-qa-agent.sh teardown --delete-local   # ...and wipe the local directory
```

It deletes the SSH and GPG keys it created and warns about any other
`qa-agent-*` keys it finds. It refuses to delete `/` or `$HOME`, and it touches
nothing but the keys recorded in `state.json` — it cannot remove a key you
created for something else.

**The PAT is not revocable through the API.** Delete it by hand in the bot
account at `https://github.com/settings/tokens?type=beta` (and an OAuth device
token, if one was ever used, at `https://github.com/settings/applications`).
The script prints both URLs.

Prove the result:

```sh
scripts/setup-qa-agent.sh status
```

After a complete teardown it reports the token as invalid and both keys as
missing. That output — not the fact that teardown ran — is the evidence that
nothing is left on the account.

## Relationship to the local harness

The QA loop and [the local end-to-end harness](run-local-e2e.md) test different
things and neither replaces the other.

The local harness is fast, deterministic, and runs in CI, but its forge,
container, agent, and reviewer are fakes. The QA loop is slow and manual, but
everything in it is real — and it is the only thing that can tell you a fake
has drifted from the tool it stands in for.
