# The change workflow

Changes to Ompire are delivered through three agent skills and ordinary
Markdown. There is no CLI, no schema, no validation command, no sync
operation, and no archive.

The operational reference is `.omp/skills/README.md` in the repository root.
This page explains why the workflow has this shape.

## The model

```text
                         durable project knowledge
                    ┌───────────────────────────────┐
                    │ VISION.md                     │
                    │ docs/features/  (behavior)    │
                    │ docs/adr/       (rationale)   │
                    └───────────────▲───────────────┘
                                    │ reconcile
                                    │
idea ──▶ changes/<name>/SPEC.md ──▶ PLAN.md ──▶ implementation ──▶ delete
```

The separation is between **temporary coordination** and **durable
knowledge**. `changes/<name>/` exists to coordinate one piece of work and is
deleted when that work is done. What survives is the vision, the current
behavior, and the reasons.

## The artifacts

`SPEC.md` — the desired observable experience. Who benefits, what becomes
possible, what the operator sees, what happens on failure and recovery, what
is out of scope, which documentation changes.

`PLAN.md` — the implementation approach and an ordered task list with
checkboxes. Checkboxes are the durable status; a session task tracker may
mirror them but never replaces them.

Neither survives the change.

## The skills

| Skill | Does |
|---|---|
| `change-propose` | Researches the repository, writes `SPEC.md` and `PLAN.md`. Never touches application code. |
| `change-implement` | Implements every unchecked task, updates feature documentation, creates ADRs when durable decisions emerge. |
| `change-finish` | Independently audits behavior against the spec, reconciles docs and ADRs, deletes the change directory. |

There is no separate review skill. Each stage contains the review appropriate
to it — which is the point: review that happens at a stage boundary is
cheaper than review bolted on at the end.

## Where a discovery goes

| Discovery | Action |
|---|---|
| Approach changes, behavior does not | Update `PLAN.md` |
| Desired behavior or scope changes | Update `SPEC.md` **first** |
| A durable architectural decision emerges | Add or supersede an [ADR](../how-to/write-an-adr.md) |
| Product behavior changes | Update [feature documentation](../how-to/write-a-feature-doc.md) |
| Transient investigation detail | Leave it in the conversation or Git history |

**A spec is never weakened after the fact to excuse an incomplete
implementation.** This is the rule the whole workflow rests on. Without it,
"the spec says what we built" is trivially true and the spec means nothing.

## Why OpenSpec was replaced

The previous workflow used proposals, design documents, delta specs, a
validation command, a sync operation, and an archive tree. It produced
machine-validated delta semantics and a permanent record of every change.

It also produced many authoritative locations for the same knowledge, a CLI
dependency, generated metadata, and an archive that accumulated documents
nobody read.

The trade made in [ADR-0001](../../adr/0001-adopt-lightweight-skills-based-change-workflow.md):
give up machine validation, keep ordinary Markdown and fewer places for the
truth to live. Rigor moves from schema checks into semantic checks inside each
skill — scope, requirement-to-task coverage, documentation currency, durable
decisions, real verification, vision alignment.

That is a real trade, not a free win. Nothing now catches a requirement with
no task except a skill reading carefully.

## Vision alignment

Each proposed and delivered change is compared against
[`VISION.md`](../../VISION.md). A conflict has exactly two valid
resolutions:

1. Reshape the change so it respects the vision, or
2. Make an explicit strategic decision to update the vision.

The skills never edit `VISION.md` merely to make a change appear aligned.
Silent drift is how a vision becomes decoration.
