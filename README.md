# Ompire

Ompire is a local-first control plane for coding-agent work. It runs as a
daemon on your own machine, gives every task an isolated clone and container,
drives it through a declared workflow, and keeps review and publishing
authority outside the agent's reach.

It is not a way to put an agent terminal in a browser. Ompire owns the
lifecycle around a session: preparing the workspace, executing the workflow,
carrying evidence between steps, asking you when a decision is needed,
reviewing the result, and performing privileged Git and forge operations
through trusted integrations.

The intended result is trustworthy leverage — delegating more work without
losing control of credentials, repositories, engineering standards, or
authorship.

## Status

Pre-1.0 and single-operator. The daemon binds to `127.0.0.1` and authenticates
with a bearer token. Interfaces change without deprecation cycles.

## Documentation

- **[Use Ompire](docs/use/index.md)** — install it, register a project, spawn
  tasks, review and ship.
- **[Build Ompire](docs/develop/index.md)** — architecture, development
  environment, testing, and the change workflow.
- **[VISION.md](docs/VISION.md)** — long-term direction.
- **[Architecture decision records](docs/adr/)** — why the system has its
  present shape.

## Quick start

Ompire needs Python 3.12+, `gh` authenticated against GitHub, `lxc` and the
workshop tooling, and `gpg` with a signing key.

```sh
cd frontend && pnpm install && pnpm build
cd ../daemon && uv sync && uv run ompire-daemon
```

Then open the UI once with the generated token:

```sh
xdg-open "http://127.0.0.1:4173/?token=$(cat ~/.local/share/ompire/token)"
```

Full instructions are in [Install Ompire](docs/use/how-to/install.md).

## Repository layout

```text
daemon/      FastAPI control plane (Python 3.12, asyncio, SQLite)
frontend/    React + TypeScript presentation layer
local-test/  end-to-end harness and executable fakes
docs/        documentation, feature reference, and decision records
snap/        classic-confinement snap packaging
```

## License

Licensed under the [GNU Affero General Public License version 3
only](LICENSE) (`AGPL-3.0-only`).
