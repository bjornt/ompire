# AGENTS.md

ompire: Python daemon (FastAPI, `daemon/`) + React/TS frontend (`frontend/`).
The daemon spawns `omp` agents in workshop containers and drives
review → signed commit → push → PR ("ship flow").

## Build

```sh
cd frontend && pnpm install && pnpm build   # daemon serves frontend/dist at /
cd daemon && uv sync                        # python >= 3.12
```

Toolchain: node 24; pnpm is managed by corepack and pinned via `frontend/package.json#packageManager`.
`openspec` is a **global** tool, never a project dependency.

## Test

```sh
cd daemon && uv run pytest                  # 256 tests
cd frontend && pnpm test                    # 98 tests (vitest)
cd frontend && pnpm lint                    # oxlint
```

Run these before committing. Frontend is a standalone pnpm project (own
lockfile) — there is intentionally no root package.json.

## Run

```sh
cd daemon && uv run ompire-daemon           # http://127.0.0.1:4173
```

Open the UI once with `http://127.0.0.1:4173/?token=$(cat
~/.local/share/ompire/token)` (stashes itself in localStorage). Config:
`~/.config/ompire/config.toml` (`gpg_signing_key`, `agent_env`, …).
Ship flow needs: a registered project with `upstream_url`, `gh` on PATH, and
a GPG-cached signing key (the gate refuses `locked`/`unknown` keys).

## QA (dogfooding)

QA runs against the `ompire-test` GitHub bot account and its sandbox repo —
never against real repos. Scripts live in `scripts/`:

- `scripts/setup-qa-agent.sh` — identity lifecycle (operator's machine, needs
  `gh` authed as the bot with `admin:public_key,admin:gpg_key`): creates the
  bot's SSH+GPG keys on GitHub; the agent token is a repo-scoped fine-grained
  PAT. `rotate ssh|gpg|all` rotates keys; `status` verifies; `teardown` cleans.
- `scripts/setup-qa-env.sh` — QA environment: toolchain, workshop/LXD,
  browser, deps+build, daemon config, registers the sandbox project, starts
  the daemon, runs smoke checks. Idempotent; re-run freely.
- `scripts/qa-auth-tunnel.sh <user@host>` — forwards the auth gateway to a QA
  host (`ssh -R 4000`) when it runs elsewhere.
- Any shell acting as the bot: `. .qa-agent/env.sh`.

The QA loop: spawn a task via the UI or `POST /api/tasks`, drive Review to
approval, then Ship (draft → Sign & commit → push+PR). Verify results on the
sandbox repo (commits must show Verified). Record findings in the active
openspec change's `findings-*.md`.
