# Feature documentation

This directory describes the product as it currently works, one file per
feature. It is the single authoritative place for current behavior — reference
pages in the documentation sets link here rather than restating it.

Feature documents are created and reconciled as part of delivering a change,
not as follow-up work. See [Write a feature
document](../develop/how-to/write-a-feature-doc.md) for the shape and rules.

## Operator-facing

What an operator sees and does.

| Feature | Covers |
|---|---|
| [Projects](projects.md) | Registered repositories, fork routing, guarded rename and removal |
| [Templates](templates.md) | Spawn configuration: branch, workflow, model, preamble |
| [Tasks](tasks.md) | The task registry, lifecycle, cleanup, and the Tasks view |
| [Task spawn](task-spawn.md) | The four-step pipeline and the Spawn view |
| [Task detail](task-detail.md) | Transcript, composer, session tabs, workflow strip, gates |
| [Web UI](web-ui.md) | Chrome, sections, sorting, attention chip, theming |
| [Session states](session-states.md) | The nine statuses and every transition rule |
| [Agent interaction](agent-interaction.md) | Steer, follow-up, interrupt, questions, approvals |
| [Session advisories](session-advisories.md) | Stats, context-high, maybe-waiting |
| [Attention and notifications](attention-notifications.md) | Tiers, desktop notifications, re-notify aging |
| [Workflow engine](workflow-engine.md) | Steps, outcomes, gates, the LLM judge, recovery |
| [The bugfix workflow](bugfix-workflow.md) | The worked example workflow |
| [Review](review.md) | Host-side review, the reset dance, comment loopback |
| [Ship flow](ship-flow.md) | Draft, signed commit, push, pull request |
| [GPG signing](gpg-signing.md) | Key probing and the commit gate |
| [Merge polling](merge-poll.md) | Pull-request tracking and deferred cleanup |
| [Daemon settings](daemon-settings.md) | Layered runtime settings |

## Contributor-facing

Internals with no operator-facing surface of their own.

| Feature | Covers |
|---|---|
| [Daemon core](daemon-core.md) | Service, config, registry, auth, static serving |
| [Daemon API](daemon-api.md) | REST/WebSocket split, snapshot contents, event types |
| [Agent integration](agent-rpc.md) | Supervised child processes, NDJSON, opaque passthrough |
| [Workshop lifecycle](workshop-lifecycle.md) | Launch, on-demand status, removal |
| [Crash recovery](crash-recovery.md) | Session resume, reconciliation, graceful shutdown |
| [Local testing harness](local-testing.md) | Executable fakes, runbooks, fidelity recordings |

## Provenance

These documents were migrated from the retiring OpenSpec capability
specifications. Requirement language and per-requirement scenarios were
dropped; behavior was verified against the implementation where the two could
disagree.

Where a specification and the code disagreed, the code won and the document
says what the code does.
