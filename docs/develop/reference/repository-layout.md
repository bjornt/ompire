# Repository layout

```text
daemon/            FastAPI control plane (Python 3.12)
  src/ompire_daemon/
  alembic/         schema migrations
  tests/           pytest suite
frontend/          React + TypeScript presentation layer
  src/
  dist/            build output, served by the daemon at /
local-test/        end-to-end harness, executable fakes, recordings
  scenarios/       runbooks and the run driver
  fakes/           substituted tool implementations
  recordings/      captured real-tool contracts
docs/              documentation
  use/             operator documentation set
  develop/         contributor documentation set
  adr/             architecture decision records
scripts/           QA, dogfooding, and local browser provisioning
snap/              classic-confinement snap packaging
design/handoff/    original design bundle and UI mockups
changes/           temporary change artifacts, deleted when finished
```

## Root files

| File | Purpose |
|---|---|
| `VISION.md` | Long-term product direction |
| `AGENTS.md` | Short orientation for coding agents |
| `Makefile` | Build, test, lint, typecheck, run |
| `workshop.yaml` | Container definition, including the headless browser SDK |
| `README.md` | Entry point |

`ROADMAP.md` and `ADR.PLAN.md` are planning documents, not documentation.

## Two independent projects

The frontend is a standalone pnpm project with its own lockfile. There is
intentionally no root `package.json`, and adding one would break the
separation.

The daemon is a `uv` project. Its dependency set is deliberately small —
FastAPI, uvicorn, Pydantic, SQLAlchemy, Alembic — because everything in it
sits inside the trust boundary and has to be auditable.

## Data locations

Nothing the daemon writes lives in the repository.

| Path | Contents |
|---|---|
| `~/.config/ompire/config.toml` | Operator configuration |
| `~/.local/share/ompire/` | Database, bearer token, audit log |
| `~/tasks/<project>/<slug>/` | Task clones |
| `~/proj/<project>/` | Project checkouts, by default |

Under the snap, the data directory is `$SNAP_USER_DATA` instead.
