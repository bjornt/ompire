# Decision log

Every durable architectural choice in Ompire is recorded as an ADR in
[`docs/adr/`](../../adr/README.md). This page is a reading guide — the records
themselves are authoritative.

## Read these first

Four decisions explain most of the system's shape:

**[ADR-0002](../../adr/0002-run-as-local-daemon-with-stateless-web-ui.md)** —
a local daemon owns everything; the UI is presentation only. Nearly every
other decision assumes this.

**[ADR-0006](../../adr/0006-give-every-task-a-separate-clone-and-workshop.md)** —
each task gets its own clone and container. This is what makes parallel tasks
safe and why worktrees were rejected.

**[ADR-0008](../../adr/0008-model-tasks-as-workflows-over-named-sessions.md)** —
the task, not the session, is the unit of work. This inversion is what
separates Ompire from a session manager.

**[ADR-0001](../../adr/0001-adopt-lightweight-skills-based-change-workflow.md)** —
how changes are delivered, and why the repository prefers ordinary Markdown to
tool-enforced structure. It also explains the shape of this documentation.

## By area

| Area | ADRs |
|---|---|
| Process and topology | 0002, 0003 |
| Client protocol | 0004 |
| State and durability | 0005, 0016 |
| Isolation and credentials | 0006, 0015, 0022 |
| Agent integration | 0007 |
| Work model | 0008, 0009, 0010, 0018, 0028 |
| Review and publishing | 0011, 0017, 0032 |
| Attention | 0012 |
| Settings | 0013, 0021, 0023 |
| Testing | 0014 |
| Process and documentation | 0001, 0019, 0020 |

## How to read one

The `Alternatives considered` section is usually the most valuable part. It
records what was rejected and *why in this context* — which is exactly what a
future maintainer needs to know before reversing the choice, and exactly what
gets lost otherwise.

`Consequences` records costs as well as benefits, and names the conditions
that would justify revisiting the decision. A decision whose stated
supersession trigger has fired is a decision worth reopening.

## Status

`Accepted` means the implementation and current documentation agree.
`Proposed` means the decision is new, or that sources conflict — check
`Context` for the conflict before relying on it.

An accepted ADR is never rewritten. A later decision adds a new record and
marks the earlier one `Superseded by ADR-NNNN`.

## Unreconciled decisions

Three areas remain unreconciled because the implementation and the vision
disagree:

- **[Agent credential delivery](../../adr/0015-keep-agent-credentials-behind-narrow-brokers.md)** —
  ADR-0015 proposes replacing raw environment injection with narrow brokers.
- **[The durability boundary](../../adr/0016-persist-authority-bearing-task-history-and-provenance.md)** —
  ADR-0016 proposes durable authority-bearing history and provenance for safe
  recovery and explanation. Review history and delivery authorization, intent
  and outcomes are now inside that boundary
  ([ADR-0032](../../adr/0032-bind-trusted-delivery-to-retained-candidates.md));
  full commit lineage and transcript retention are not, so it stays `Proposed`.
- **[Publishing identity](../../adr/0017-use-dedicated-bot-as-default-publishing-identity.md)** —
  ADR-0017 proposes a dedicated bot as the default while current shipping
  inherits host identity.
These remain `Proposed` until their implementation conflicts are resolved.
Changes that touch one should move the ADR forward deliberately rather than
resolving the gap incidentally — a `Proposed` record is a decision waiting for
an implementation, not a suggestion.

Two were resolved this way rather than incidentally.
[ADR-0018](../../adr/0018-keep-built-in-workflows-in-python-until-portable-versioning-is-required.md)
took Python workflow definitions for the built-in-only phase and named, in
advance, exactly what would overturn it; when two of those triggers arrived,
[ADR-0028](../../adr/0028-retain-declarative-workflow-revisions.md) superseded
it and did what ADR-0018 had required of its successor.
[ADR-0009](../../adr/0009-use-structured-git-excluded-outcomes.md) stayed
`Proposed` for one named reason — a hidden LLM judge it could not accept — and
became `Accepted` only when ADR-0028 removed that judge.

[ADR-0011](../../adr/0011-keep-review-and-publishing-authority-outside-agent-sandbox.md)
shows the other shape a supersession can take. Its trusted boundary — review and
publishing on the host, control-plane key selection, credentials outside the
sandbox — is intact and carried forward unchanged. What
[ADR-0032](../../adr/0032-bind-trusted-delivery-to-retained-candidates.md)
replaced is narrower: the mechanics of reviewing and signing a task's *live*
workspace, which made an approval a status rather than a statement about
content. A supersession can correct a mechanism without disturbing the principle
it was serving.

ADR-0018 is the one to copy when a decision is knowingly provisional: it
commits to the current choice while naming, in advance, what would overturn
it, and it made its own succession a short argument rather than an
archaeology.

## Adding a record

See [Write an architecture decision record](../how-to/write-an-adr.md).
