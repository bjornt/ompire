---
name: epic-propose-next
description: Select or refine the next eligible change in an epic, create its SPEC.md and PLAN.md through the normal proposal workflow, and stop for review before implementation.
---

Propose exactly one child change from an existing epic. This skill selects work and creates or refines its proposal; it never implements the change.

## Resolve and read the epic

Resolve `epics/<epic>/EPIC.md` from the user's explicit path, epic name, or unambiguous repository context. If multiple epics remain plausible, ask the user to choose. Require `EPIC.md`; use `epic-propose` rather than inventing a missing epic.

Read completely:

- `EPIC.md`;
- repository instructions;
- `docs/VISION.md` when present;
- the project documentation entry point and relevant pages;
- relevant ADRs;
- all child directories under `epics/<epic>/changes/`; and
- the full `SPEC.md` and `PLAN.md` of an active or otherwise surviving child.

Also read and follow the current `change-propose` skill. Its research, specification, planning, documentation-impact, ADR, vision, and self-review gates apply unchanged to the selected child. The epic entry is seed context, not permission to produce a weaker proposal.

## Reconcile state before selection

Parse status from change headings under `## Changes` and dependencies from their entries. Require unique kebab-case names and only these markers:

- `[ ]`: planned and no child directory exists;
- `[~]`: active and its child directory contains both `SPEC.md` and `PLAN.md`;
- `[x]`: finished and no child directory remains.

Dependencies must name earlier entries in the same epic. At most one entry may be active.

Before choosing work, compare `EPIC.md` with the child tree:

- One `[~]` entry with complete child artifacts: this is the selected change. Refine it through `change-propose` and stop; never activate another entry.
- A `[ ]` entry with a complete matching child: preserve the child, change the marker to `[~]`, refine it, and stop.
- A child matching an entry but missing `SPEC.md` or `PLAN.md`: preserve it and repair the proposal through `change-propose`; set or retain `[~]` only after both artifacts are coherent.
- An unknown child directory, more than one active marker, more than one surviving child, an `[x]` entry with a child, or `[~]` without a child: do not select new work until reconciled.

For inconsistent state, inspect surviving artifacts, delivered repository behavior, durable documentation and ADRs, and Git history where useful. Repair only the state uniquely supported by evidence. Never delete unexpected work, mark a change finished from checkboxes alone, or skip an ambiguous active child. If evidence cannot distinguish accidental deletion from an incomplete finish, report the exact mismatch and the required recovery rather than guessing.

## Select the next proposal

If there is no active child after reconciliation:

1. Read entries in document order.
2. Treat a dependency as finished only when its exact entry is `[x]`.
3. Select the first `[ ]` entry whose dependencies are all finished.
4. If no planned entry is eligible, report each unfinished dependency or explicit external blocker. Do not activate a later dependent change silently.
5. If every entry is `[x]`, report that the epic is ready for `epic-finish`.

Materialize only the selected child at:

```text
epics/<epic>/changes/<change>/
  SPEC.md
  PLAN.md
```

Apply `change-propose` using:

- the complete selected epic entry;
- the epic outcome, vision alignment, boundaries, and completion conditions;
- completed sibling outcomes that affect current repository facts; and
- fresh repository research.

The child spec owns its independently deliverable observable delta. The child plan owns its implementation approach and tasks. Do not copy the whole epic into either file or treat sibling seeds as child scope.

After both child artifacts pass proposal self-review, verify that `change-propose` changed exactly that epic heading from `[ ]` to `[~]`. Do not perform a second competing status update. If artifact creation cannot be completed, preserve the evidence, leave or restore `[ ]`, and report the failure. Re-running this skill with the active child refines the same proposal; it never advances twice.

## Review boundary

Stop after the proposal and parent status are coherent. Do not edit application code, run the change implementation plan, or mark plan tasks complete.

Implementation is a separate invocation:

```text
/skill:change-implement <epic>/<change>
```

If the user explicitly asks to propose and implement in one engagement, complete this workflow first and then apply `change-implement` sequentially. The combined request does not weaken the proposal review or implementation gates.

## Output

Report:

- the epic and selected change;
- whether the proposal was created, repaired, or refined;
- the exact `SPEC.md` and `PLAN.md` paths;
- dependency state and the parent marker update;
- major proposal decisions; and
- confirmation that implementation has not started.
