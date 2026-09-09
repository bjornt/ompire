# ADR 0034: Retain durable task results outside the workspace

- Status: Accepted
- Date: 2026-09-08

Extended by [ADR-0035](0035-refuse-to-publish-handoff-destinations.md), which
gives a retained revision a reference-protected lifetime: a revision pinned by a
consumer task cannot be purged, and the reference is released only by that
consumer's own explicit task purge.

Advances the artifact-retention slice of
[ADR-0016](0016-persist-authority-bearing-task-history-and-provenance.md);
that ADR stays `Proposed`, because transcript retention and commit lineage
remain undone. Preserves
[ADR-0006](0006-give-every-task-a-separate-clone-and-workshop.md)'s isolation
and [ADR-0011](0011-keep-review-and-publishing-authority-outside-agent-sandbox.md)'s
trusted boundary. Leaves [ADR-0032](0032-bind-trusted-delivery-to-retained-candidates.md)'s
temporary candidate store and
[ADR-0026](0026-resolve-launch-inputs-once-and-pin-them-to-the-task.md)'s pinned
launch inputs unchanged.

## Context

A task's output had exactly one durable form: a signed commit, a pushed branch,
or a pull request. Everything else lived in the clone until cleanup deleted it.

That works for a task that ends in code. It fails for the ones that end in
understanding. A task asked to investigate a bug, draft an epic, or write a
change proposal produces Markdown that is worth keeping and is not worth
committing to the mainline. Under the old model an operator had three
unappealing options: publish planning files to the repository, leave the task
un-cleaned forever so its clone survived, or copy files out by hand and lose
every trace of where they came from.

The workspace is deliberately a bad place to keep them. It is agent-writable, it
is deleted by cleanup, and its contents change under any hand that touches the
container. "The files are still in the clone" is not retention; it is an absence
of deletion so far.

`workflows.read_outcome`'s small JSON envelope was not an answer either. Its
`artifacts` map carries workflow-defined *values* — a boolean, a summary line —
inside `.ompire/`, inside the disposable clone. It describes what a step
concluded. It does not retain bytes, and a description of a file is not a file.

Four things had to be decided together, because deciding any one of them alone
produces a system that loses data.

**Where the bytes live**, given that they must outlive the workspace.

**What "this result" names**, given that an operator reviewing a result and
deciding to keep it must be deciding about content that cannot subsequently
change under them.

**What that decision authorizes**, given that Ompire's other approval — review
approval — authorizes publication, and conflating the two would let reading a
plan grant permission to ship code.

**When retained bytes may be destroyed**, given that cleanup already destroys a
workspace and an operator would reasonably expect it to leave a kept result
alone.

## Decision

**Result bytes are retained transactionally in the existing SQLite database,
outside the workspace and inaccessible to its sandbox.** A capture is one row in
`task_results` carrying an immutable manifest, plus one row per file in
`task_result_files` carrying the exact bytes. The payload and the `ready` state
commit in a single transaction, so no partial bundle is ever observable and
restart recovery never has to adopt half-copied files. Bounds are fixed in code
— 128 files, 1 MiB per file, 8 MiB per bundle, UTF-8 `.md`/`.txt`/`.json`/
`.yaml`/`.yml` only — not configurable, because they are what makes
transactional storage the right shape.

**Capture reads the workspace through descriptor-relative, no-follow
traversal, and refuses anything it cannot vouch for.** Every path component is
opened `O_NOFOLLOW` relative to the previous directory's descriptor; type, link
count, device, size, and inode identity are checked on the descriptor that was
actually opened and again after the read. A resolved string path followed by an
ordinary `open` is not a trusted boundary: between resolving and opening, any
component can become a symlink. Recognizable credential material and invalid
encoding are *refusals*, never redactions — storing altered bytes under a
checksum an operator will later trust is worse than storing nothing.

**A revision is identified by the hash of its complete manifest, and acceptance
names that hash.** Every decision — accept, purge — carries the identity the
operator was looking at, and a mismatch is refused rather than retargeted. New
edits produce a new revision; acceptance never floats to the latest files. A
separate content identity over the file set alone lets identical bytes be
*recognized* without merging two captures' distinct provenance.

**Provenance is recorded honestly, and gaps are named.** A manual capture is the
operator's action: `capture_actor` is `operator`, and the producing run, step,
and session are `unknown`. The run's most recent step is evidence that something
executed, never evidence that it wrote a particular file. Git values are labelled
as capture-time observations, distinct from the launch base that was recorded at
acceptance; where neither is available, the manifest lists the gap.

**Acceptance is retention, not authority.** It advances no workflow, answers no
review, and makes nothing publishable. The Results panel is deliberately separate
from Review and Ship flow, and its own label says so. Nothing a captured file
declares grants any authority: a valid manifest and matching checksums prove
identity, not correctness and not permission.

**Cleanup and purge are different actions.** Cleanup destroys the workspace and
retains every result; purge destroys one revision's bytes and is never reachable
from cleanup. Purge requires the manifest identity, the task's result version,
and an explicit acknowledgement, and has no force variant. It is logical removal
that leaves a tombstone — identity, manifest, provenance, acceptance, purge
actor and time — so the record of what existed survives the bytes. Task purge
refuses while any complete revision or in-flight capture remains, and that
refusal is decided before any of the task's history is deleted.

**Capture shares the existing workspace guard, and cleanup now holds it.**
Capture takes the guard as a `HOST` owner, so it excludes review, drafting and
delivery exactly as they exclude it. Cleanup previously only *checked* the guard
and then ran its teardown unowned; it now holds ownership across teardown, so a
capture cannot be admitted into a clone that is being deleted.

## Consequences

Exploration becomes a complete outcome. A task can produce something worth
keeping with no commit, no push, and no pull request, and the result survives
restart, workspace cleanup, and the task's own archival.

The database grows with retained results, and SQLite does not return freed pages
to the filesystem on purge. The retained payload is bounded per capture but
unbounded in total, because nothing expires: there is no automatic eviction, no
disk-pressure collection, and no expiry of unaccepted revisions. An operator who
wants space back purges revisions explicitly.

Purge is logical. It removes rows; it makes no claim about database free pages,
backups, or copies already downloaded. A forensic-erase requirement would need a
different storage decision, not a flag on this one.

The supported set is small on purpose. Binary artifacts, images, archives,
arbitrary extensions and larger bundles are all out, and admitting them would
mean revisiting this storage decision rather than quietly lifting the bounds.

Credential detection is bounded and cannot recognize every secret. It rejects
recognizable material — private-key blocks, GitHub tokens, authorization header
values, credential-bearing URLs, the daemon's own token — and a downloaded result
remains sensitive, untrusted data that a human still has to read.

The guard's new cleanup ownership means a busy capture refuses cleanup. That is
a visible, short-lived conflict rather than a race, and it is the price of not
reading a directory while it is being removed.

## Alternatives considered

### Keep the workspace alive instead

Defer cleanup until the operator no longer needs the files. This retains
nothing: it postpones deletion of a mutable, agent-writable directory. The files
can still change, the container still holds resources, and there is still no
answer to "which exact bytes did I decide to keep?".

### Commit results to a branch

Let the task commit its planning files and push them. This is the behavior the
epic exists to avoid. It puts coordination files into repository history, makes
the useful outcome depend on signing and forge availability, and turns "I read
this and want to keep it" into a publication.

### A filesystem blob store beside the database

Write bytes to owner-private files and keep metadata in SQLite. At the bundle
sizes this admits, it buys nothing and costs a two-resource commit: a finalize
that can half-succeed, orphan collection for bytes whose row never landed, and a
purge journal for rows whose bytes outlived them. Transactional BLOBs make the
capture, the manifest, and the `ready` state one commit. If the size bounds ever
rise materially, this becomes the right answer and this ADR should be revisited.

### Reuse the delivery candidate store

Delivery already retains content in owner-private bare repositories
([ADR-0032](0032-bind-trusted-delivery-to-retained-candidates.md)). But a
candidate is temporary operation evidence with a deliberately short lifecycle,
and it is a *Git* object store — capturing into one would reintroduce the commit
that this whole capability exists to avoid needing.

### Extend the outcome JSON envelope

Let a step declare files in its `artifacts` map. The envelope is the agent's own
statement about what it concluded, written inside the clone by the sandbox. Bytes
retained on that basis would be described by the party they are being retained
from, and would still live in the directory cleanup deletes.

### Let acceptance answer the workflow

Treat accepting a result as satisfying a gate or approving the work. It would
make the Results panel a second, weaker approval surface for publication, and an
operator saying "this plan is worth keeping" would be saying something about code
they never read.
