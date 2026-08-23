# Build, test, and run

Every target is available through `make`. Run `make` with no arguments to list
them.

## Build

```sh
make build              # both halves
make build-frontend     # cd frontend && pnpm build
make build-backend      # cd daemon && uv sync
```

The daemon serves `frontend/dist` as static files, so the frontend build is a
prerequisite for a working UI, not an optional step.

## Test

```sh
make test               # both suites
make test-backend       # pytest
make test-frontend      # vitest
```

Forward arguments with `ARGS`:

```sh
make test-backend ARGS="-k test_ship"
make test-frontend ARGS="--reporter=verbose"
```

Both suites must pass before committing.

## Lint

```sh
make lint               # both
make lint-backend       # ruff check src tests
make lint-frontend      # oxlint
```

## Typecheck

```sh
make typecheck          # both
make typecheck-backend  # mypy src
make typecheck-frontend # tsc -b
```

## Run

```sh
make run                # cd daemon && uv run ompire-daemon
```

Serves on `http://127.0.0.1:4173`. Open with the token once:

```sh
xdg-open "http://127.0.0.1:4173/?token=$(cat ~/.local/share/ompire/token)"
```

For frontend iteration, `pnpm dev` in `frontend/` gives hot reload against the
running daemon.

## Clean

```sh
make clean
```

Removes `frontend/dist`, `frontend/node_modules`, `daemon/.venv`, and the
Python and tool caches. It does not touch your data directory, so registered
projects, tasks, and the bearer token survive.

To reset daemon state as well, remove `~/.local/share/ompire` — this deletes
the database and the token.

## Before committing

```sh
make test && make lint && make typecheck
```

CI runs backend and frontend jobs conditionally based on which paths changed,
so a green local run of everything is the reliable signal.
