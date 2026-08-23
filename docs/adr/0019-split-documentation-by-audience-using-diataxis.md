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
is written and reconciled by the change skills on every change. The skills name
no documentation location. They discover the documentation the repository
maintains and place work in the category that documentation's own structure
assigns, which means the structure chosen here is the structure the workflow
will maintain — and that any second home for current behavior would drift,
because only one of the two copies has an owner in the workflow.

A structure is also needed that tells an author — human or agent — which page a
piece of information belongs on. Without such a rule, documentation collapses
into one page per product area in which a learning path, an operational
procedure, an interface listing, and a rationale are interleaved. The Diátaxis
framework provides that rule by separating learning-oriented, task-oriented,
information-oriented, and understanding-oriented material.

That rule only pays if it governs the bulk of the material. A structure that
keeps current behavior in a separate tree outside the quadrants — one file per
feature, surfaced into the sets through navigation — exempts most of the
documentation from the placement rule, expresses each page's audience and
category only in a navigation file rather than in its location, and leaves
behavior split across two locations with no rule saying which holds what.

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

Current product behavior is documented inside these sets, in the quadrant that
owns it. There is no separate location for it.

- Behavior an operator observes is documented in `docs/use/`; behavior only a
  contributor observes is documented in `docs/develop/`.
- Within a set, precise current behavior — interfaces, options, states, errors,
  schemas — belongs to `reference/`. Procedures belong to `how-to/`, mental
  models to `explanation/`, and guaranteed first-run paths to `tutorials/`.

A page's location states its audience and its quadrant. Navigation reflects
that location rather than supplying it.

Architecture decision records remain at `docs/adr/`. They are not a third
documentation set; a decision record is explanation material, presented under
the contributor set's explanation quadrant. Rationale is split by kind: a
decision that could reasonably be reversed by someone unaware of its
constraints is an architecture decision record; a mental model that helps a
reader understand how the parts relate is an explanation page. An explanation
page links to decision records rather than reproducing their reasoning.

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

The placement rule governs all documentation rather than the fraction outside a
separate behavior tree. A page's path carries its audience and quadrant, so a
misfiled page is visible as a wrong path rather than only as a wrong navigation
entry, and adding a page is one decision — which set, which quadrant — instead
of a directory choice followed by an unrelated navigation choice.

Material about one product area is free to split across quadrants as the
framework intends, and the sets grow by revising existing pages rather than by
adding parallel ones. Pages describing the same subject sit beside each other,
so overlaps are visible and expected to be merged rather than left to drift.

Behavior no longer has a single directory to enumerate, so "what does this
product do" is answered by two reference sections rather than one index. The
landing pages carry that burden and must stay complete; a page absent from its
set's landing page is effectively unreachable. The reference sections are large
and their internal grouping is hand-maintained. If a set's reference section
grows past what a landing page can usefully list, the answer is subdirectories
within the quadrant, not a return to a separate behavior tree.

The separation between the sets is a maintenance obligation. Some material —
the trust boundary, the workflow model, the attention model — is genuinely
interesting to both audiences and will be written twice at different depths.
That duplication is deliberate: two calibrated explanations serve readers
better than one hedged page, but the two copies can contradict each other and
must be reconciled when the underlying behavior changes.

Diátaxis is a discipline, not a schema. Nothing validates that a page is in the
correct quadrant, and pages will occasionally be misfiled. The placement rules
above are the mitigation; a misfiled page is corrected by moving it, not by
adding a fifth category.

Revisit this decision if the audiences converge — if the product is
consistently operated only by the people who build it, the cost of maintaining
two sets stops being repaid — or if the contributor set never accumulates
enough material to justify a separate tree.

## Alternatives considered

### Keep current behavior in a separate flat tree surfaced through navigation

One file per feature in its own directory, mounted into each set's reference
quadrant by the navigation configuration. This keeps behavior documents in one
enumerable place and lets a set's own reference pages stay short. Rejected
because it exempts the bulk of the documentation from the placement rule the
framework exists to provide: each file accumulates a purpose statement, an
operator procedure, an interface listing, and failure behavior together, and
its audience and quadrant live in a navigation file rather than in its
location. It also leaves behavior split across two locations with no rule
saying which holds what, which is how two descriptions of the same protocol
drift apart.

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

Top-level sections per capability. This matches the shape of the capability
specifications being migrated and requires no placement rule. Rejected because
it reproduces the failure the framework addresses: each area's page accumulates
a learning path, procedures, interface listings, and rationale together, and
readers with different needs all fail on the same page.

### Retain the handoff design document as the architecture reference

Keeping the existing single large design document as the contributor-facing
architecture material, and writing only operator documentation. This is the
least work and preserves an artifact that is currently accurate. Rejected on
the same grounds as the ADR migration itself: one oversized document mixes
durable decisions with current behavior and implementation detail, and it has
no mechanism keeping any of the three current.
