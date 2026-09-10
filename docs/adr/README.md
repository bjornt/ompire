# Architecture decision records

ADRs capture durable architectural choices, their rationale, consequences, and
rejected alternatives. Current behavior and ordinary implementation details
belong in the reference section of the audience they serve —
[operator](../use/index.md#reference) or
[contributor](../develop/index.md#reference) — or in the code.

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
| [0009](0009-use-structured-git-excluded-outcomes.md) | Use structured, Git-excluded files for agent-step outcomes | Accepted |
| [0010](0010-separate-projects-templates-and-task-snapshots.md) | Separate projects, templates, and task snapshots | Superseded by ADR-0026 |
| [0011](0011-keep-review-and-publishing-authority-outside-agent-sandbox.md) | Keep review and publishing authority outside the agent sandbox | Accepted; live-workspace review and signing mechanics superseded by ADR-0032 |
| [0012](0012-derive-attention-centrally-from-session-state.md) | Derive attention centrally from session state | Accepted |
| [0013](0013-layer-daemon-writable-settings-over-operator-configuration.md) | Layer daemon-writable settings over operator configuration | Accepted |
| [0014](0014-test-end-to-end-behavior-at-external-process-boundaries.md) | Test end-to-end behavior at external process boundaries | Accepted |
| [0015](0015-keep-agent-credentials-behind-narrow-brokers.md) | Keep agent credentials behind narrow brokers | Accepted |
| [0016](0016-persist-authority-bearing-task-history-and-provenance.md) | Persist authority-bearing task history and provenance | Proposed |
| [0017](0017-use-dedicated-bot-as-default-publishing-identity.md) | Use a dedicated bot as the default publishing identity | Proposed |
| [0018](0018-keep-built-in-workflows-in-python-until-portable-versioning-is-required.md) | Keep built-in workflows in Python until portable versioning is required | Superseded by ADR-0028 |
| [0019](0019-split-documentation-by-audience-using-diataxis.md) | Split documentation into operator and contributor sets organized by Diátaxis | Proposed |
| [0020](0020-author-documentation-as-portable-markdown.md) | Author documentation as portable Markdown and treat the site generator as a presentation layer | Proposed |
| [0021](0021-admit-signing-key-selection-as-bounded-daemon-writable-setting.md) | Admit signing-key selection as a bounded daemon-writable setting | Accepted |
| [0022](0022-create-or-adopt-base-checkouts-without-mutating-them.md) | Create or adopt a project's base checkout without mutating operator repositories | Accepted |
| [0023](0023-admit-checkout-root-as-bounded-daemon-writable-setting.md) | Admit `checkout_root` as a bounded daemon-writable setting | Accepted |
| [0024](0024-keep-operator-state-outside-package-revisions.md) | Keep operator state outside package revisions | Accepted |
| [0025](0025-store-global-model-profiles-separately-from-launch-policy.md) | Store global model profiles separately from launch policy | Accepted |
| [0026](0026-resolve-launch-inputs-once-and-pin-them-to-the-task.md) | Resolve launch inputs once and pin them to the task | Accepted |
| [0027](0027-hand-off-model-policy-between-turns.md) | Hand off model policy between turns and record what applied | Accepted |
| [0028](0028-retain-declarative-workflow-revisions.md) | Retain declarative workflow revisions and pin them to tasks | Accepted |
| [0029](0029-declare-domain-outcomes-and-evidence-handoffs.md) | Declare domain outcomes and bind evidence to the attempt that used it | Accepted |
| [0030](0030-commit-human-decisions-before-advancing.md) | Commit a human decision before the run advances | Accepted |
| [0031](0031-let-operators-own-a-workflow-library-above-retained-revisions.md) | Let operators own a workflow library above retained revisions | Accepted |
| [0032](0032-bind-trusted-delivery-to-retained-candidates.md) | Bind trusted delivery to retained candidates and write-ahead action intent | Accepted |
| [0033](0033-scope-trusted-delivery-authority-to-the-workflow-run.md) | Scope trusted delivery authority to the workflow run | Accepted |
| [0034](0034-retain-durable-task-results-outside-the-workspace.md) | Retain durable task results outside the workspace | Accepted |
| [0035](0035-refuse-to-publish-handoff-destinations.md) | Refuse to publish handoff destinations | Accepted |
| [0036](0036-install-exported-result-files-without-replacing-them.md) | Install exported result files without replacing them | Accepted |
| [0037](0037-capture-workflow-results-before-accepted-handoffs.md) | Capture workflow results before accepted handoffs | Accepted |

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
