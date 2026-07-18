# ompire-daemon

The ompire daemon: a long-lived local service that owns process lifecycle,
the project registry, auth, and the REST/WebSocket API. See
`openspec/changes/add-daemon-skeleton/design.md` for the design rationale.

## Dependencies

| Package | Purpose |
| --- | --- |
| `fastapi` | HTTP/WebSocket routing, request validation via Pydantic models |
| `uvicorn` | ASGI server that runs the FastAPI app |
| `pydantic` | Request/response model validation (REST bodies, config) |
| `sqlalchemy` | Core (no ORM) — table metadata and query building for the SQLite registry |
| `alembic` | Schema migrations, run programmatically at startup |

Dev-only: `pytest`, `pytest-asyncio` (async test support), `httpx` (FastAPI
test client transport).

## Development

```sh
cd daemon
uv sync
uv run pytest
uv run ompire-daemon
```

## Install as a systemd user service

See `contrib/ompire.service`. The unit assumes the repo is checked out at
`~/proj/ompire`; edit `WorkingDirectory` first if yours lives elsewhere.

```sh
mkdir -p ~/.config/systemd/user
cp contrib/ompire.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ompire
```

The unit runs `uv run ompire-daemon` from this directory, so `uv` must be on
`PATH` for the systemd user session (it inherits your login environment on
most distros; if not, set `Environment=PATH=...` in the unit).

Check status and logs with:

```sh
systemctl --user status ompire
journalctl --user -u ompire -f
```

The auth token lives at `<data_dir>/token` (default
`~/.local/share/ompire/token`) — `cat` it once to paste into the frontend.

## Configuration

Optional file at `~/.config/ompire/config.toml`. All keys are optional; the
daemon runs with documented defaults when the file or any key is absent.

```toml
port = 4173
bind = "127.0.0.1"
data_dir = "~/.local/share/ompire"
task_dir_root = "~/tasks"
checkout_root = "~/proj"
```

An unknown key or malformed TOML causes the daemon to exit non-zero with an
error naming the offending key.

## Security posture

The daemon binds to `127.0.0.1` only by default and requires a bearer token
(generated on first run at `<data_dir>/token`, mode 0600) on every REST and
WebSocket request. The WebSocket upgrade takes the token as a query
parameter, since the browser WebSocket API cannot set headers — this is
only acceptable because the daemon is not reachable off localhost by
default. If you ever expose this daemon beyond localhost (LAN, tunnel,
reverse proxy), the token-in-query-string approach becomes a real leak risk
(it can land in proxy/access logs) and needs revisiting — put it behind TLS
and prefer a header-based scheme at minimum.
