# Set up a development environment

This gets both halves of Ompire building and both test suites passing. Expect
15 minutes.

## Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Python | 3.12 or newer | Daemon runtime |
| `uv` | current | Manages the daemon's environment |
| Node | 24 | Frontend toolchain |
| `pnpm` | pinned in `frontend/package.json` | Managed by corepack |

Enable corepack so `pnpm` resolves to the pinned version rather than whatever
is installed globally:

```sh
corepack enable
```

The runtime tools Ompire drives — `gh`, `lxc`, `gpg` — are only needed to
*run* Ompire against real repositories, not to build it or run its unit tests.

## Build

```sh
make build
```

Or the two halves separately:

```sh
cd frontend && pnpm install && pnpm build
cd daemon && uv sync
```

The frontend is a standalone pnpm project with its own lockfile. There is
intentionally no root `package.json` — do not add one.

## Test

```sh
make test
```

Both suites must pass before committing:

```sh
cd daemon && uv run pytest        # backend
cd frontend && pnpm test          # frontend, vitest
```

## Lint and typecheck

```sh
make lint
make typecheck
```

The backend uses `ruff`, the frontend uses `oxlint`. Both are enforced.

## Run

```sh
make run
```

The daemon starts on `http://127.0.0.1:4173` and serves `frontend/dist` at
`/`. If you skipped the frontend build, the UI will not load.

Open it once with the generated token:

```sh
xdg-open "http://127.0.0.1:4173/?token=$(cat ~/.local/share/ompire/token)"
```

For frontend work, run Vite's dev server instead so you get hot reload:

```sh
cd frontend && pnpm dev
```

## Verify

Everything is working when:

1. `make test` passes both suites.
2. `make run` starts the daemon and the UI loads with a live daemon chip.
3. `make lint` and `make typecheck` are clean.

## What to read next

- [Architecture overview](../explanation/architecture.md) — read this before
  changing anything structural.
- [Repository layout](../reference/repository-layout.md) — where things live.
- [Deliver a change end to end](deliver-a-change.md) — the workflow used to
  land work in this repository.

## Working against real repositories

Unit tests do not need containers or credentials. Driving a real task does.
Two options:

- [Run the local end-to-end harness](../how-to/run-local-e2e.md) — the real
  daemon and frontend with executable fakes standing in for the network,
  containers, and the model.
- [Run the dogfooding QA loop](../how-to/run-the-qa-loop.md) — the real stack
  against a sandbox GitHub repository and bot account.

Never point the QA loop at a repository you care about.
