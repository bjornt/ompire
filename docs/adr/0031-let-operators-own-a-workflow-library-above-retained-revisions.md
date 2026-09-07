# ADR 0031: Let operators own a workflow library above retained revisions

- Status: Accepted
- Date: 2026-09-07

Extends [ADR-0028](0028-retain-declarative-workflow-revisions.md) and
[ADR-0026](0026-resolve-launch-inputs-once-and-pin-them-to-the-task.md).

## Context

ADR-0028 made a workflow definition a document rather than code, retained it by
content identity, and pinned one to each task. It deliberately stopped short of
letting anyone add a document: the catalog was daemon-packaged definitions only,
and it said so as an explicitly temporary boundary. That boundary is now the
whole remaining obstacle — an operator who needs a different procedure has to
edit the daemon's package and release it, which is exactly the review-gated code
path ADR-0028 removed the *need* for.

Lifting the boundary is not just "add CRUD". Four things have to hold at once,
and each of them is a place where an obvious implementation goes wrong.

A definition an operator is still writing is usually not a valid one. If the
only place text can live is the executable store, then either half-finished work
cannot be saved at all, or invalid documents enter the store — and the second
option means a bad paste can take out the catalog, the launch form, or daemon
startup, since startup validates what it loads.

Two browser tabs are the normal case, not an edge case. The obvious version
comparison is the content revision, which ADR-0028 already computes. It is the
wrong one: a revision is a digest of *normalized semantics*, so two tabs whose
YAML differs only in comments produce the same revision, and the second save
would silently discard the first tab's work while every check passed.

What a workflow name means becomes mutable while the daemon runs. The current
catalog is a process-local dictionary, which is safe only because packaged
definitions cannot change mid-process. Once they can, a cached name is a
time-of-check-to-time-of-use bug with a launch on the other end: an entry can be
archived, or saved with a different revision, between the moment a preview is
reviewed and the moment a task is accepted.

And built-ins are now sharing a namespace with operator work. A package that
adds a built-in whose name an operator already used has to resolve that somehow,
and both obvious answers are bad: overwriting destroys work nobody asked to
lose, and refusing to start leaves the operator with no UI in which to rename it.

## Decision

Split the mutable part from the durable part, and give each its own table.

`workflow_revisions` stays exactly as ADR-0028 defined it: append-only,
content-addressed, never updated and never deleted. A new `workflow_library`
holds one entry per workflow name, carrying the mutable facts — the raw draft
text, an optional *current* revision, an archive flag, and an edit version.
Nothing in the library deletes a revision, and no library state is ever consulted
to resolve a task's own pinned revision.

**A draft is inert text.** Save-draft accepts any UTF-8 within the existing 1 MiB
document limit — empty, invalid, or unsafe-looking alike — and nothing parses it.
Only an explicit executable save validates, and it validates the exact text it
was handed rather than trusting an earlier validation response. A failed
executable save leaves the entry's current revision untouched, so a broken edit
can never take a launchable workflow away, and a broken draft can never be a
reason the daemon fails to start.

**The edit version is not the content revision.** Every successful mutation
advances a per-entry integer, including a comment-only draft save, and every
mutation of an existing entry submits the version it was loaded at. The
comparison, the retention, and the selection happen inside one write reservation.
A stale submission changes nothing and comes back with the entry's actual
version; there is no force, and no automatic merge.

**Selection and launch share a transactional boundary.** The process-local
catalog is removed. `resolve_launch` reads the library through the connection it
was given, which during acceptance is the connection holding the write
reservation — so an archive or an executable save committing alongside cannot
land between the check and the pin. An archived, draft-only, or unreadable entry
refuses the launch and says which; it never resolves to some other revision.
ADR-0028's fingerprint is unchanged: it already covers the resolved revision, so
an executable edit invalidates a reviewed preview while a draft edit does not.

**Built-ins are read-only packaged examples.** They are library entries with no
stored draft — their text is in the package — synchronized at startup to whatever
the running package ships. Editing or archiving one is refused; duplication is
the customization path. A name the package no longer ships keeps its entry and
its retained history and stops being launchable, rather than being deleted along
with an old task's readable procedure. A collision with an existing custom entry
is reported and the custom entry is left alone: the operator keeps their work and
keeps a running daemon in which to rename it.

Clients see the library the way they see every other registry (ADR-0004): a
snapshot plus one full-entry upsert per committed mutation, ordered by the edit
version, from which the launch catalog is derived rather than published
separately. Because the catalog can now change under an open socket, the main
WebSocket subscribes before it reads its snapshot, and forwards from that
overlap window only the deltas a client can order for itself — today, the
library's. An entry's edit version makes re-delivering one the snapshot already
holds a no-op, so nothing is lost and nothing regresses. Every other delta is
unversioned: one published before the snapshot read and delivered after it would
move a client backwards, which is worse than the missed update it replaces,
because the snapshot is already newer than all of them. Those are dropped,
exactly as the pre-subscription gap dropped them.

Authoring adds no authority. The loader is the same closed grammar ADR-0028
fixed — no code, no template engine, no plugin loader, no path or URL the daemon
will open — and validating or importing a document executes nothing. Import is
the browser reading a local file into the editor, followed by the ordinary draft
and save operations. Saving a workflow grants no review, signing, push, or PR
authority.

## Consequences

An operator can add a procedure without a daemon release or a restart, which is
the outcome ADR-0028 was building toward and ADR-0018 deferred. The library is
global and its names are permanent: renaming means creating a separate entry,
and an archived name stays reserved, because the tasks that ran under it are
still filed under that name.

There are now two versions attached to one workflow, and they answer different
questions. The content revision says what would execute; the edit version says
who edited last. Anything comparing the wrong one has a bug that only shows up
under concurrency, which is why they are named differently everywhere they
appear — in the schema, on the wire, and in the UI.

Editing is genuinely append-only underneath. Every executable save that changes
semantics retains a new document, and nothing collects them: a workflow edited
weekly accumulates revisions forever. That is the price of an old task staying
explainable, and it is a small one — a definition is kilobytes, and re-saving
semantically identical YAML reuses the existing revision rather than adding one.

The failure modes an operator meets are now per-entry rather than global. One
damaged current revision makes one workflow visibly unlaunchable with a reason
and a repair path, while everything else keeps working. What it costs is that
"the library" is no longer a thing that is simply valid: every consumer has to
handle an entry that exists but cannot run.

YAML was the whole authoring surface when this decision was taken, and it was
deliberately the first one: the step-card builder is a separate change over this
same library, and building it first would have meant designing a visual editor
against a store that did not exist yet. That builder now exists. It edits the
same drafts, through the same draft, validate, and executable-save operations,
and it adds one stateless conversion between the text this library stores and
the data a form edits — no second store, no second validator, and no authority
this decision did not already grant. The ownership, concurrency, and per-entry
failure rules above are unchanged by it; what changed is that YAML is now one of
two ways to write a definition rather than the only one.

## Alternatives considered

### Keep one table and let executable definitions be mutable

Simplest, and it destroys the property ADR-0028 exists for. A task pins a
revision; if the row behind that revision can be edited, the pin means nothing
and an accepted run's procedure can change under it. Retaining a copy at
acceptance instead just moves the same problem to a second store with a weaker
guarantee.

### Compare content revisions instead of an edit version

Reuses machinery already there, and silently loses updates. Comments and
formatting do not change a normalized document's digest, so two tabs editing
prose in the same definition would both pass a revision check and the later save
would overwrite the earlier one. Concurrency safety has to be about edits, not
about meaning.

### Watch a directory of YAML files

Familiar, and it introduces a second authority nobody can transactionally reason
about. A filesystem write has no version to compare, no reservation to share with
acceptance, and no way to refuse a stale edit — so a file changing between
preview and acceptance is unobservable rather than refused. It also hands
anything that can write that directory the ability to change what launches.

### Keep an in-memory merged catalog over the database

Fast reads, and exactly the time-of-check-to-time-of-use race this decision
exists to close. A cache is a claim that the name's meaning has not changed since
it was read, which is precisely what an editable library cannot promise. Caching
by *content identity* stays, because a revision genuinely cannot change.

### Let a validation response authorize a later save

Would save one parse. It would also mean the daemon executes a document it
verified at a different time than the one it was handed, which is the same class
of mistake as trusting a client-supplied identity. Validation stays informative,
and the executable save re-validates its own input.
