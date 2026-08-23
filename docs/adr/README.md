# Architecture decision records

ADRs capture durable architectural choices, their rationale, consequences, and
rejected alternatives. Current feature behavior and ordinary implementation
details belong in [feature documentation](../features/README.md) or in the code.

The full authoring procedure and rules are in [Write an architecture decision
record](../develop/how-to/write-an-adr.md).

`Accepted` means the implementation and current documentation agree.
`Proposed` means the decision is new, or that the implementation and the
vision still disagree — check the record's `Context` for the conflict before
relying on it.

## Index

| ADR | Decision | Status |
|---|---|---|
| [0001](0001-adopt-lightweight-skills-based-change-workflow.md) | Adopt the lightweight skills-based change workflow | Accepted |
| [0002](0002-run-as-local-daemon-with-stateless-web-ui.md) | Run Ompire as a local daemon with a stateless web UI | Accepted |
| [0003](0003-implement-trusted-control-plane-in-python.md) | Implement the trusted control plane in Python | Accepted |
| [0004](0004-use-rest-and-websocket-snapshot-deltas.md) | Use REST for commands and WebSocket snapshot-then-deltas for observation | Accepted |
| [0005](0005-persist-local-state-with-sqlite-core-and-alembic.md) | Persist local control-plane state in SQLite using SQLAlchemy Core and Alembic | Accepted |
| [0006](0006-give-every-task-a-separate-clone-and-workshop.md) | Give every task a separate clone and Workshop container | Proposed |
| [0007](0007-use-native-omp-rpc.md) | Integrate agents through supervised native Omp RPC processes | Accepted |
| [0008](0008-model-tasks-as-workflows-over-named-sessions.md) | Model tasks as workflows over named sessions | Accepted |
| [0009](0009-use-structured-git-excluded-outcomes.md) | Use structured, Git-excluded files for agent-step outcomes | Proposed |
| [0010](0010-separate-projects-templates-and-task-snapshots.md) | Separate projects, templates, and task snapshots | Proposed |
| [0011](0011-keep-review-and-publishing-authority-outside-agent-sandbox.md) | Keep review and publishing authority outside the agent sandbox | Accepted |
| [0012](0012-derive-attention-centrally-from-session-state.md) | Derive attention centrally from session state | Accepted |
| [0013](0013-layer-daemon-writable-settings-over-operator-configuration.md) | Layer daemon-writable settings over operator configuration | Accepted |
| [0014](0014-test-end-to-end-behavior-at-external-process-boundaries.md) | Test end-to-end behavior at external process boundaries | Accepted |
| [0015](0015-keep-agent-credentials-behind-narrow-brokers.md) | Keep agent credentials behind narrow brokers | Proposed |
| [0016](0016-persist-authority-bearing-task-history-and-provenance.md) | Persist authority-bearing task history and provenance | Proposed |
| [0017](0017-use-dedicated-bot-as-default-publishing-identity.md) | Use a dedicated bot as the default publishing identity | Proposed |
| [0018](0018-keep-built-in-workflows-in-python-until-portable-versioning-is-required.md) | Keep built-in workflows in Python until portable versioning is required | Accepted |
| [0019](0019-split-documentation-by-audience-using-diataxis.md) | Split documentation into operator and contributor sets organized by Diátaxis | Proposed |
| [0020](0020-author-documentation-as-portable-markdown.md) | Author documentation as portable Markdown and treat the site generator as a presentation layer | Proposed |

## Template

```markdown
# ADR NNNN: <Decision title>

- Status: Proposed
- Date: YYYY-MM-DD

## Context

## Decision

## Consequences

## Alternatives considered

### <Alternative>
```
