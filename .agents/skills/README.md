# Lightweight change workflow

This repository can define and deliver changes with ordinary Markdown and three agent skills. It needs no specification CLI, generated metadata, sync operation, archive, or permanent completed-change directory.

The model separates temporary coordination from durable project knowledge:

```text
                         durable project knowledge
                    ┌───────────────────────────────┐
                    │ docs/VISION.md     optional   │
                    │ the project's documentation   │
                    │ architecture decision records │
                    └───────────────▲───────────────┘
                                    │ reconcile
                                    │
idea ──▶ changes/<name>/SPEC.md ──▶ PLAN.md ──▶ implementation ──▶ delete change
```

## Files

```text
docs/VISION.md                    optional long-term direction
changes/<name>/SPEC.md            temporary desired experience and requirements
changes/<name>/PLAN.md            temporary implementation approach and task list
docs/**                           the project's own documentation, in its own structure
docs/adr/NNNN-*.md                default location for architectural decisions
```

Durable product knowledge goes into the documentation the project already maintains, in the place that documentation's own structure assigns to it. The workflow defines no documentation location of its own and creates no side tree of change- or feature-specific pages. `docs/adr/` is a default only when the repository has not already established an ADR location.

`changes/<name>/` contains exactly the active planning artifacts needed for the workflow. Once the change is complete and its durable knowledge is reconciled, the directory is deleted. Git and the associated commit or pull request retain history.

## Skills

Skills live one level below `.omp/skills/`, so Oh My Pi discovers them automatically in a new session.

### `change-propose`

Creates or refines:

```text
changes/<name>/SPEC.md
changes/<name>/PLAN.md
```

It reads the project's documentation, ADRs, relevant code, and `VISION.md` when present. It reviews requirement coverage, scope, documentation impact—naming the exact pages that will change—architecture decisions, risks, and plan completeness. It does not implement application code.

Invoke explicitly with:

```text
/skill:change-propose <description or change name>
```

### `change-implement`

Implements every unchecked task in an existing `PLAN.md`, verifies the specified behavior, updates the project's documentation in the categories that own the change, and creates or supersedes ADRs when durable decisions emerge. It keeps `SPEC.md` and `PLAN.md` accurate when implementation discoveries invalidate assumptions.

Invoke with:

```text
/skill:change-implement <change name>
```

It leaves the completed change directory in place for the final audit.

### `change-finish`

Independently audits the completed behavior against the spec and plan, reconciles the project's documentation and ADRs, performs final verification, and removes `changes/<name>/`. It never archives the directory.

Invoke with:

```text
/skill:change-finish <change name>
```

There is no separate review skill. Proposal, implementation, and finishing each contain the review appropriate to that stage.

## Lifecycle

### 1. Propose

`change-propose` researches the repository and creates two readable files.

`SPEC.md` answers:

- Who benefits?
- What becomes possible or better?
- What does the user or operator experience?
- What happens on success, failure, unavailability, and recovery?
- What observable requirements and invariants apply?
- What is in scope and explicitly out of scope?
- Which documentation pages change, and in which category of the project's documentation?
- When a vision exists, how does the change align with it?

`PLAN.md` answers:

- What is the smallest coherent implementation approach?
- Which components and interfaces are affected?
- Does a durable decision require an ADR?
- What material risks need mitigation or verification?
- What ordered, verifiable tasks deliver every requirement?

### 2. Implement

`change-implement` treats the plan as a checked hypothesis, not unquestionable truth. It validates the plan against current code before editing, then completes and verifies each task. Markdown checkboxes are durable status; a session task tracker may mirror them but does not replace them.

Implementation discoveries go to the artifact that owns them:

| Discovery | Action |
|---|---|
| Implementation approach changes, behavior does not | Update `PLAN.md` |
| Desired observable behavior or scope changes | Update `SPEC.md` first |
| Durable architectural decision emerges | Add or supersede an ADR |
| Product behavior changes | Update the project's documentation |
| Transient investigation detail | Leave it in the working conversation or Git history |

A spec is never weakened after the fact to excuse incomplete implementation.

### 3. Finish

`change-finish` checks behavior rather than trusting task checkboxes. It confirms:

- implementation satisfies every spec requirement;
- the plan is complete and no obsolete path remains;
- the project's documentation stands alone without the change files and each part sits in the category that owns it;
- durable architectural decisions are recorded as ADRs;
- the delivered result aligns with `VISION.md` when one exists;
- focused tests and the real changed surface pass.

It then deletes the completed change directory. There is no validation command, spec sync, or archive step.

## Optional `VISION.md`

`VISION.md` is optional. If it is absent, all three skills skip vision alignment completely. They do not create it, treat its absence as a defect, insert placeholder vision sections, or add vision tasks.

When present, it should describe stable long-term direction rather than current implementation or a task backlog:

```markdown
# Vision

## Product promise

## Desired experience

## Product boundaries

## Principles

## Long-term direction

## Strategic tensions
```

Each proposed and delivered change is compared against that vision. A conflict has two valid resolutions:

1. reshape the change so it delivers the desired outcome while respecting the vision; or
2. make an explicit strategic decision to update the vision.

The skills never edit `VISION.md` merely to make a change appear aligned.

## `SPEC.md` shape

With `VISION.md`:

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

Without `VISION.md`, omit `## Vision alignment` entirely.

The spec focuses on observable experience. Implementation files, classes, database columns, and task ordering belong in the plan unless they are themselves public interfaces. Examples are encouraged when they remove ambiguity, but formal SHALL statements and a scenario for every requirement are not required.

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

Every requirement maps to one or more tasks, and every task maps back to stated scope. Applicable documentation, ADR work, behavioral tests, and real-surface verification are tasks rather than afterthoughts.

## Product documentation

The project's documentation replaces cumulative capability specifications. It describes the product as it currently works and should be useful to users, operators, and agents.

This workflow does not define where that documentation lives; the project does. Each skill finds the destination the same way:

1. Read the documentation entry point—`docs/index.md`, `docs/README.md`, the repository README, or a contributing guide—to learn how the set is organized.
2. Identify the framework and any audience split it uses. Diátaxis (`tutorials/`, `how-to/`, `reference/`, `explanation/`) and per-audience roots such as `docs/use/` and `docs/develop/` are common; a project may use its own structure.
3. Read the pages that already cover the affected area. They are the default destination.

Each piece of information goes to the category that owns it. With Diátaxis:

| Information the change produces | Category |
|---|---|
| Precise current behavior: interfaces, options, states, errors, schemas | Reference |
| A goal the reader now accomplishes differently, or at all | How-to |
| A changed mental model, concept, or rationale that is not architectural | Explanation |
| A first-run path that no longer works as written | Tutorial |
| Why a durable architectural choice was made | ADR |

An audience split is respected the same way: operator-facing behavior goes to the operator set, contributor-facing behavior to the contributor set, and behavior serving both is written once for its primary audience and linked from the other.

Between them, the updated pages should cover the applicable purpose, normal flow, states and actions, failures and recovery, configuration, and public interfaces—distributed across categories rather than concentrated in a single page per feature.

Existing pages are updated in place. A new page is added only when the change introduces something the current structure has no home for, and it is then placed in the correct category and linked from that category's index. Superseded claims are removed rather than accumulating historical deltas. The change spec explains the intended delta while active; the project's documentation explains the resulting current state permanently.

If a repository has no documentation set at all, the skills create the smallest useful set under `docs/` for the affected area and say so. They do not impose a framework the project has not chosen.

## Architecture decision records

ADRs explain why durable architectural choices exist. They do not describe ordinary implementation details or duplicate feature behavior.

Default format:

```markdown
# ADR 0007: <Decision>

- Status: Accepted
- Date: YYYY-MM-DD

## Context

## Decision

## Consequences

## Alternatives considered
```

Use the next available zero-padded number. Never rewrite the substance of an accepted ADR. A later decision adds a new ADR and changes the earlier status to `Superseded by ADR-NNNN`.

A placement test:

| Information | Destination |
|---|---|
| Long-term product principle | `VISION.md`, when the project uses one |
| Desired behavior for active work | `changes/<name>/SPEC.md` |
| Implementation order and affected code | `changes/<name>/PLAN.md` |
| Current user or operator behavior | The project's documentation, in the category its framework assigns |
| Reason for a durable architecture choice | ADR |
| Historical implementation discussion | Git commit or pull request |

## How this differs from OpenSpec

| Concern | OpenSpec-style workflow | This workflow |
|---|---|---|
| Change intent | Proposal plus delta specs | `SPEC.md` |
| Technical design and tasks | Separate design and task artifacts | `PLAN.md` with embedded tasks |
| Current behavior | Cumulative capability specs | The project's own documentation, in its own structure |
| Architecture history | Often embedded in change design | ADRs |
| Long-term direction | External configured context | Optional `VISION.md` |
| Status | CLI and artifact schema | Plan checkboxes |
| Completion | Validate, sync, archive | Reconcile durable docs, then delete |
| Historical record | Archived change tree | Git and ADRs |

The workflow trades machine-validated delta semantics for readability and a smaller number of authoritative places. Its rigor comes from semantic checks in every skill: scope, requirement-to-task coverage, correctly placed documentation, durable decisions, real verification, and optional vision alignment.

## Non-goals

This workflow intentionally provides no:

- CLI;
- YAML metadata or generated indexes;
- archived or completed change directory;
- sync operation;
- formal requirement language requirement;
- documentation location of its own;
- separate review skill;
- automatic `VISION.md` creation.
