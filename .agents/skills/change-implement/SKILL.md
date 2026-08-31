---
name: change-implement
description: Implement a lightweight standalone or epic child change from SPEC.md and PLAN.md, keeping plans, documentation, ADRs, tests, and optional vision alignment coherent.
---

Implement an existing lightweight change completely. A standalone change is defined by:

```text
changes/<change>/SPEC.md
changes/<change>/PLAN.md
```

An epic child is defined by:

```text
epics/<epic>/changes/<change>/SPEC.md
epics/<epic>/changes/<change>/PLAN.md
```

No specification CLI is involved.

## Resolve the change

Accept an explicit directory path, an epic-qualified `<epic>/<change>` name, or a bare change name. For a bare name, inspect exact candidates under both `changes/` and `epics/*/changes/`:

- If exactly one candidate contains both artifacts, use it.
- If repository and conversation context identify one candidate unambiguously, use it.
- If multiple candidates remain, ask the user for an explicit path or qualified name.
- If either artifact is missing, do not invent implementation scope. Create or repair the proposal with `change-propose` first.

For an epic child, also require and read its parent `EPIC.md`. The exact entry must be `[~]`, its dependencies must be `[x]`, and no other entry may be active. If the child exists under a `[ ]` entry, reconcile it through `change-propose` before implementation. If parent and child state are otherwise inconsistent, preserve the artifacts and repair the state rather than guessing or selecting different work.

## Re-establish context

Before editing application code:

1. Read `SPEC.md` and `PLAN.md` completely.
2. Read repository instructions, the project's documentation entry point and the pages covering the affected area, and the relevant ADRs.
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

## Project documentation

The change is documented in the project's own documentation, in the place that documentation's own structure assigns to it. Do not create a change-specific or feature-specific side tree beside it.

Find the destination before writing:

1. Read the documentation entry point—`docs/index.md`, `docs/README.md`, the repository README, or a contributing guide—to learn how the set is organized.
2. Identify the framework and any audience split. Diátaxis categories (`tutorials/`, `how-to/`, `reference/`, `explanation/`) and per-audience roots (`docs/use/`, `docs/develop/`) are common; a project may use its own structure.
3. Read the pages that already cover the affected area. They are the default destination, and `SPEC.md`'s documentation impact should already name them.

Place each piece of information in the category that owns it. With Diátaxis:

| Information the change produces | Category |
|---|---|
| Precise current behavior: interfaces, options, states, errors, schemas | Reference |
| A goal the reader now accomplishes differently, or at all | How-to |
| A changed mental model, concept, or rationale that is not architectural | Explanation |
| A first-run path that no longer works as written | Tutorial |
| Why a durable architectural choice was made | ADR |

Respect an audience split: operator-facing behavior goes to the operator set, contributor-facing behavior to the contributor set. Behavior serving both is written once for its primary audience and linked from the other.

Update existing pages in place. Add a page only when the change introduces something the current structure has no home for; then place it in the correct category and link it from that category's index. Keep whatever conventions the set already uses for headings, cross-links, and terminology.

Cover the applicable purpose, normal user or operator flow, meaningful states and actions, failures and recovery, configuration, and externally meaningful interfaces—distributed across the categories above rather than concentrated in one page.

Describe current behavior, not the history of this change. Remove or revise superseded statements rather than appending contradictory deltas.

If the repository has no documentation set at all, create the smallest useful set under `docs/` for the affected area and say so in the report. Do not impose a full framework the project has not chosen.

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
- Documentation-only change: validate links, examples, and consistency against current behavior, including links from the owning category index.

Run broader applicable checks after focused behavior passes. Record concrete verification in the final response; do not fabricate evidence or treat checkbox state as proof.

## Completion boundary

This skill is complete when:

- every requirement in `SPEC.md` is implemented;
- every `PLAN.md` task is checked;
- required tests and real-surface verification pass;
- the project's documentation describes the new behavior in the correct place;
- required ADRs are accepted or superseded;
- optional vision alignment still holds when `VISION.md` exists.

Do not delete the change directory. Deletion and final reconciliation belong to `change-finish`. An epic child remains `[~]` after implementation because checked plan tasks do not prove the independent finish audit has passed. Report the qualified change when applicable, implemented behavior, documentation pages and ADRs changed by path, exact verification results, and that the parent marker remains active pending `change-finish`.