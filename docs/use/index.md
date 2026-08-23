# Use Ompire

This documentation is for operators — people running Ompire to get work done.
If you are changing Ompire itself, read [Build Ompire](../develop/index.md)
instead.

Ompire runs as a daemon on your machine and serves a web UI at
`http://127.0.0.1:4173`. You register a project, spawn a task against it, and
Ompire prepares an isolated clone and container, runs a coding agent inside a
declared workflow, and tells you when it needs you. When the work is done you
review it and ship it as a signed commit and pull request.

Nothing leaves your machine except the Git and forge operations you approve,
and the agent never holds your credentials.

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

- [Configuration](reference/configuration.md) — every `config.toml` key
- [States](reference/states.md) — task, session, attention, GPG, and PR states
- [HTTP and WebSocket API](reference/api.md)

Current behavior, feature by feature:

- [Projects](../features/projects.md) · [Templates](../features/templates.md)
- [Tasks](../features/tasks.md) · [Task spawn](../features/task-spawn.md) ·
  [Task detail](../features/task-detail.md) · [Web UI](../features/web-ui.md)
- [Session states](../features/session-states.md) ·
  [Agent interaction](../features/agent-interaction.md) ·
  [Session advisories](../features/session-advisories.md)
- [Attention and notifications](../features/attention-notifications.md)
- [Workflow engine](../features/workflow-engine.md) ·
  [The bugfix workflow](../features/bugfix-workflow.md)
- [Review](../features/review.md) · [Ship flow](../features/ship-flow.md) ·
  [GPG signing](../features/gpg-signing.md) ·
  [Merge polling](../features/merge-poll.md)
- [Daemon settings](../features/daemon-settings.md)

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
