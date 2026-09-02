# Ompire

Ompire is a personal AI engineering workbench: a daemon on your own machine
that runs coding-agent work in four parts.

- **Agent isolation.** Each task gets a disposable workshop container and a
  full clone of the project. The agent never sees your signing key, your
  forge credentials, or even the model provider's credentials — model access
  arrives through a local auth gateway — and it cannot rewrite history in
  your main repository.
- **Parallel task oversight.** The founding motivation: running several
  unrelated tasks at once, across projects, without a wall of terminals.
  Tasks are organized by project with real state and context, not listed as
  sessions the way a terminal multiplexer or a tool like herdr does.
- **The workflow engine.** Workflows are declared deterministically —
  reproduce, diagnose, fix, validate, review, gate, publish — instead of
  being described to a single agent in markdown. Privileged steps like
  signing and pushing are performed by the daemon, never by the agent, and
  humans are asked for structured feedback at declared gates. This part is
  expected to evolve the most.
- **The refinement loop.** A retrospective over finished runs that proposes
  improvements — skills, AGENTS.md rules, documentation — where agents
  struggled. Direction today; it is what compounds over time.

The first three parts are built and running today; the refinement loop is
direction recorded in [`VISION.md`](docs/VISION.md). Agent isolation is the
part most likely to be extracted into a standalone project first.

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
