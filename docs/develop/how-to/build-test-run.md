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

### Architecture dependency checks

`daemon/tests/test_architecture.py` runs as part of `make test-backend` —
there is no separate command and no generated baseline. It statically parses
every daemon module and fails the suite when an import crosses an enforced
ownership boundary: platform modules importing product code, isolation
importing anything but the platform foundation and itself, cross-owner
imports of an isolation submodule or of a symbol outside its declared public
surface, work modules importing transport or projections, application
commands importing transport, work routers importing storage or execution
managers, or any code importing a retired module path or a symbol from its
retired location (for example the workspace guard or the Git helpers, whose
canonical home is now `isolation` and `platform/git`).

Run it alone while iterating on a boundary:

```sh
make test-backend ARGS="tests/test_architecture.py"
```

The checker also carries synthetic self-tests that prove each rule rejects
what it claims to reject, so a policy regression fails visibly rather than
silently allowing everything.

When a violation is intentional migration debt, add it to the checked-in
`EXCEPTIONS` table in that test — the importing module, the imported module
and symbol, and the later change that owns removing it. An exception whose
import disappears fails as *stale*, so paid-off debt cannot linger as
permission for its return. The checker cannot establish SQL column ownership
or runtime behavior; code review and the behavioral suites cover those.

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
