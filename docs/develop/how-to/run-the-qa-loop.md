# Run the dogfooding QA loop

The QA loop runs the complete real stack — real containers, real GitHub, real
signing — against a sandbox repository owned by a bot account.

**It never runs against real repositories.** Everything below assumes the
`ompire-test` bot account and its sandbox repository.

## Scripts

All in `scripts/`:

| Script | Purpose |
|---|---|
| `setup-qa-agent.sh` | Bot identity lifecycle — SSH and GPG keys on GitHub |
| `setup-qa-env.sh` | The QA environment itself — toolchain, containers, config, daemon |
| `qa-auth-tunnel.sh` | Forwards the auth gateway when QA runs on another host |

## Set up the bot identity

Run this on your own machine, with `gh` authenticated as the bot and holding
`admin:public_key` and `admin:gpg_key` scopes:

```sh
scripts/setup-qa-agent.sh
```

It creates the bot's SSH and GPG keys on GitHub. The agent token itself is a
repo-scoped fine-grained PAT, created separately.

Subcommands:

```sh
scripts/setup-qa-agent.sh status          # verify
scripts/setup-qa-agent.sh rotate ssh      # rotate one key type
scripts/setup-qa-agent.sh rotate all
scripts/setup-qa-agent.sh teardown        # clean up
```

## Set up the environment

```sh
scripts/setup-qa-env.sh
```

Installs the toolchain, prepares workshop and LXD, sets up the browser,
builds, writes the daemon configuration, registers the sandbox project, starts
the daemon, and runs smoke checks.

It is idempotent — re-run it freely.

If the QA host is not your machine, forward the auth gateway to it:

```sh
scripts/qa-auth-tunnel.sh user@qa-host
```

## Act as the bot

Any shell that needs the bot's identity:

```sh
. .qa-agent/env.sh
```

## The loop

1. Spawn a task through the UI, or `POST /api/tasks`.
2. Let the agent work.
3. Drive Review to approval.
4. Ship: draft, then sign and commit, then push and open the pull request.
5. Verify on the sandbox repository.

The verification that matters most: **commits must show as Verified on
GitHub.** An unsigned or badly signed commit means the publishing path is
broken even if every other step reported success.

## Recording findings

Findings go into the active change's artifacts under `changes/<name>/`. They
are working notes, not durable documentation — when the change is finished,
anything durable is reconciled into feature documentation or an ADR and the
directory is deleted.

See [The change workflow](../explanation/change-workflow.md).

## Relationship to the local harness

The QA loop and [the local end-to-end
harness](run-local-e2e.md) test different things and neither replaces the
other.

The local harness is fast, deterministic, and runs in CI, but its forge,
container, agent, and reviewer are fakes. The QA loop is slow and manual, but
everything in it is real — and it is the only thing that can tell you a fake
has drifted from the tool it stands in for.
