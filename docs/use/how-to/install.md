# Install Ompire

Ompire runs on Linux as a user-level daemon. It drives tools that already live
on your machine rather than bundling them, so most of the installation work is
making those prerequisites available.

## Prerequisites

Ompire spawns containers, invokes `gh`, and talks to your GPG agent. None of
this is bundled, and the snap uses classic confinement for the same reason.

| Requirement | Why | Check |
|---|---|---|
| Python 3.12 or newer | The daemon runtime | `python3 --version` |
| `gh`, authenticated | Pull-request creation | `gh auth status` |
| `lxc` and the workshop tooling | Per-task container isolation | `lxc list` |
| `gpg` with a signing key | Signed commits | `gpg --list-secret-keys` |
| `notify-send` | Desktop notifications (optional) | `which notify-send` |

Without `notify-send` the daemon logs a warning and keeps working; the badge
count in the UI still reflects what needs you.

## Install from source

```sh
git clone <repository-url> ompire
cd ompire/frontend && pnpm install && pnpm build
cd ../daemon && uv sync
```

The daemon serves the built frontend from `frontend/dist`, so the frontend
build is not optional.

## Install from the snap

```sh
snap install --classic ompire
```

Classic confinement is required: Ompire needs to reach your container tooling,
your GPG agent, and `gh`. The prerequisites above are not bundled in the snap
and must be present on the host.

## First run

```sh
uv run ompire-daemon
```

On first run the daemon generates a bearer token and writes it with owner-only
permissions to `$XDG_DATA_HOME/ompire/token` — by default
`~/.local/share/ompire/token`. Under the snap it goes to `$SNAP_USER_DATA`
instead.

The daemon listens on `http://127.0.0.1:4173`. Open it once with the token in
the query string; the frontend stores it in `localStorage` and subsequent
visits need no token:

```sh
xdg-open "http://127.0.0.1:4173/?token=$(cat ~/.local/share/ompire/token)"
```

If you open `http://127.0.0.1:4173` without a token and none is stored, the UI
cannot authenticate. Re-open with the query string.

### Run it as a service

The daemon runs in the foreground. To keep it running across logins, install
the provided systemd user unit:

```sh
mkdir -p ~/.config/systemd/user
cp daemon/contrib/ompire.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ompire
```

The unit's `WorkingDirectory` is `%h/proj/ompire/daemon`. Edit it if your
checkout lives elsewhere. It restarts on failure with a 2-second delay.

Under systemd the daemon may not inherit your D-Bus session, which disables
desktop notifications. If the log warns about it:

```sh
systemctl --user import-environment DBUS_SESSION_BUS_ADDRESS
```

## Configure

Configuration lives at `~/.config/ompire/config.toml` and is optional — every
key has a default. The file is read once at startup, and an unknown key or
malformed TOML makes the daemon refuse to start with the offending key named.
That is deliberate: a silently ignored key is worse than a failed start.

A minimal useful configuration:

```toml
gpg_signing_key = "YOUR_KEY_ID"
checkout_root = "~/proj"
task_dir_root = "~/tasks"
```

Every key is listed in [Configuration](../reference/configuration.md).

## Verify

1. The UI loads and the daemon chip shows a live connection.
2. The GPG chip is not `unknown`. See [Configure GPG
   signing](configure-gpg-signing.md).
3. `curl -H "Authorization: Bearer $(cat ~/.local/share/ompire/token)" \
   http://127.0.0.1:4173/api/daemon/info` returns the version, bind address,
   config path, and data directory.

## Next

[Register a project](register-a-project.md).
