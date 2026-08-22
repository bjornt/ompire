# ADR 0001: Adopt the lightweight skills-based change workflow

- Status: Accepted
- Date: 2026-08-22

## Context

Ompire accumulated durable knowledge across 27 OpenSpec capability specifications and 32 archived change designs. The OpenSpec lifecycle also separated proposals, designs, delta specifications, task lists, synchronization, validation, and archives. That structure provided machine-validated delta semantics, but it created several potentially authoritative locations for current behavior and architectural rationale.

The repository now defines a smaller lifecycle through three skills. `change-propose` creates an observable-behavior specification and an implementation plan in `changes/<name>/`. `change-implement` delivers the plan and reconciles implementation discoveries into the artifact that owns them. `change-finish` independently audits the result, updates durable documentation, and deletes the temporary change directory rather than synchronizing or archiving it.

This separates temporary coordination from durable project knowledge: long-term direction, current feature documentation, and ADRs remain durable, while active change artifacts do not. Legacy OpenSpec artifacts remain migration evidence, not the destination for new work.

This ADR is a backfill. No reliable original acceptance date was recorded, so its creation date is used.

## Decision

Ompire uses the repository-local, skills-based Markdown workflow for new planned changes:

- An active change lives in `changes/<name>/` and contains `SPEC.md` for desired observable behavior and `PLAN.md` for the implementation approach, risks, affected areas, and verifiable tasks.
- The `change-propose`, `change-implement`, and `change-finish` skills own proposal, delivery, reconciliation, and completion. There is no specification CLI, generated metadata, sync operation, or archive operation in this workflow.
- Current behavior is maintained in feature documentation, durable architectural rationale is maintained in ADRs, and long-term product direction remains in `VISION.md` when present.
- A change is complete only after its implementation and durable documentation have been reconciled and verified. Its temporary directory is then deleted; Git and the associated commit or pull request retain the delivery history.
- Existing OpenSpec specifications and archived changes may be consulted while their durable knowledge is migrated, but new work does not update or extend them as an authoritative specification system.

The invariant is that temporary change artifacts coordinate active work only. They must not become a second permanent source of current behavior or architectural rationale.

## Consequences

The project has fewer authoritative locations, and each durable document has one role. Change artifacts are ordinary Markdown, readable without a global tool, and can evolve with normal repository conventions. Proposal, implementation, and finishing still require requirement coverage, documentation reconciliation, architectural review, and real verification.

The project gives up OpenSpec's machine-validated artifact schemas, delta semantics, synchronization, and archive commands. Workflow rigor therefore depends on the skills' semantic checks, plan checkboxes, review, and repository verification rather than a dedicated specification CLI.

Deleting completed change directories removes a convenient repository-local archive. Investigating implementation history requires Git, the commit or pull request, and any ADR that preserved a durable decision. This is accepted to prevent completed plans and superseded change descriptions from competing with current feature documentation.

Migration is incremental. Until relevant legacy OpenSpec knowledge has been reconciled, authors must consult those artifacts as evidence and synthesize durable content into feature documentation or ADRs; they must not copy historical change documents wholesale or treat migration evidence as the current workflow.

Revisit this decision if the project requires enforceable cross-artifact schemas or traceability guarantees that the skills-based workflow cannot provide without duplicating authoritative documentation. A replacement requires a new ADR rather than rewriting this one.

## Alternatives considered

### Retain OpenSpec as the change workflow

OpenSpec provides formal artifact types, machine validation, delta specifications, synchronization, and an explicit archive. It was rejected because those mechanics distribute knowledge across more files and lifecycle states than this repository needs, retain a global tool dependency, and risk making generated or archived artifacts compete with current feature documentation.

### Use unstructured issues and direct implementation

Issues and conversational plans would eliminate nearly all repository-local process. They were rejected because substantial changes still need a durable statement of observable behavior, an implementation plan, requirement-to-task coverage, and a completion audit that can survive across sessions.

### Keep completed change directories permanently

A permanent `changes/` archive would make historical plans directly browsable without Git. It was rejected because completed specifications and plans become stale snapshots that duplicate current feature documentation and ADRs. Git already preserves the historical artifacts and their delivery context.
