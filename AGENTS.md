# AGENTS.md

ompire: Python daemon (FastAPI, `daemon/`) + React/TS frontend (`frontend/`).
The daemon spawns `omp` agents in workshop containers and drives
review → signed commit → push → PR ("ship flow").

Full contributor documentation: [`docs/develop/`](docs/develop/index.md).
Start with [Architecture overview](docs/develop/explanation/architecture.md).

## Commands

```sh
make build        # frontend build + daemon deps
make test         # pytest + vitest — both must pass before committing
make lint         # ruff + oxlint
make typecheck    # mypy + tsc
make run          # daemon on http://127.0.0.1:4173
```

Toolchain: node 24; pnpm is managed by corepack and pinned via
`frontend/package.json#packageManager`. The frontend is a standalone pnpm
project with its own lockfile — there is intentionally no root
`package.json`.

Details: [Build, test, and run](docs/develop/how-to/build-test-run.md).

## Run

Open the UI once with
`http://127.0.0.1:4173/?token=$(cat ~/.local/share/ompire/token)` (it stashes
itself in localStorage). Config: `~/.config/ompire/config.toml`.

Ship flow needs a registered project with `upstream_url`, `gh` on PATH, and a
GPG-cached signing key — the gate refuses `locked`/`unknown` keys.

See [Configuration](docs/use/reference/configuration.md).

## Testing beyond unit tests

- [Local end-to-end harness](docs/develop/how-to/run-local-e2e.md) — real
  daemon and frontend, executable fakes for network, containers, and the
  model. `local-test/scenarios/run --all`.
- [Dogfooding QA loop](docs/develop/how-to/run-the-qa-loop.md) — the real
  stack against the `ompire-test` bot and its sandbox repo. **Never against
  real repos.**

## Delivering a change

Changes go through the lightweight skills-based workflow: `change-propose` →
`change-implement` → `change-finish`, using `changes/<name>/SPEC.md` and
`PLAN.md`, which are deleted when the change is finished.

Durable knowledge goes to:

| Kind | Location |
|---|---|
| Current behavior | [`docs/features/`](docs/features/) |
| Durable rationale | [`docs/adr/`](docs/adr/) |
| Long-term direction | [`VISION.md`](VISION.md) |

See [The change workflow](docs/develop/explanation/change-workflow.md),
[Write a feature document](docs/develop/how-to/write-a-feature-doc.md), and
[Write an ADR](docs/develop/how-to/write-an-adr.md).

OpenSpec is being retired; `openspec/` is a migration source, not a live
workflow.
