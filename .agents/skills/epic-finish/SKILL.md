---
name: epic-finish
description: Audit a completed epic against EPIC.md, delivered behavior, project documentation, ADRs, verification, and optional vision alignment, then remove the temporary epic directory.
---

Finish a completed multi-change outcome. This is an aggregate reconciliation and deletion gate, not a shortcut for finishing child changes.

## Resolve and read the epic

Resolve `epics/<epic>/EPIC.md` from the user's explicit path, epic name, or unambiguous repository context. If multiple epics remain plausible, ask the user to choose. Require and read `EPIC.md` completely.

Also read:

- repository instructions;
- `docs/VISION.md` when present;
- the project documentation entry point and all pages affected across the epic;
- relevant ADRs, including decisions created or superseded by child changes;
- affected implementation and tests; and
- any remaining contents under `epics/<epic>/changes/`.

## Child completion gate

Require every change entry to be `[x]` and require no child change directory or artifact to remain.

If an entry is `[ ]`, the epic is not implemented; report the next eligible proposal and do not finish it.

If an entry is `[~]` or its child directory remains, use the normal `change-finish <epic>/<change>` workflow first. Do not perform the child audit from aggregate status alone and do not delete its artifacts.

If markers and directories disagree, apply the conservative recovery rules documented by the epic workflow. Preserve unexpected files. Do not convert an entry to `[x]` merely to make the epic finishable.

## Aggregate completion audit

Do not treat finished child markers as proof of the epic outcome. Audit the delivered system independently.

### Outcome and boundaries

- Every aggregate outcome and completion condition in `EPIC.md` is delivered.
- The child results compose into one coherent user, operator, or maintainer experience.
- Work has not silently narrowed the aggregate outcome or crossed an explicit boundary.
- Dependencies were resolved in the delivered behavior, not only in document order.
- No obsolete integration path, temporary bridge, placeholder, or follow-up remains between children.

When a reachable gap remains, finish it through the child change that owns it or propose a new child entry if it is genuinely required by the agreed epic outcome. Never weaken `EPIC.md` after the fact to excuse incomplete delivery.

### Vision

Only when `docs/VISION.md` exists, compare the aggregate delivered behavior—not just individual child specs—with the product promise, boundaries, principles, and long-term direction. Resolve implementation-level tension when possible. A product or strategy conflict requires an explicit user decision; never rewrite the vision merely to make the epic pass.

### Durable project knowledge

Confirm the project's own documentation stands alone without the epic or child files:

- current behavior, flows, states, failures, recovery, configuration, and public interfaces are documented in the pages and categories that own them;
- superseded statements and change-oriented wording are removed;
- cross-links and examples describe the composed final behavior; and
- no epic-specific documentation side tree has become a permanent substitute for project documentation.

Confirm every durable architectural decision has an accepted ADR and any superseded ADR points to its replacement. ADRs explain decisions, not the epic chronology. Git remains the implementation history.

Fix stale or missing durable documentation and ADR relationships before finishing when the agreed epic outcome determines the correction.

## Final verification

Run focused checks for the integrations between child changes and the aggregate real-surface scenario stated under `## Completion`. Run broader checks required by repository instructions.

Prior child reports and `[x]` markers are context, not current evidence. Fix failures caused by the epic. Do not suppress diagnostics or weaken coverage.

## Remove the epic

Only after every gate passes:

1. Remove `epics/<epic>/EPIC.md`.
2. Remove the empty `epics/<epic>/changes/` directory if present.
3. Remove the now-empty `epics/<epic>/` directory.
4. Do not move the epic to `archive/`, `completed/`, a roadmap, or another historical tree.
5. Leave other active epics and standalone changes untouched.

Invoking this skill authorizes deletion of this temporary epic directory after the gates pass. Unexpected files are not authorized for deletion; inspect them and preserve their durable information in its proper destination first.

## Output

Report:

- the epic finished;
- the aggregate behavior and boundaries audited;
- the documentation pages and ADRs reconciled, by path;
- exact verification commands or scenarios and their results; and
- confirmation that `epics/<epic>/` was removed.

Do not report success while any child or aggregate completion gate remains unmet.
