# Daemon core

## Overview

The daemon is a single long-lived process that owns everything consequential:
process supervision, state, credentials, review, and publishing. Everything
else in Ompire is either inside it or presentation over it.

## States and behavior

### Running as a service

The daemon binds to a localhost address by default and is suitable for a
systemd user service. A unit ships at `daemon/contrib/ompire.service` — type
`simple`, restart on failure with a 2-second delay, working directory
`%h/proj/ompire/daemon`.

Started as a service, the daemon survives terminal and browser exits. A
connection attempt from a non-loopback address is refused under default
configuration.

### Configuration

`~/.config/ompire/config.toml` is read at startup. With no file present, the
daemon runs entirely on documented defaults.

A malformed file, an unknown key, or a wrong value type makes the daemon exit
non-zero with an error naming the offending key. Failing fast is the point: a
silently ignored key produces a daemon that runs with settings the operator
believes are in effect.

See [Configuration](../../use/reference/configuration.md).

### Registry and migrations

One SQLite database under the data directory, with ordered schema migrations
applied automatically at startup and the applied version tracked.

A fresh start creates the database and applies every migration. An existing
database at an earlier version receives only the missing ones, preserving
rows.

### Authentication

A random token is generated on first run and stored with owner-only (0600)
permissions at `<data_dir>/token`.

Every REST request must present `Authorization: Bearer <token>`; a missing or
wrong token returns `401`. A WebSocket upgrade without the valid token is
refused.

Rotating the token closes every open WebSocket with code `1008`.

### Serving the frontend

The daemon serves the built frontend from `frontend/dist/` at the site root
when present, and a placeholder status page otherwise.

Registered API routes take precedence over static paths. Beyond that, the
rules are:

| Request | Result |
|---|---|
| A file that exists under `dist/` | That file, normal static behavior |
| `GET` that resolves to no built file, path not `/api` or `/api/…` | `index.html`, HTTP 200, HTML content type |
| `GET /api/missing` | FastAPI's JSON `404` — never the SPA |
| `GET /apiary` | `index.html` — outside the `/api` path-segment namespace |
| Non-`GET` on an unmatched non-API path | No SPA fallback |
| Any deep link with no frontend build present | `404` |

The fallback exists so client-side routes like `/tasks/42` survive a page
reload or a pasted link. Excluding `/api` from it keeps an unknown API path an
honest `404` rather than a 200 of HTML that a client would fail to parse.

## Configuration

| Key | Default | Effect |
|---|---|---|
| `bind` | `127.0.0.1` | The single-user security boundary |
| `port` | `4173` | |
| `data_dir` | `$SNAP_USER_DATA`, else `$XDG_DATA_HOME/ompire` | Database, token, audit log |

## Interfaces

| Method | Path | Returns |
|---|---|---|
| `GET` | `/api/daemon/info` | Version, bind, port, config path, data dir, audit log path when present |
| `GET` | `/api/settings/token` | The current bearer token |
| `POST` | `/api/settings/token/rotate` | A new token; closes all WebSockets |

FastAPI's generated OpenAPI documentation is served at `/docs`, with the
schema at `/openapi.json`.
