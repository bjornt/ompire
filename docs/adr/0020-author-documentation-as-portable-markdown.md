# ADR 0020: Author documentation as portable Markdown and treat the site generator as a presentation layer

- Status: Proposed
- Date: 2026-08-22

## Context

Documentation needs a source format and, eventually, a published site with
navigation and search. These are separable choices, and coupling them commits
the project to a toolchain before any documentation exists to justify one.

Three properties of this repository constrain the format choice.

Documentation here is co-authored by agents. The change workflow adopted in
ADR-0001 requires that current feature documentation be updated and reconciled
as part of delivering every change. The format must therefore be one that an
agent produces correctly without a build step to validate against, and one in
which a malformed construct degrades to readable text rather than to a broken
page.

The repository has just moved away from tool-enforced structure. ADR-0001
replaced a specification CLI, its schema, validation, synchronization, and
archive mechanics with ordinary Markdown, accepting less machine validation in
exchange for fewer authoritative locations and a format that needs no tooling
to read. A documentation toolchain that reintroduces a markup dialect and a
mandatory build to read the source would contradict that tradeoff in the
adjacent domain.

The product is an application, not a library. The control plane is a private
service with no third-party consumers importing it. Generated API
documentation from its internals would produce volume without readers, and the
interfaces that are externally meaningful — operator configuration, the
external API, the installed package — are not covered by source-level
extraction.

A published site remains a genuine requirement. An operator installing the
product needs search, navigation, and a stable URL, none of which a repository
tree provides. Both major documentation toolchains publish to the same hosting
services, so the hosting requirement does not by itself select a toolchain.

## Decision

Documentation is authored as portable Markdown. Every page renders correctly
and completely in a plain Markdown viewer, including the repository host's,
with no build step and no generator-specific markup. Admonitions, cross-page
reference syntax, content inclusion, and other generator directives are not
used in page bodies. Front matter and configuration files are permitted,
because they do not affect whether the body of a page can be read.

A static site generator is a presentation layer over that same tree, not a
prerequisite for reading it. The generator supplies navigation, search,
theming, and link checking; it supplies no content and no meaning. The current
choice of generator is MkDocs with the Material theme, configured over the
existing documentation directory, publishing the two audience sets as separate
navigation sections of one site.

Adopting the generator is a separate step from writing documentation, and
writing is not blocked on it. The source layout is identical with or without a
generator.

The invariant future changes must preserve: removing the site generator leaves
the documentation complete and readable.

## Consequences

Documentation stays readable at its source, which is where contributors and
agents encounter it, and correct authoring requires no knowledge of the
toolchain. Because content carries no generator-specific markup, the generator
becomes a cheap and reversible choice rather than a commitment, and the
decision to publish can be made once there is enough material to publish.

Link integrity, navigation, and search depend on the generator's build. Running
that build in continuous integration in a mode that fails on broken references
is what keeps cross-page links honest; without it, the portable-Markdown
constraint gives no protection against a link rotting.

The constraint has real costs. Admonitions and other visual affordances are not
available, so emphasis must be carried by structure and prose. Cross-page
references are ordinary relative links rather than resolved symbolic
references, so moving a page requires updating the links that point at it, and
only the build detects the ones that were missed. Navigation is configured
rather than derived from the tree, so adding a page requires a configuration
change.

No documentation is generated from source code. Interface reference material is
either hand-written or generated from an interface description the service
already produces, and the accuracy of hand-written reference pages depends on
the change workflow reconciling them.

Revisit this decision if the product exposes a public plugin interface that
third parties import, which would create a real payoff for source-level
extraction and cross-project reference resolution, or if the documentation
grows to a scale where configured navigation and relative links stop being
manageable.

## Alternatives considered

### Sphinx with MyST Markdown

The stronger toolchain in general: mature cross-referencing that survives page
moves, source-level API extraction, cross-project reference resolution, and
first-class versioned publishing. Rejected because its principal advantages do
not apply here — the source-level extraction has no audience for an application
with no importers, and the hosting service that motivated the suggestion builds
either toolchain. Against that, its Markdown dialect introduces directives that
do not render at the repository host and that agents author less reliably than
plain Markdown, which conflicts with both the agent-authored workflow and the
tradeoff accepted in ADR-0001. The supersession trigger is a public plugin
interface.

### Sphinx with reStructuredText

The same capabilities without the dialect ambiguity of a Markdown superset.
Rejected for the same absent payoff, plus a larger cost: it makes
reStructuredText a second authoring language in a repository whose entire
change workflow is built on ordinary Markdown, and every agent-authored
documentation update would have to switch languages depending on which file it
touches.

### A JavaScript documentation framework

Strong product-documentation presentation and a good operator reading
experience. Rejected because it adds a second build and dependency tree in a
language ecosystem the repository deliberately confines to the presentation
layer, in exchange for presentation benefits that the chosen generator already
provides.

### No site generator at any point

The lowest possible infrastructure cost, perfect fidelity for agent authoring,
and no build to keep working. Rejected as an end state because it offers no
search, no navigation beyond directory listings, and no stable published
location, which fails the operator audience specifically. It is accepted as the
starting state: the portable-Markdown constraint means the project sits in this
configuration until a generator is added, and loses nothing by doing so.

### Serving documentation from the running service

Keeping documentation with the installed product, so an operator always reads
the version they are running. Rejected because it couples documentation
releases to service releases, makes documentation unreadable to anyone
evaluating the product before installing it, and collides with a route the
service already serves.

### Requiring the generator to read the source

Permitting generator-specific markup and treating the built site as the only
supported reading surface. This is the conventional arrangement and unlocks the
full feature set of any toolchain. Rejected because it makes the toolchain a
dependency of comprehension: contributors and agents read documentation at its
source far more often than they read a published site, and a format only
legible after a build is a format that degrades silently when the build is not
run.
