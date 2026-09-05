# Use Ompire

This documentation is for operators — people running Ompire to get work done.
If you are changing Ompire itself, read [Build Ompire](../develop/index.md)
instead.

Ompire runs as a daemon on your machine and serves a web UI at
`http://127.0.0.1:4173`. You register a project, spawn a task against it, and
Ompire prepares an isolated clone and container, runs a coding agent inside a
declared workflow, and tells you when it needs you. When the work is done you
review it and ship it as a signed commit and pull request.

That is the daily loop of a workbench built from four parts — agent
isolation, parallel task oversight, the workflow engine, and the refinement
loop described in [`VISION.md`](../VISION.md). See
[What Ompire is](explanation/what-ompire-is.md) for the map.

## Start here

[Install Ompire and run your first task](tutorials/install-and-first-task.md)
takes you from nothing to a task running in its own container.

## Tutorials

Learning-oriented. Follow them in order the first time.

- [Install Ompire and run your first task](tutorials/install-and-first-task.md)

## How-to guides

Task-oriented. Each one assumes you already have Ompire running.

- [Install Ompire](how-to/install.md)
- [Configure GPG signing](how-to/configure-gpg-signing.md)
- [Register a project](how-to/register-a-project.md)
- [Spawn a task](how-to/spawn-a-task.md)
- [Review and ship a task](how-to/review-and-ship.md)
- [Troubleshoot the daemon](how-to/troubleshoot.md)

## Reference

Precise detail, looked up rather than read through.

Cross-cutting:

- [Configuration](reference/configuration.md) — every `config.toml` key
- [States](reference/states.md) — task, session, attention, GPG, and PR states
- [HTTP and WebSocket API](reference/api.md)

Projects and tasks:

- [Projects](reference/projects.md) · [Templates](reference/templates.md) ·
  [Model profiles](reference/model-profiles.md)
- [Tasks](reference/tasks.md) · [Task spawn](reference/task-spawn.md) ·
  [Task detail](reference/task-detail.md) · [Web UI](reference/web-ui.md)

Sessions and attention:

- [Session states](reference/session-states.md) ·
  [Agent interaction](reference/agent-interaction.md) ·
  [Session advisories](reference/session-advisories.md)
- [Attention and notifications](reference/attention-notifications.md)

Workflows:

- [Workflow engine](reference/workflow-engine.md) ·
  [The bugfix workflow](reference/bugfix-workflow.md)

Review and publishing:

- [Review](reference/review.md) · [Ship flow](reference/ship-flow.md) ·
  [GPG signing](reference/gpg-signing.md) ·
  [Merge polling](reference/merge-poll.md)

Settings:

- [Daemon settings](reference/daemon-settings.md)

## Explanation

Background. Read when you want to know why Ompire behaves as it does.

- [What Ompire is](explanation/what-ompire-is.md)
- [The trust boundary](explanation/trust-boundary.md)
- [The attention model](explanation/attention.md)

## Before you rely on it

Ompire is pre-1.0 and built for a single operator. The daemon binds to
localhost, there is one bearer token, and there is no multi-user model.
Interfaces change without deprecation cycles. Run it against repositories you
control.
