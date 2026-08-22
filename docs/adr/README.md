# Architecture decision records

Architecture decision records (ADRs) capture durable architectural choices, their rationale, consequences, and rejected alternatives. Current feature behavior and ordinary implementation details belong in feature documentation or code.

Create ADRs in this directory using the next available zero-padded number and a kebab-case filename, for example `0007-use-native-omp-rpc.md`.

## ADR template

```markdown
# ADR NNNN: <Decision title>

- Status: Proposed
- Date: YYYY-MM-DD

## Context

<Describe the architectural problem, constraints, forces, and relevant
evidence in durable terms. Explain why a decision is needed, not merely what
the current feature does. Do not cite implementation paths or line numbers.>

## Decision

<State one durable choice directly. Define its scope and the invariant future
changes must preserve.>

## Consequences

<Describe positive consequences, accepted costs, operational constraints,
risks, and any explicit conditions that would cause this decision to be
revisited.>

## Alternatives considered

### <Alternative>

<Explain the material benefit and why it was rejected for this context.>
```

## Authoring rules

- Keep one architectural decision per ADR. Split independently reversible choices rather than joining them under a broad topic.
- Write at the level of a durable constraint or boundary. Current UI behavior, endpoint inventories, task ordering, and ordinary implementation details belong in feature documentation or code.
- Use `Status: Accepted` only when the implementation and current durable documentation agree on the choice. Use `Status: Proposed` when a decision is new or when sources conflict.
- Use the decision's original acceptance date when it is reliably recorded. Otherwise, use the ADR creation date and identify the record as a backfill in `Context`; never invent a historical date.
- Ground `Context` by inspecting the current implementation and relevant historical design material, but do not cite source paths or line ranges in the ADR. Those locations are supporting research, not durable links.
- For an accepted ADR, add a concise `ADR-NNNN` backlink in a comment or docstring at each stable implementation boundary that enforces the decision. Add a comment linking superseded design documents forward to the ADR. Do not annotate every caller or copy rationale into source comments.
- Treat evidence paths in planning or migration documents as research inputs only. Do not copy them into an ADR.
- State the decision in present tense and normative terms. Do not retell the implementation sequence.
- Record both benefits and costs under `Consequences`. Include compatibility, security, recovery, operational, and migration effects when applicable.
- Name only material alternatives that were genuinely considered or remain plausible. Do not manufacture token alternatives to fill the section.
- If the implementation, durable documentation, and `VISION.md` disagree, expose the conflict in `Context` and leave the ADR `Proposed`; do not silently choose one source.
- Never rewrite the substance of an accepted ADR. A later decision adds a new ADR and changes the earlier status to `Superseded by ADR-NNNN`.
- Omit extra sections unless they add durable information. Implementation instructions belong in a change `PLAN.md`; links flow from implementation and superseded design artifacts to accepted ADRs, not from ADRs to mutable source locations.
