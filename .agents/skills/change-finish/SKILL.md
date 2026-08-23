---
name: change-finish
description: Finish a completed lightweight change by auditing the implementation against SPEC.md, PLAN.md, current feature documentation, ADRs, tests, and optional VISION.md, then removing the temporary change directory. Use when the user wants to reconcile and close a completed change.
---

Finish an implemented lightweight change. This replaces specification validation, syncing, and archiving with one reconciliation step followed by deletion of temporary planning files.

## Resolve and read the change

Resolve `changes/<name>/` from the user's name or unambiguous repository context. Require both `SPEC.md` and `PLAN.md`. Read them completely before evaluating completion.

Also read:

- repository instructions;
- relevant current feature documentation;
- relevant ADRs;
- the affected implementation and tests;
- `docs/VISION.md` if it exists.

If `VISION.md` does not exist, skip all vision-alignment work. Do not create it, mention its absence as a defect, or add a placeholder alignment section.

## Completion audit

Do not trust checked boxes by themselves. Audit the delivered behavior independently.

### Specification

- Every outcome, user flow, requirement, failure behavior, and invariant in `SPEC.md` is present.
- The implementation has not silently narrowed the scope or substituted easier behavior.
- Any implemented user-visible behavior not described by the spec is either necessary supporting behavior or is removed.
- `SPEC.md` and `PLAN.md` reflect any decisions made during implementation.

If implementation does not satisfy the spec, return to implementation and finish the reachable work. Never rewrite the spec merely to excuse missing behavior.

### Plan

- Every task is checked and its result exists.
- Every affected caller, test, interface, migration, and obsolete path is handled.
- Verification tasks contain real evidence from the appropriate surface.
- No placeholder, scaffold, compatibility alias, dead branch, or follow-up task remains.

### Vision

Only perform this section when `VISION.md` exists.

Compare the delivered behavior—not only the wording of `SPEC.md`—against the vision's product promise, boundaries, principles, and long-term direction. If an aligned implementation can resolve a conflict without changing the specified experience, implement it. If the conflict requires changing product behavior or the vision, surface the exact conflict and obtain the user's strategic decision. Never weaken `VISION.md` to make finishing easier.

### Current feature documentation

Confirm the documentation describes the product as it now exists and is useful without reading the change files. It should cover the relevant user or operator flow, states, failures and recovery, configuration, and public interfaces. Remove superseded claims and change-oriented wording such as “this change adds.”

If documentation is missing or stale, update it before finishing. Do not copy `SPEC.md` wholesale; synthesize current behavior into the appropriate feature document.

### ADRs

Confirm every durable architectural decision is represented by an accepted ADR and that superseded decisions point to their replacements. Do not create ADRs for routine implementation details. Ensure current documentation describes what the system does while ADRs explain why the architecture does it that way.

## Final verification

Run the focused changed-contract checks and the applicable real-surface smoke scenario. Run broader repository checks required by project instructions. A prior implementation report is useful context but not a substitute for the final check when the repository may have changed.

Fix failures caused by the change. Do not suppress diagnostics, loosen assertions, or delete meaningful coverage to obtain a pass.

## Remove the change

Only after every audit and verification item passes:

1. Remove `changes/<name>/SPEC.md` and `changes/<name>/PLAN.md`.
2. Remove the now-empty `changes/<name>/` directory.
3. Do not move it to `archive/`, `completed/`, or another historical directory.
4. Do not copy its contents into feature docs or ADRs verbatim. Durable knowledge must already be synthesized there.
5. Leave other active change directories untouched.

The user's request to run this finishing workflow authorizes deletion of this temporary change directory once the completion gates pass. If the directory contains unexpected files, inspect them and preserve any durable information in its proper destination before removal; never discard unrelated user work.

Git and the associated commit or pull request provide change history. The default branch should retain only current product documentation, accepted architectural history, implementation, and any still-active changes.

## Output

Report:

- the change that was finished;
- the delivered behavior audited;
- feature documentation and ADRs reconciled;
- exact verification commands or scenarios and their results;
- confirmation that `changes/<name>/` was removed.

Do not report success while any completion gate remains unmet.