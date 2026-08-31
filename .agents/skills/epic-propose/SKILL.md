---
name: epic-propose
description: Define or refine a lightweight epic in epics/<name>/EPIC.md for one coherent outcome that needs several independently deliverable changes. Use when the user wants to shape multi-change work without creating child changes yet.
---

Create a complete, readable epic proposal using ordinary Markdown. Do not create child change directories or implement application code while using this skill.

## Resolve the epic

The user may provide a kebab-case epic name, a description, or both. If only a description is supplied, derive a concise kebab-case name. The epic lives at:

```text
epics/<epic>/EPIC.md
```

At proposal time, create only `EPIC.md`. Do not create an empty `changes/` directory; Git cannot preserve it, and planned children are intentionally materialized only when selected.

If the epic already exists, read it and any active child completely before refining it. Preserve delivered entries, active work, prior decisions, and useful detail. Never overwrite them blindly. If multiple existing epics could match and repository context cannot disambiguate them, ask the user which one to use.

## Decide whether an epic is warranted

Use an epic only when one coherent outcome requires multiple independently specifiable and deliverable changes whose order or dependencies benefit from shared coordination.

Keep a standalone change under `changes/<change>/` when one `SPEC.md` and `PLAN.md` can describe and deliver the outcome coherently. Never create a synthetic `no-epic`, `standalone`, or miscellaneous epic.

Use this terminology consistently:

- **epic**: the temporary coordination boundary for the aggregate outcome;
- **change**: one independently specifiable and deliverable delta;
- **task**: one implementation step in a change's `PLAN.md`; and
- **roadmap**: longer-horizon planning across possible epics, outside this workflow.

Do not rename changes to stories. Changes also include refactors, migrations, infrastructure, operations, and documentation work.

## Research first

1. Read repository instructions and the project documentation entry point.
2. Read `docs/VISION.md` if it exists. If it does not exist, do not create it or invent vision work.
3. Read relevant project documentation and ADRs.
4. Inspect affected implementation and tests enough to divide the outcome along real architectural and delivery boundaries.
5. Inspect active standalone changes and epics for overlap or conflicting ownership.
6. Resolve product ambiguity from repository context first. Ask only about materially different product outcomes or tradeoffs.

## Write `EPIC.md`

Use this required shape:

```markdown
# Epic: <title>

## Outcome

## Vision alignment

## Boundaries

## Changes

### [ ] 1. <change-name>
<Why this independently deliverable change is needed and what it must achieve.>
- Depends on: <earlier change names, or "None">
- Acceptance: <observable completion condition>
- Verification: <appropriate behavioral evidence>

## Completion
```

Omit `## Vision alignment` when the project has no `docs/VISION.md`. Extra explanatory prose and phase headings inside `## Changes` are allowed when they improve navigation. Keep the required headings and status-bearing change headings recognizable without a parser.

### Outcome and boundaries

- State the aggregate user, operator, or maintainer outcome and why it matters.
- Explain alignment with the maintained vision without copying it wholesale.
- Bound the epic to one coherent outcome. Exclude adjacent backlog candidates.
- Name behavior or responsibilities that deliberately remain unchanged.
- Keep implementation sequencing out of these sections except where it is part of the observable outcome.

### Change entries

Each entry is a seed for a future `SPEC.md` and `PLAN.md`, not a compressed implementation plan.

- Use a unique kebab-case change name.
- Size the entry so it can be proposed, implemented, verified, and finished independently.
- Describe the observable result and the reason it forms a coherent delivery boundary.
- Name dependencies by exact earlier change name. Use `None` when independent.
- Dependencies may refer only to earlier entries in the same epic. Do not infer cross-epic dependencies.
- Include a concrete acceptance condition and verification surface.
- Include enough repository evidence to guide later proposal research, but do not pre-create detailed specs or plans.
- Start every new entry at `[ ]`. Preserve `[~]` and `[x]` entries in an existing epic unless evidence requires conservative status recovery.

Use only these lifecycle markers:

- `[ ]`: planned; no child directory exists;
- `[~]`: active; `epics/<epic>/changes/<change>/` contains `SPEC.md` and `PLAN.md`;
- `[x]`: finished and reconciled; no child directory remains.

At most one entry may be `[~]`. Do not reorder or rewrite an active entry in a way that invalidates its child artifacts. Do not remove a finished entry while the epic remains active; it is needed to resolve dependencies and audit aggregate completion.

### Completion

State observable aggregate conditions that prove the epic outcome, not merely “all changes are done.” Include the applicable end-to-end verification and durable documentation or architectural reconciliation expected across the epic.

## Self-review

Before finishing:

1. Confirm an epic is more useful than one standalone change.
2. Confirm all entries contribute to the one stated outcome and no backlog miscellany is included.
3. Confirm each entry is independently deliverable and has acceptance and verification evidence.
4. Confirm every dependency names an earlier entry, has a real delivery reason, and forms no cycle.
5. Confirm the first entry can be proposed without unavailable future detail.
6. Confirm the aggregate completion conditions go beyond checked statuses.
7. Confirm vision alignment when a vision exists, and confirm no vision section was invented when absent.
8. Confirm no child directory, generated metadata, tracker schema, or implementation was created.

Report the epic name, `EPIC.md` path, ordered changes, dependencies, major boundaries, and any explicit user decision still blocking a complete epic. Otherwise state that it is ready for `epic-propose-next`.
