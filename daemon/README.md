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

The auth token lives at `<data_dir>/token`. The default is
`~/.local/share/ompire/token`; the snap user service uses
`~/snap/ompire/current/token`. `cat` it once to paste into the frontend.

## Building and serving the frontend

The daemon serves `frontend/dist/` at `/` when it exists (falling back to a
placeholder page otherwise; API routes always take precedence). Build it
from the `frontend/` project:

```sh
cd frontend
pnpm install
pnpm build
```

Then visit `http://127.0.0.1:<port>/?token=<the token from <data_dir>/token>`
once — the frontend stashes the token in `localStorage` and strips it from
the URL. For local development against `pnpm dev` instead (a separate origin
from the daemon), set `VITE_OMPIRE_TOKEN` in `frontend/.env.local`.

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

### Attention/notifications keys

```toml
stall_threshold = 300              # seconds of silence before a `working` session is marked `stalled`
renotify_interval = 300            # seconds between re-firing an unanswered notify/interrupt-tier notification
context_advisory_threshold = 80    # context percent that triggers the "context-high" advisory
stats_throttle_interval = 10       # minimum seconds between `stats` samples per task
notifications_enabled = true       # set false to disable desktop notifications (attention badges still work)
```

### Ship/PR keys

```toml
gpg_signing_key = "your@key.id"  # key id/email passed to `git commit -S`; when unset the daemon falls back to `git config user.signingkey`
gh_command = ["gh"]              # command invoked for `gh pr create` / `gh pr view` (argument list, no shell)
pr_poll_interval = 60            # seconds between PR-state polls of shipped, unresolved PRs (must be > 0)
```

`gpg_signing_key` is optional; when absent the daemon reads `git config user.signingkey` to discover the signing key. The GPG lock state is probed via `gpg-connect-agent KEYINFO --no-ask <keygrip>` and exposed as a shared condition used by both the chrome chip and the ship commit gate.

Once a task ships a PR, a background watcher polls `gh pr view <pr-url> --json state,mergedAt` every `pr_poll_interval` seconds until the PR reaches a terminal state (`merged`/`closed`), persisting `pr_state`/`pr_merged_at` on the task row and broadcasting `task_updated`. Post-merge cleanup is never automatic: the Ship Flow Cleanup step unlocks once the PR resolves and always asks for confirmation.

### Review keys

```toml
llmvet_command = ["llmvet"]      # command invoked to run llmvet (argument list, no shell)
review_port_range = [7180, 7280] # ephemeral-bind probe range for concurrent llmvet instances
```

`llmvet_command` is the argument list (not a shell string) the daemon runs in the task clone's working directory, appending `-no-open -host 127.0.0.1 -port <n>`. `review_port_range` bounds the ephemeral socket bind used to pick a free localhost port for each review.

### Crash-recovery keys

```toml
shutdown_grace = 10          # seconds a live agent gets to exit cleanly on SIGTERM before SIGKILL
recovery_concurrency = 4     # max agents resumed concurrently on startup after a restart
```

On shutdown the daemon terminates every live agent concurrently and waits up
to `shutdown_grace` seconds total (not per agent) before force-killing
stragglers — well under systemd's default 90s `TimeoutStopSec` for the
default value. If you raise `shutdown_grace` past that, raise the unit's
`TimeoutStopSec` (in `contrib/ompire.service`) to match, or systemd will
SIGKILL the daemon itself before its own graceful shutdown finishes.

## Desktop notifications

The daemon fires desktop notifications itself (via the host's `notify-send`,
not the browser) when a task needs the operator — SPEC Decision 4's `notify`
and `interrupt` attention tiers. This requires:

- **`notify-send`** (from `libnotify-bin`/`libnotify`) on `PATH`, with
  `--action` support (used for the notification's single **Open** button).
- **A reachable D-Bus session bus.** Under a normal desktop login this is
  automatic. Under a `systemctl --user` service (see above), the user
  session's D-Bus address is not always inherited — if notifications never
  appear, run once (in the graphical session):

  ```sh
  systemctl --user import-environment DBUS_SESSION_BUS_ADDRESS
  ```

  and restart the `ompire` unit. Some distros need the same treatment for
  `DISPLAY`/`WAYLAND_DISPLAY` if `xdg-open` (used for the notification's Open
  action) also fails to launch a browser tab.

Missing `notify-send`, an unreachable bus, or `notifications_enabled = false`
all degrade gracefully: the daemon logs one warning at startup and keeps
running with attention badges (the "N need you" pill, tab-title/favicon
badge, and task-card tier styling) intact — no desktop popups, nothing else
affected.

### Stock GNOME: notifications appear but the Open action doesn't work

At startup the daemon queries the notification server's actual capabilities
(`gdbus ... GetCapabilities`) rather than trusting the `notify-send` binary
alone. On stock GNOME (confirmed on Ubuntu with the default GNOME Shell),
that query comes back **without** `actions` — GNOME's notification daemon
doesn't advertise interactive actions for the legacy `org.freedesktop.Notifications`
interface `notify-send` uses; that capability is effectively reserved for
apps going through the XDG Desktop Portal instead (why a browser's own
notifications, or Firefox-as-a-snap's, can have clickable buttons while a
plain `notify-send --action ...` call can't). If you see
`notify-send: Actions are not supported by this notifications server.` when
testing manually, this is what's happening.

The daemon detects this automatically and falls back to a plain,
non-interactive notification — you'll still see the popup, but clicking it
does nothing; use the "N need you" pill / favicon badge to jump to the task
instead. There's no config workaround for this today; supporting the XDG
Desktop Portal notification API instead of `notify-send` would fix it, but
that's a larger change (a new D-Bus dependency) deferred unless this
degradation proves too costly in practice.

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
