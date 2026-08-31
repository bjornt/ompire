---
name: change-propose
description: Define a lightweight standalone or epic child change by creating SPEC.md and PLAN.md before implementation, without OpenSpec or another specification CLI.
---

Create a complete, implementation-ready change proposal using ordinary Markdown. Do not implement application code while using this skill.

## Resolve the destination

The user may provide a kebab-case change name, a description, an explicit change-directory path, or an epic-qualified `<epic>/<change>` name. If only a description is supplied, derive a concise kebab-case change name.

A standalone change lives at:

```text
changes/<change>/
  SPEC.md
  PLAN.md
```

An epic child lives at:

```text
epics/<epic>/changes/<change>/
  SPEC.md
  PLAN.md
```

Use the epic path only when the user supplies it, uses a qualified name, or conversation and repository context identify the parent epic unambiguously. Otherwise default new work to the standalone path; never invent a synthetic epic.

For a bare existing name, consider exact matches under both roots. Use it only when one candidate exists. If the same name exists in multiple locations or multiple candidates remain plausible, ask the user to select an explicit path or `<epic>/<change>` name.

For an epic child, require `epics/<epic>/EPIC.md` and an exact change entry. Read the epic outcome, boundaries, seed, dependencies, completion conditions, and any completed or active siblings that affect current facts. Refuse to create the child when its dependencies are unfinished, another child is active, or the entry is already `[x]`. Do not add unapproved entries to the epic from this workflow; refine the epic first.

If the destination exists, read it and continue refining it; never overwrite prior decisions blindly. Preserve unexpected files and resolve incomplete artifact pairs through this proposal workflow.

## Research first

1. Read repository instructions and relevant existing documentation.
2. If `docs/VISION.md` exists, read it. If it does not exist, skip all vision-alignment work: do not create it, require it, or add a placeholder vision section.
3. Read the project's documentation and learn how it is organized. Start at its entry point—`docs/index.md`, `docs/README.md`, the repository README, or a contributing guide—and identify the framework and any audience split it uses. Diátaxis categories (`tutorials/`, `how-to/`, `reference/`, `explanation/`) and per-audience roots (`docs/use/`, `docs/develop/`) are common; a project may use its own structure. Then read the existing pages that cover the affected area.
4. Read relevant ADRs. Prefer the established ADR location; otherwise use `docs/adr/`.
5. Inspect the affected implementation and its tests enough to make the plan concrete and to reuse existing patterns.
6. Resolve product ambiguity from available context first. Ask the user only about decisions with materially different product outcomes or tradeoffs.

## Create `SPEC.md`

The spec describes the desired user or operator experience, not the implementation.

When `VISION.md` exists, use:

```markdown
# <Change title>

## Outcome

## Vision alignment

## User experience

## Requirements

## Scope

## Non-goals

## Documentation impact
```

When `VISION.md` does not exist, omit `## Vision alignment` entirely.

Content rules:

- Name the user or operator outcome and why it matters.
- Describe the normal flow plus observable failure, unavailable, empty, and recovery behavior where relevant.
- State requirements as observable behavior and important invariants. Use examples when they remove ambiguity; do not mechanically turn every requirement into formal WHEN/THEN scenarios.
- Keep implementation files, symbols, database columns, and task ordering out of the spec unless they are themselves a public interface.
- Link to the existing documentation pages instead of restating the whole existing feature.
- Make scope and non-goals explicit. Do not add adjacent improvements that the user did not request.
- Name, by path, the documentation pages that will change, and the category each belongs to. Prefer updating existing pages; justify every new page. `None` is valid only with a concrete reason.

### Vision handling

Only perform this section when `VISION.md` exists.

Classify the change in substance, without requiring classification labels in the file:

- aligned: advances the vision;
- neutral or enabling: preserves it while enabling maintenance or future work;
- in tension: conflicts with a principle, boundary, or desired experience;
- vision-changing: intentionally changes long-term direction.

For a tension, first find an implementation or UX shape that satisfies both the requested outcome and the vision. If that is impossible, surface the exact conflict and ask the user to choose between changing the proposal and changing the vision. Never weaken or rewrite `VISION.md` merely to make a proposal appear aligned. Edit it only after an explicit strategic decision from the user, and record the decision in the spec.

## Documentation destination

The change must land in the project's own documentation, in the place that documentation's own structure assigns to it. Never route product documentation into a change-specific or feature-specific side tree.

Decide the destination while writing the spec, and record it under `## Documentation impact`:

- Prefer the pages that already cover the affected area. They are the default destination.
- Respect the framework the project uses. With Diátaxis, precise current behavior—interfaces, options, states, errors, schemas—belongs to reference; a goal the reader now accomplishes differently belongs to how-to; a changed mental model or rationale belongs to explanation; a first-run path that no longer works as written belongs to a tutorial.
- Respect any audience split. Operator-facing behavior goes to the operator set, contributor-facing behavior to the contributor set. Behavior that serves both is written once for its primary audience and linked from the other.
- Propose a new page only when the change introduces something the current structure has no home for. Place it in the correct category and link it from that category's index.
- Reasons for durable architectural choices go to an ADR, not to the documentation set.

If the repository has no documentation set at all, plan the smallest useful set of pages under `docs/` for the affected area and say so. Do not impose a full framework the project has not chosen.

## Create `PLAN.md`

Create the plan after `SPEC.md` is coherent. Use:

```markdown
# Plan

## Approach

## Affected areas

## Architecture decisions

## Risks

## Tasks

- [ ] <concrete, verifiable task>
```

Content rules:

- Explain the smallest coherent implementation approach and why it fits existing architecture.
- Name affected components, interfaces, persistence, operations, tests, and documentation pages where applicable.
- Identify durable architectural decisions that require a new ADR or supersede an existing ADR. Routine implementation choices do not require ADRs. If no ADR is expected, say why.
- Include only material risks and pair each with mitigation or verification.
- Embed tasks directly in the plan. Tasks must be ordered, implementation-sized, and independently checkable.
- Map every spec requirement to one or more tasks, and every task back to stated scope.
- Include applicable behavioral verification, real-surface smoke testing, documentation updates in the project's own documentation set, and ADR work in the tasks.
- If `VISION.md` exists, include a final task to re-check the completed behavior against it. If it does not exist, include no vision task.
- Do not add archive, sync, generated metadata, or CLI-validation tasks.

## Self-review

Before finishing:

1. Read both files as a user and as an implementer.
2. Confirm the spec contains the complete desired experience without implementation leakage.
3. Confirm every requirement is covered by the plan.
4. Confirm the plan introduces no unstated product scope.
5. Confirm documentation and ADR effects are explicit, and that every named documentation page is a real path in the project's documentation set.
6. If `VISION.md` exists, independently re-check alignment; if absent, confirm neither artifact invented vision work.
7. Confirm there are no placeholders, deferred design decisions needed to start, or contradictory statements.

For an epic child, only after both artifacts pass self-review, ensure the exact parent entry is `[~]`. Change `[ ]` to `[~]`; preserve `[~]`; never change `[x]` here. Do not update another entry or implement the plan.

Report the change name, qualified name when applicable, artifact paths, parent marker state, major decisions, and any explicit user decision still blocking implementation. Otherwise state that the change is ready for `change-implement`.