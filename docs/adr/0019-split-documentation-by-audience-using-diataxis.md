# ADR 0019: Split documentation into operator and contributor sets organized by Diátaxis

- Status: Proposed
- Date: 2026-08-22

## Context

The repository has no documentation. It has durable knowledge — a vision
document, architecture decision records, retiring capability specifications, an
oversized handoff design document, and a short agent instruction file — but no
material that an operator or a contributor can read to become productive.

Two unrelated audiences need documentation, and almost nothing serves both. An
operator installs the product, registers a source repository, configures
signing and forge access, spawns tasks, answers gates, reviews results, and
ships pull requests. A contributor changes the control plane, the presentation
layer, workflow definitions, or the end-to-end test harness. An operator must
never need to understand the internal process supervision model to configure
signing; a contributor must not have to read installation prose to find the
module map. Documentation sets that mix these audiences degrade for both,
because every page must hedge about who it is addressing.

A durable knowledge map already exists and is enforced by the change workflow
adopted in ADR-0001. Long-term direction lives in the vision document, durable
rationale lives in architecture decision records, and current product behavior
lives in feature documentation that the change skills create and reconcile on
every change. Only feature documentation is maintained automatically as
behavior changes. Any documentation structure that creates a second home for
current behavior therefore guarantees drift, because the hand-written copy has
no owner in the workflow.

A structure is also needed that tells an author — human or agent — which page a
piece of information belongs on. Without such a rule, documentation collapses
into one page per product area in which a learning path, an operational
procedure, an interface listing, and a rationale are interleaved. The Diátaxis
framework provides that rule by separating learning-oriented, task-oriented,
information-oriented, and understanding-oriented material.

This record is a backfill in the sense that it establishes a convention where
none existed; there is no earlier acceptance date to preserve.

## Decision

Documentation is organized as two audience-scoped sets, each internally
organized by the four Diátaxis quadrants.

- `docs/use/` documents the product for operators.
- `docs/develop/` documents the repository for contributors.
- Each set contains `tutorials/`, `how-to/`, `reference/`, and `explanation/`,
  and each has its own landing page that states its audience and links to the
  other set.
- `docs/index.md` is a chooser that routes a reader to one set or the other.

No page serves both audiences. When both audiences need the same information,
each set states what its audience needs and links across rather than sharing a
page.

Feature documentation and architecture decision records remain at their
established flat locations, `docs/features/` and `docs/adr/`. They are not a
third documentation set. They are surfaced inside the two sets through
navigation and links:

- a feature document is reference material, presented under the reference
  quadrant of whichever audience its behavior belongs to;
- an architecture decision record is explanation material, presented under the
  contributor set's explanation quadrant.

Current product behavior is documented only in `docs/features/`. Reference
pages in either set describe stable interfaces — configuration, the external
API, the protocol, enumerated states — or index feature documentation. They do
not restate feature behavior.

Rationale is split by kind: a decision that could reasonably be reversed by
someone unaware of its constraints is an architecture decision record; a mental
model that helps a reader understand how the parts relate is an explanation
page. An explanation page links to decision records rather than reproducing
their reasoning.

The vision document remains at the repository root and is linked from both
landing pages rather than moved into either set.

The invariant future changes must preserve: current behavior has exactly one
authoritative location, and no documentation page addresses both audiences at
once.

## Consequences

Each audience gets an entry point that is immediately useful, and neither has
to filter the other's material. The quadrant structure gives authors, including
the change skills, a placement rule rather than a judgment call, which matters
because documentation here is co-authored by agents rather than by a single
maintainer holding the structure in their head.

Retaining the flat `docs/features/` and `docs/adr/` locations means the change
workflow's skills continue to work without modification, and the repository
avoids the second documentation convention that the workflow explicitly
forbids. The cost is that placing a feature document into the right audience's
reference section is a navigation decision made outside the file itself, so
adding a feature document requires a navigation update. Splitting
`docs/features/` by audience would remove that step but would invalidate the
established convention the skills name; the navigation cost is accepted
instead.

The separation is a maintenance obligation. Some material — the trust boundary,
the workflow model, the attention model — is genuinely interesting to both
audiences and will be written twice at different depths. That duplication is
deliberate: two calibrated explanations serve readers better than one hedged
page, but the two copies can contradict each other and must be reconciled when
the underlying behavior changes.

Diátaxis is a discipline, not a schema. Nothing validates that a page is in the
correct quadrant, and pages will occasionally be misfiled. The placement rules
above are the mitigation; a misfiled page is corrected by moving it, not by
adding a fifth category.

Revisit this decision if the audiences converge — if the product is
consistently operated only by the people who build it, the cost of maintaining
two sets stops being repaid — or if the contributor set never accumulates
enough material to justify a separate tree.

## Alternatives considered

### One unified documentation set

A single Diátaxis tree covering both product use and repository development.
This is the cheapest structure to maintain and removes the duplication problem
entirely. Rejected because the operator set is the one with no substitute: a
contributor who cannot find documentation can read the source, while an
operator cannot. Interleaving internals into the operator path degrades exactly
the audience that depends on documentation most.

### Two separately published sites

Independent configurations and URLs for the two sets. This gives the strongest
separation and makes it impossible for a reader to wander from one audience
into the other. Rejected because it duplicates infrastructure, turns every
cross-reference between the sets into an external link, and gives shared
material two independently drifting homes for no benefit that navigation
separation does not already provide at this scale.

### Diátaxis quadrants at the top level, audience below

A tree of `tutorials/`, `how-to/`, `reference/`, and `explanation/`, each split
by audience. This keeps the framework's vocabulary at the highest level and is
the more conventional reading of it. Rejected because it forces both audiences
through the same entry point and defers the audience question to the second
level, which is the precise outcome the split exists to prevent. The audience
distinction here is stronger than the quadrant distinction.

### Structure by product area instead of Diátaxis

Top-level sections per capability, mirroring the feature documentation the
change skills already produce. This matches the shape of the material being
migrated and requires no placement rule. Rejected because it reproduces the
failure the framework addresses: each area's page accumulates a learning path,
procedures, interface listings, and rationale together, and readers with
different needs all fail on the same page.

### Retain the handoff design document as the architecture reference

Keeping the existing single large design document as the contributor-facing
architecture material, and writing only operator documentation. This is the
least work and preserves an artifact that is currently accurate. Rejected on
the same grounds as the ADR migration itself: one oversized document mixes
durable decisions with current behavior and implementation detail, and it has
no mechanism keeping any of the three current.
