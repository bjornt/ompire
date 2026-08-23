# Lightweight change workflow

This repository can define and deliver changes with ordinary Markdown and three agent skills. It needs no specification CLI, generated metadata, sync operation, archive, or permanent completed-change directory.

The model separates temporary coordination from durable project knowledge:

```text
                         durable project knowledge
                    ┌───────────────────────────────┐
                    │ docs/VISION.md     optional   │
                    │ feature documentation         │
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
docs/features/*.md                default location for current feature documentation
docs/adr/NNNN-*.md                default location for architectural decisions
```

Existing repository conventions take precedence over the default `docs/features/` and `docs/adr/` locations. Do not create a second documentation or ADR convention beside one already in use.

`changes/<name>/` contains exactly the active planning artifacts needed for the workflow. Once the change is complete and its durable knowledge is reconciled, the directory is deleted. Git and the associated commit or pull request retain history.

## Skills

Skills live one level below `.omp/skills/`, so Oh My Pi discovers them automatically in a new session.

### `change-propose`

Creates or refines:

```text
changes/<name>/SPEC.md
changes/<name>/PLAN.md
```

It reads current documentation, ADRs, relevant code, and `VISION.md` when present. It reviews requirement coverage, scope, documentation impact, architecture decisions, risks, and plan completeness. It does not implement application code.

Invoke explicitly with:

```text
/skill:change-propose <description or change name>
```

### `change-implement`

Implements every unchecked task in an existing `PLAN.md`, verifies the specified behavior, updates current feature documentation, and creates or supersedes ADRs when durable decisions emerge. It keeps `SPEC.md` and `PLAN.md` accurate when implementation discoveries invalidate assumptions.

Invoke with:

```text
/skill:change-implement <change name>
```

It leaves the completed change directory in place for the final audit.

### `change-finish`

Independently audits the completed behavior against the spec and plan, reconciles current feature documentation and ADRs, performs final verification, and removes `changes/<name>/`. It never archives the directory.

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
- Which current feature documentation changes?
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
| Product behavior changes | Update current feature documentation |
| Transient investigation detail | Leave it in the working conversation or Git history |

A spec is never weakened after the fact to excuse incomplete implementation.

### 3. Finish

`change-finish` checks behavior rather than trusting task checkboxes. It confirms:

- implementation satisfies every spec requirement;
- the plan is complete and no obsolete path remains;
- current feature documentation stands alone without the change files;
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

## Feature documentation

Feature documentation replaces cumulative capability specifications. It describes the product as it currently works and should be useful to users, operators, and agents.

A feature document normally covers:

```markdown
# <Feature>

## Overview

## Using <feature>

## States and behavior

## Failures and recovery

## Configuration

## Interfaces
```

Use only applicable sections. Update or remove superseded claims instead of appending historical deltas. The change spec explains the intended delta while active; feature documentation explains the resulting current state permanently.

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
| Current user or operator behavior | Feature documentation |
| Reason for a durable architecture choice | ADR |
| Historical implementation discussion | Git commit or pull request |

## How this differs from OpenSpec

| Concern | OpenSpec-style workflow | This workflow |
|---|---|---|
| Change intent | Proposal plus delta specs | `SPEC.md` |
| Technical design and tasks | Separate design and task artifacts | `PLAN.md` with embedded tasks |
| Current behavior | Cumulative capability specs | User- and agent-useful feature documentation |
| Architecture history | Often embedded in change design | ADRs |
| Long-term direction | External configured context | Optional `VISION.md` |
| Status | CLI and artifact schema | Plan checkboxes |
| Completion | Validate, sync, archive | Reconcile durable docs, then delete |
| Historical record | Archived change tree | Git and ADRs |

The workflow trades machine-validated delta semantics for readability and a smaller number of authoritative places. Its rigor comes from semantic checks in every skill: scope, requirement-to-task coverage, current documentation, durable decisions, real verification, and optional vision alignment.

## Non-goals

This workflow intentionally provides no:

- CLI;
- YAML metadata or generated indexes;
- archived or completed change directory;
- sync operation;
- formal requirement language requirement;
- separate review skill;
- automatic `VISION.md` creation.
