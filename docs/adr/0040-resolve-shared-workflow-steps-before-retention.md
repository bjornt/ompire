# ADR 0040: Resolve shared workflow steps before retention

- Status: Accepted
- Date: 2026-09-30

Extends [ADR-0028](0028-retain-declarative-workflow-revisions.md) and
[ADR-0033](0033-scope-trusted-delivery-authority-to-the-workflow-run.md) with
workflow format 5. Formats 1–4 keep their canonical identities and semantics;
review and delivery retain their existing authority owners.

## Context

Workflows repeated review routing, approval structure, and signed publication
chains. Their operator readers exposed the entire flat execution graph,
including routing and exhaustion nodes, as equally prominent steps. A bugfix
was therefore presented as 26 detailed declarations rather than seven meaningful
phases. Sharing mutable definitions directly at runtime would shorten authoring
but undermine the exact procedure a task accepted. Merely sharing display labels
would leave duplicated executable policy in every workflow.

## Decision

Format 5 separates a globally reusable definition from a workflow's invocation
of it. The initial global catalog consists of bounded packaged YAML resources,
available to custom workflows through the authoring API. Invocations supply
explicit typed bindings for workflow-specific evidence, engine identities,
questions, choices, correction destinations, and named endings.

Composition is structural data processing before ordinary validation. Exact
parameter nodes replace one JSON value, and array-splice nodes replace one array
element with the declared array binding. Substituted data is never parsed as a
template again. No nested invocation, arbitrary source, text interpolation, or
new runtime step kind exists. The complete expansion must satisfy existing
reference, loop-bound, review-binding, and publication-grant rules.

An executable revision retains both its ordinary expansion and its composition:
source invocations, every used shared definition with its verified content
revision, and meaningful phase membership. Runtime execution consumes the
expanded steps only. Retained decoding verifies the source/snapshot expansion
against those steps without consulting the global catalog. Self-contained
exports preserve invocations and snapshots and import to the same revision.

Catalog changes affect prospective saves only. An embedded snapshot takes
precedence over the deployed catalog; adopting the current definition requires
an explicit draft edit and executable save. No saved workflow or task moves
because a dependency's name still exists with different content.

Phases group execution step identities under pinned labels and descriptions.
They may include a later exception gate without reordering execution. Compact
readers show possible destinations and privileged effects; expanded readers
retain exact declarations and attempt history. Clients neither evaluate
predicates nor reinterpret a phase visit as completion of every possible branch.

## Consequences

Shared review, approval, and delivery behavior has one authoring owner, while
workflows retain explicit policy choices. Existing engine identities and
attempt provenance remain stable when packaged workflows adopt composition.
Global reuse adds no execution or publication authority.

Retained documents are larger because they include both source and expansion,
but remain bounded by the existing document limits. Phase labels and membership
participate in content identity: a historical procedure must retain the view
that explains it, not borrow labels from today's library.

The initial catalog is read-only packaged data. Operators can compose and
configure its definitions, but modifying the global catalog itself requires a
daemon package change. A separate mutable global library would need its own
versioned authoring and transactional save rules; it is not simulated through
unvalidated paths or filesystem imports.

## Alternatives considered

### Resolve global names when a task runs

Rejected: updates, removal, or damage to the deployed library would change or
block accepted work even when its retained procedure should remain complete.

### Inline shared steps and discard composition

Rejected: execution would remain safe, but exported authoring would duplicate
policy again and readers could not recover the meaningful shared units.

### Add phase labels without executable reuse

Rejected: a compact screen alone does not remove repeated workflow declarations.

### Build a general nested subworkflow interpreter

Rejected: recursion and runtime composition add execution semantics and recovery
complexity unnecessary for sharing ordinary review and publication operations.
