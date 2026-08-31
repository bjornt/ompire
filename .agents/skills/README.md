# Change Compass

**Keep vision, change, implementation, and durable project knowledge aligned.**

Change Compass is a Markdown-first workflow for deliberate project change shared by humans and coding agents. It uses ordinary repository files and six agent skills. There is no specification CLI, generated metadata, sync operation, archive, or permanent completed-change database.

The workflow is rigorous about where knowledge belongs:

- `docs/VISION.md`, when maintained, owns stable product direction and boundaries;
- active epic and change files own temporary intent, scope, approach, and status;
- the project's own documentation owns current behavior for its real audiences;
- ADRs own durable architectural reasoning; and
- Git commits and pull requests own history and transient implementation discussion.

Finishing reconciles every durable result into its owning artifact before deleting temporary coordination files. Checked boxes are status, not proof.

## Model and terminology

```text
optional epic ──▶ change ──▶ implementation task
 aggregate       deliverable   plan step
 outcome          delta
```

- An **epic** is a temporary coordination boundary for one coherent outcome that needs multiple independently deliverable changes.
- A **change** is one independently specifiable and deliverable delta. It may be a feature, bug fix, refactor, migration, infrastructure, operations, or documentation change.
- A **task** is one implementation step inside a change's `PLAN.md`.
- A **roadmap** is longer-horizon planning across possible epics. It remains outside Change Compass.

Changes are not called stories because the unit is broader than user-facing Agile stories.

## Files

A standalone change uses the shortest path:

```text
changes/<change>/
  SPEC.md                         temporary desired experience and requirements
  PLAN.md                         temporary approach, risks, and task list
```

A multi-change outcome uses an optional epic:

```text
epics/<epic>/
  EPIC.md                         temporary outcome, boundaries, order, and status
  changes/<change>/               created only when this child is selected
    SPEC.md
    PLAN.md
```

An epic proposal initially creates only `epics/<epic>/EPIC.md`. Its `changes/` subtree appears with the first active child; Git cannot preserve an empty directory. Planned children are not pre-generated.

Durable files remain project-owned:

```text
docs/VISION.md                    optional long-term direction
docs/**                           current project documentation in its own structure
docs/adr/NNNN-*.md                default ADR location when none is established
```

There is no `no-epic` or `standalone` container. Small work remains under `changes/`. Completed changes and epics are deleted after reconciliation; Git retains their history.

## Skills

The repository's `.agents/skills` link points to `skills/`, so Oh My Pi discovers every lifecycle skill from the same source files.

### `change-propose`

Creates or refines `SPEC.md` and `PLAN.md` without implementation. It supports standalone names, explicit paths, and epic-qualified names.

```text
/skill:change-propose <description-or-change>
/skill:change-propose <epic>/<change>
```

A new unqualified change defaults to `changes/<change>/`. A qualified child requires a matching epic entry, finished dependencies, and no other active child.

### `change-implement`

Implements every unchecked task, verifies the changed behavior, updates project documentation, and creates or supersedes ADRs when durable decisions emerge.

```text
/skill:change-implement <change>
/skill:change-implement <epic>/<change>
```

It leaves the completed change directory in place for the independent finish audit. An epic child remains `[~]` until that audit passes.

### `change-finish`

Independently audits delivered behavior against `SPEC.md` and `PLAN.md`, reconciles documentation and ADRs, performs final verification, and removes the change directory.

```text
/skill:change-finish <change>
/skill:change-finish <epic>/<change>
```

For an epic child, successful finishing removes the child and then marks its exact parent entry `[x]`. It does not delete the parent epic.

### `epic-propose`

Creates or refines one bounded `EPIC.md` without pre-creating children or implementing application behavior.

```text
/skill:epic-propose <description-or-epic>
```

Use it only when one coherent outcome genuinely needs multiple changes. Otherwise use `change-propose` directly.

### `epic-propose-next`

Returns or refines the active child proposal, or selects the first planned dependency-ready entry and creates its `SPEC.md` and `PLAN.md` through the normal proposal rules.

```text
/skill:epic-propose-next <epic>
```

It stops for review and never implements implicitly. To skip the pause, explicitly ask the agent to run `epic-propose-next` and `change-implement` sequentially; the same proposal and implementation gates still apply.

### `epic-finish`

Audits the aggregate outcome after every child is independently finished, reconciles any remaining durable knowledge, performs the epic completion scenario, and removes the epic.

```text
/skill:epic-finish <epic>
```

It refuses to replace child finishing or archive an incomplete epic.

## Standalone lifecycle

```text
idea
  │
  ▼
changes/<change>/SPEC.md ──▶ PLAN.md ──▶ implementation ──▶ finish audit
                                                                    │
                              reconcile docs, ADRs, and vision ◀─────┘
                                                                    │
                                                           delete change
```

1. Propose:

   ```text
   /skill:change-propose improve-session-recovery
   ```

2. Review `changes/improve-session-recovery/SPEC.md` and `PLAN.md`.
3. Implement:

   ```text
   /skill:change-implement improve-session-recovery
   ```

4. Finish independently:

   ```text
   /skill:change-finish improve-session-recovery
   ```

Finishing removes `changes/improve-session-recovery/` only after delivered behavior, verification, project documentation, ADRs, and optional vision alignment pass.

## Epic lifecycle

An epic keeps aggregate coordination readable while each child retains the complete change lifecycle:

```text
EPIC.md
   │ propose next
   ▼
child SPEC.md + PLAN.md ──▶ review ──▶ implement ──▶ finish child ──▶ [x]
   ▲                                                                    │
   └──────────────────────────── next child ────────────────────────────┘
                                                                        │
                                            aggregate audit + delete epic
```

Example `epics/improve-account-recovery/EPIC.md`:

```markdown
# Epic: Improve account recovery

## Outcome

People can recover access safely without an administrator editing account state.

## Vision alignment

Recovery remains understandable, self-service, and explicit about security boundaries.

## Boundaries

This epic does not replace the authentication provider or add account delegation.

## Changes

### [ ] 1. add-recovery-codes
Add one-time recovery codes with safe generation, storage, and use.
- Depends on: None
- Acceptance: A person can recover access once with an unused code and cannot reuse it.
- Verification: Focused authentication tests and a real recovery smoke scenario.

### [ ] 2. add-recovery-guidance
Document and surface the complete recovery path and failure states.
- Depends on: add-recovery-codes
- Acceptance: A locked-out person can identify and complete the supported recovery path.
- Verification: Documentation link validation and browser exercise of success and failure flows.

## Completion

The full recovery journey works from lockout through restored access, project documentation describes current behavior and security limits, and applicable architectural decisions are recorded.
```

Omit `## Vision alignment` when the project has no `docs/VISION.md`.

Operate it as follows:

1. Create the epic:

   ```text
   /skill:epic-propose improve-account-recovery
   ```

2. Propose only the next change:

   ```text
   /skill:epic-propose-next improve-account-recovery
   ```

   This creates:

   ```text
   epics/improve-account-recovery/changes/add-recovery-codes/
     SPEC.md
     PLAN.md
   ```

   and changes only that entry to `[~]`.

3. Review the proposal, then implement and finish it explicitly:

   ```text
   /skill:change-implement improve-account-recovery/add-recovery-codes
   /skill:change-finish improve-account-recovery/add-recovery-codes
   ```

   Successful finishing removes the child directory and marks the entry `[x]`.

4. Repeat `epic-propose-next`, `change-implement`, and `change-finish` for `add-recovery-guidance`.
5. Audit and remove the completed epic:

   ```text
   /skill:epic-finish improve-account-recovery
   ```

The final repository keeps current documentation, accepted ADRs, implementation, and Git history—not completed epic or change files.

## `EPIC.md` reference

Required shape:

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

`## Vision alignment` is conditional on a maintained `docs/VISION.md`. Extra prose and phase headings inside `## Changes` are allowed when useful. Change names are unique kebab-case identifiers. Dependencies use exact names and may point only to earlier entries in the same epic.

Each entry is a seed, not a substitute for its future `SPEC.md` and `PLAN.md`. It should establish why the child exists, its independently observable delivery boundary, dependencies, acceptance, and verification without guessing detailed implementation too early.

`## Completion` describes aggregate proof beyond “every entry is checked”: the composed behavior, end-to-end verification, documentation reconciliation, and durable decisions that must hold across the epic.

## Epic status and selection invariants

| Marker | Meaning | Child directory |
|---|---|---|
| `[ ]` | Planned, not activated | Must not exist |
| `[~]` | Active from proposal through child finish | Must contain `SPEC.md` and `PLAN.md` |
| `[x]` | Independently finished and reconciled | Must not exist |

Additional invariants:

- At most one entry is `[~]`.
- A dependency is finished only when its exact entry is `[x]`.
- Planned child directories are never created in advance.
- `epic-propose-next` returns or refines an active proposal before considering another entry.
- With no active child, it chooses the first `[ ]` entry in document order whose dependencies are `[x]`.
- It never implements the selected proposal.
- `change-finish` changes `[~]` to `[x]` only after removing the audited child.
- `epic-finish` requires every entry `[x]` and no child artifacts.

### Inconsistent state

Plain files cannot make child removal and parent updates atomic. Manual edits can also create mismatches. Every epic skill therefore reconciles before advancing.

Safe recoveries include:

- `[ ]` plus one complete matching child: preserve it, restore `[~]`, and refine the same proposal;
- incomplete matching child: preserve it and repair `SPEC.md` or `PLAN.md` through `change-propose`;
- `[~]` plus complete matching child: continue that child;
- child removed after a successful finish but marker still `[~]`: use finish evidence and Git to complete the parent update.

Ambiguous cases—multiple active entries, multiple surviving children, unknown directories, `[x]` with surviving artifacts, or `[~]` without evidence of a completed finish—must not trigger new selection or deletion. Inspect child artifacts, delivered behavior, durable documentation, ADRs, and Git. Repair only what the evidence uniquely supports; otherwise report the exact recovery needed.

A bare change name is resolved across both `changes/<name>/` and `epics/*/changes/<name>/`. When more than one candidate exists, use an explicit path or `<epic>/<change>` qualification.

## `SPEC.md` shape

With `docs/VISION.md`:

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

Without a maintained vision, omit `## Vision alignment` entirely.

The spec owns observable experience and scope. Implementation files, internal symbols, persistence details, and task ordering belong in the plan unless they are public interfaces. Documentation impact names real destination pages and their categories.

## `PLAN.md` shape

```markdown
# Plan

## Approach

## Affected areas

## Architecture decisions

## Risks

## Tasks

- [ ] <concrete, verifiable task>
```

The plan is a checked hypothesis. Every spec requirement maps to one or more tasks, and every task maps to stated scope. Applicable implementation, caller migration, obsolete-path removal, documentation, ADR work, behavioral tests, real-surface verification, and final vision alignment are tasks rather than afterthoughts.

Implementation discoveries update the artifact that owns the affected truth:

| Discovery | Action |
|---|---|
| Implementation approach changes, behavior does not | Update `PLAN.md` |
| Desired observable behavior or scope changes | Update `SPEC.md` first |
| Durable architectural decision emerges | Add or supersede an ADR |
| Product behavior changes | Update the project's documentation |
| Transient investigation detail | Leave it in working context or Git history |

A spec is never weakened after implementation to excuse missing behavior.

## Project documentation

Change Compass does not define a product-documentation tree. It follows the structure and audience boundaries the project already uses.

Each lifecycle skill:

1. reads the documentation entry point;
2. identifies the project's categories and audiences;
3. updates pages that already own the affected behavior; and
4. adds a page only when no existing page can own the new information.

With Diátaxis, place information by purpose:

| Information | Destination |
|---|---|
| Precise current behavior, interfaces, states, errors, schemas | Reference |
| A goal the reader accomplishes | How-to |
| A changed mental model or non-architectural rationale | Explanation |
| A first-run path | Tutorial |
| Why a durable architectural choice exists | ADR |

Current behavior is synthesized into these pages rather than copying active specs. Superseded claims are removed. If no documentation set exists, create the smallest useful set under `docs/` for the affected area rather than imposing a framework.

## Optional `VISION.md`

`VISION.md` is optional. When absent, all skills skip vision work; they do not create a placeholder or treat absence as a defect.

When present, it is the strategic alignment point for epic proposal, change proposal, implementation, child finishing, and aggregate epic finishing. A conflict has two valid resolutions:

1. reshape the work to deliver the desired outcome within the vision; or
2. make an explicit strategic decision to update the vision.

The vision is never weakened merely to make work appear aligned.

## Architecture decision records

ADRs preserve durable architectural reasoning, not implementation chronology or ordinary design detail. Use the repository's established ADR location, or `docs/adr/` by default. Accepted ADRs are not rewritten in substance; a reversal creates a new ADR and marks the old one superseded.

Default shape:

```markdown
# ADR 0007: <Decision>

- Status: Accepted
- Date: YYYY-MM-DD

## Context

## Decision

## Consequences

## Alternatives considered
```

## How this differs from OpenSpec

| Concern | OpenSpec-style workflow | Change Compass |
|---|---|---|
| Change intent | Proposal plus delta specs | `SPEC.md` |
| Technical design and tasks | Separate design and task artifacts | `PLAN.md` with embedded tasks |
| Multi-change outcome | Tool-managed change collection | Optional readable `EPIC.md` |
| Current behavior | Cumulative capability specs | The project's own documentation |
| Architecture history | Often embedded in change design | ADRs |
| Long-term direction | External configured context | Optional `VISION.md` |
| Status | CLI and artifact schema | Markdown status and plan checkboxes |
| Completion | Validate, sync, archive | Reconcile durable knowledge, then delete |
| Historical record | Archived change tree | Git and ADRs |

Change Compass trades machine-validated delta semantics for readability and fewer authoritative places. Its rigor comes from repository research, explicit scope, requirement-to-task coverage, vision alignment, documentation ownership, ADRs, independent finishing audits, and real behavioral verification.

## Non-goals

Change Compass intentionally provides no:

- dedicated CLI, service, or artifact database;
- YAML metadata, generated index, or schema validator;
- required epic wrapper for standalone changes;
- archived or completed change/epic tree;
- roadmap, sprint, estimation, ownership, or project-management system;
- automatic multi-change scheduling or parallel-agent orchestration;
- cumulative capability-specification tree;
- separate general-purpose review artifact or skill;
- automatic `VISION.md` creation.
