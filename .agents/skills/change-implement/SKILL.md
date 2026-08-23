---
name: change-implement
description: Implement a lightweight change from changes/<name>/SPEC.md and PLAN.md, keeping the plan, current feature documentation, ADRs, tests, and optional vision alignment coherent. Use when the user wants to start or continue an existing planned change.
---

Implement an existing lightweight change completely. The change is defined by:

```text
changes/<name>/SPEC.md
changes/<name>/PLAN.md
```

No specification CLI is involved.

## Resolve the change

Use the name supplied by the user. Otherwise inspect `changes/`:

- If exactly one change directory contains both files, use it.
- If repository and conversation context identify one unambiguously, use it.
- If multiple remain plausible, ask the user to select one.
- If either artifact is missing, do not invent implementation scope. Create or repair the proposal with the `change-propose` workflow first.

## Re-establish context

Before editing application code:

1. Read `SPEC.md` and `PLAN.md` completely.
2. Read repository instructions and the relevant current feature documentation and ADRs.
3. If `docs/VISION.md` exists, read it and independently compare the proposed outcome and approach against it. If it does not exist, skip vision alignment entirely; do not create a vision file or vision tasks.
4. Inspect the affected code, interfaces, callers, tests, and existing conventions. Plans are hypotheses until checked against the repository.
5. Reconcile stale or incorrect plan details before implementation. Preserve the agreed outcome and scope.
6. Mirror the unchecked `PLAN.md` tasks in the session task tracker when one is available. The Markdown checkboxes remain the durable status.

## Alignment gate

Only apply this gate when `VISION.md` exists.

If the approach conflicts with the vision but another implementation can deliver the same specified experience, revise `PLAN.md` to use the aligned approach. If resolving the conflict changes the user experience, project boundary, or long-term direction, stop before that conflicting work and ask the user to choose. Never edit `VISION.md` simply to make implementation pass the gate; a vision update requires an explicit strategic decision and must be reflected in `SPEC.md`.

## Implement the plan

For each unchecked task:

1. Re-read the relevant spec requirements and plan section.
2. Research and reuse the repository's existing patterns. Do not introduce a parallel convention.
3. Implement the smallest complete change that satisfies the requirement.
4. Update all affected callers and remove obsolete paths; do not leave compatibility shims unless the spec explicitly requires them.
5. Verify the task's observable contract with the appropriate focused check.
6. Mark its `PLAN.md` checkbox complete only after the implementation and focused verification are complete.
7. Keep the session task tracker synchronized.

Do not stop merely because a phase or task boundary was reached. Continue until every actionable task is complete.

## Keep artifacts honest

Implementation may reveal incorrect assumptions:

- If the implementation approach changes without changing observable behavior, update `PLAN.md` before or with the code.
- If desired observable behavior or scope must change, update `SPEC.md` first. Ask the user when the change represents a materially different product choice.
- If a durable architectural decision emerges, add an ADR task to `PLAN.md`, then create or supersede the ADR as part of the implementation.
- Never change the spec after the fact merely to describe an incomplete implementation. The implementation must satisfy the agreed spec.

## Current feature documentation

Feature documentation describes the product after the change and is useful to both users and agents. Update the repository's existing documentation location; if none exists, use `docs/features/`.

Document applicable:

- purpose and when the feature is used;
- normal user or operator flow;
- meaningful states and available actions;
- failures, unavailable states, and recovery;
- configuration and externally meaningful interfaces.

Describe current behavior, not the history of this change. Remove or revise superseded statements rather than appending contradictory deltas.

## ADRs

Use the repository's established ADR location; otherwise use `docs/adr/` with zero-padded sequential names such as `0007-short-title.md`.

Create an ADR only for a durable decision future maintainers could reasonably reverse without knowing the original constraints. An accepted ADR contains:

```markdown
# ADR NNNN: <Decision>

- Status: Accepted
- Date: YYYY-MM-DD

## Context

## Decision

## Consequences

## Alternatives considered
```

Do not rewrite the substance of an accepted ADR. Add a new ADR and mark the old one `Superseded by ADR-NNNN`.

## Verification

Verification must match the changed surface:

- Bug fix: reproduce the failure, then confirm it no longer occurs.
- Web UI: exercise the actual UI in a browser.
- CLI or TUI: run the program and exercise the interaction.
- Feature or API: run focused changed-contract tests and an applicable smoke scenario.
- Documentation-only change: validate links, examples, and consistency against current behavior.

Run broader applicable checks after focused behavior passes. Record concrete verification in the final response; do not fabricate evidence or treat checkbox state as proof.

## Completion boundary

This skill is complete when:

- every requirement in `SPEC.md` is implemented;
- every `PLAN.md` task is checked;
- required tests and real-surface verification pass;
- current feature documentation is updated;
- required ADRs are accepted or superseded;
- optional vision alignment still holds when `VISION.md` exists.

Do not delete the change directory. Deletion and final reconciliation belong to `change-finish`. Report implemented behavior, documentation and ADR changes, and exact verification results.