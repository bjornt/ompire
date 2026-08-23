# Write an architecture decision record

An ADR explains why a durable architectural choice exists — its rationale,
consequences, and the alternatives rejected. It does not describe current
behavior or ordinary implementation detail.

## When to write one

Write an ADR for a decision a future maintainer could reasonably reverse
without knowing the original constraints.

Do not write one for current UI behavior, endpoint inventories, task ordering,
or mechanical implementation choices. Those belong in [feature
documentation](write-a-feature-doc.md) or in the code.

The placement test:

| Information | Destination |
|---|---|
| Long-term product principle | `VISION.md` |
| Desired behavior for active work | `changes/<name>/SPEC.md` |
| Implementation order and affected code | `changes/<name>/PLAN.md` |
| Current user or operator behavior | Feature documentation |
| Reason for a durable architecture choice | ADR |
| Historical implementation discussion | Git commit or pull request |

## Create the file

Use the next available zero-padded number and a kebab-case filename in
[`docs/adr/`](../../adr/):

```text
docs/adr/0021-short-decision-title.md
```

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

## Authoring rules

**Scope**

- One architectural decision per ADR. Split independently reversible choices
  rather than joining them under a broad topic.
- Write at the level of a durable constraint or boundary.
- Omit extra sections unless they add durable information.

**Status**

- `Accepted` only when the implementation and current durable documentation
  agree on the choice.
- `Proposed` when a decision is new, or when sources conflict.
- If the implementation, durable documentation, and `VISION.md` disagree,
  expose the conflict in `Context` and leave it `Proposed`. Do not silently
  pick one source.

**Dates**

- Use the original acceptance date when it is reliably recorded.
- Otherwise use the creation date and identify the record as a backfill in
  `Context`. Never invent a historical date.

**Context**

- Describe the problem, constraints, forces, and evidence in durable terms.
  Explain why a decision is needed, not what the feature does.
- Ground it by inspecting the implementation and relevant historical design
  material, but do not cite source paths or line ranges. Those are research
  inputs, not durable links.

**Decision**

- State one choice directly, in present tense and normative terms.
- Define its scope and the invariant future changes must preserve.
- Do not retell the implementation sequence.

**Consequences**

- Record both benefits and costs. Include compatibility, security, recovery,
  operational, and migration effects where they apply.
- State the conditions that would cause the decision to be revisited.

**Alternatives**

- Name only alternatives genuinely considered or still plausible.
- Explain the material benefit of each and why it was rejected *for this
  context*.
- Do not manufacture token alternatives to fill the section.

## Link it from the code

For an accepted ADR, add a short `ADR-NNNN` backlink in a comment or docstring
at each stable implementation boundary that enforces the decision:

```python
# Local single-user boundary: ADR-0002
# (docs/adr/0002-run-as-local-daemon-with-stateless-web-ui.md)
DEFAULT_BIND = "127.0.0.1"
```

Do not annotate every caller, and do not copy rationale into source comments.
Links flow from implementation to the ADR, not the other way around — an ADR
that cites mutable source locations rots.

Add a forward link from any superseded design document to the ADR.

## Superseding

Never rewrite the substance of an accepted ADR. A later decision adds a new
ADR and changes the earlier one's status to `Superseded by ADR-NNNN`.

The record of what was decided, and why it was later reversed, is the point.
