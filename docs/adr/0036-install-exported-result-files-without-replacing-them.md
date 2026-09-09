# ADR 0036: Install exported result files without replacing them

- Status: Accepted
- Date: 2026-09-09

Extends [ADR-0034](0034-retain-durable-task-results-outside-the-workspace.md)
with a second way a retained revision leaves the store, and with a *temporary*
export lifetime beside that record's permanent retention rules. Leaves
[ADR-0035](0035-refuse-to-publish-handoff-destinations.md) unchanged in
substance: task attachments still install full bundles at their original paths
into a disposable clone, under destinations delivery refuses to publish.
Advances the explicit-export slice of
[ADR-0016](0016-persist-authority-bearing-task-history-and-provenance.md); that
record stays `Proposed` for its transcript and lineage gaps. Preserves
[ADR-0006](0006-give-every-task-a-separate-clone-and-workshop.md)'s isolation —
ordinary task execution still never writes to a checkout — and
[ADR-0011](0011-keep-review-and-publishing-authority-outside-agent-sandbox.md)'s
trusted boundary.

## Context

A retained result could be read in Ompire, downloaded as a ZIP, or attached to
another task. The one thing an operator actually wanted next was often none of
those: put this accepted plan into my repository, so I can read it beside the
code and decide what to do with it.

Downloading and unpacking by hand does that, badly. The operator loses which
revision the files came from, learns nothing about what is already at those
paths until `unzip` has answered by overwriting, and gets no record that it
happened. It is a privileged filesystem write performed without review, by a
tool that does not know it is one.

Doing it inside the daemon means Ompire writing into a directory it does not
own. That is a boundary it crosses nowhere else. Capture reads a workspace.
Handoff writes a *disposable clone* Ompire created, where "this failed, throw it
away" is a complete recovery story. Delivery works through Git in a
repository whose history is designed to be inspected and reverted. A project
checkout is none of those: it holds the operator's uncommitted work, other
people's edits arrive through their editor, and there is no undo that Ompire
can offer without risking destroying something real.

Four questions had to be settled together.

**What happens when a destination is occupied.** This is the whole decision.
Every other question follows from it.

**What an approval names**, given that the checkout can change between the
moment an operator reads a preview and the moment anything is written.

**What a crash leaves behind**, given that a bundle of files cannot be
installed atomically and Ompire cannot roll back an operator's directory.

**How long an export holds the bytes it read**, given that a retained result can
be purged and a task record can be deleted.

## Decision

**Export creates missing files and does nothing else.** A destination that does
not exist is created. A destination that already holds exactly the approved
bytes is a no-op — untouched, including its permissions and its modification
time. Anything else is a *conflict*: different content, a directory where a file
belongs, a symlink, a special or multiply-linked file, an unreadable entry.

A conflict blocks the whole submission while the file is selected. The operator
resolves it by deselecting that file, choosing a different prefix, or fixing the
checkout themselves. There is no overwrite, no merge, no skip-and-continue, and
no force flag. A difference between two files is a fact; it is not permission to
replace one with the other, and Ompire has no way to know which of the two the
operator wants.

**Approval names one canonical observation, which the daemon recomputes.**
Preview reads the retained revision and the real checkout — root identity, every
relevant ancestor's identity, and each destination's absence or its regular-file
identity, size, mode, timestamps, and checksum — and hashes a canonical document
of exactly that. Confirmation re-derives the document from a fresh observation
and compares. A changed revision, selection, prefix, root, or relevant
destination yields a different digest and a refusal to export. The client cannot
supply a classification, and a destination outside the recomputed set cannot be
written. Preview itself creates nothing, not even a staging directory, and runs
no Git command: conflict classification is about working-tree bytes, and a
commit tree would answer a different question.

**Installation is atomic per file, through `renameat2(RENAME_NOREPLACE)`.**
Bytes are staged in a daemon-named `.ompire-export-<id>` directory under the
approved root, written `0600` with exclusive creation, verified against the
accepted manifest, and fsynced. Each is then moved into place with the kernel
performing the existence check and the move as one operation. `os.replace`
silently clobbers, and "stat, then rename" is exactly the race a create-only
guarantee has to survive — between the two calls, an editor can save a file.
A host without that primitive refuses before any destination is touched; a
less-safe fallback would be the same feature with the guarantee removed.

**A bundle is not atomic, and the record says so instead of pretending.** A
failure stops the remaining installations and leaves what was installed exactly
where it is, along with any directories that were created. Ompire does not
delete an operator's files to fake a rollback. Every export carries a durable
per-destination outcome — `created`, `already-identical`, `not-installed`,
`unknown` — and an operation state of `completed`, `incomplete`, or
`unresolved`.

**Recovery classifies; it never repeats.** Startup and an explicit recheck
re-observe the filesystem read-only. A staged file's device and inode found at
its destination establishes that the rename happened before the journal was
updated. Matching *bytes* establish nothing about who wrote them, and are
reported `unknown`. No destination is written, no effect is rolled back, and
nothing is retried: resuming an interrupted export is a new preview and a new
confirmation, under which delivered files are no-ops and differing ones are
conflicts. An operator can acknowledge an unresolved export closed; that records
that the uncertainty was read, and leaves every `unknown` outcome unknown.

**The root is reserved durably, by identity, and the reservation is temporary.**
Concurrent exports are serialized on the checkout's device and inode through a
partial unique index in SQLite — not the project name, so two registrations
aliasing one directory cannot both install into it, and not an in-memory lock,
so a restart does not drop it. Repointing a project's `checkout_path` is refused
while one of its exports is unfinished. None of this claims the directory cannot
be replaced on disk; that is detected separately, by comparing the root's
identity against the approval.

While an export is `running` or `unresolved` it blocks purge of the result's
bytes and of the task's record, naming itself. Once settled it releases both.
That asymmetry with [ADR-0035](0035-refuse-to-publish-handoff-destinations.md)
is deliberate: a consumer task's record permanently says what it ran with, so it
holds its inputs forever, while an export's product is a set of ordinary files
in the operator's checkout that already outlive everything Ompire retains.

**Export is not publication, and delivers no protection with the files.** No
commit, no branch, no push, no pull request, and no Git state of any kind is
touched. The copies are ordinary checkout files: unlike a task's handoff inputs,
nothing stops the operator committing them later, and the UI says so.

## Consequences

An accepted plan reaches the repository the operator actually works in, with the
exact revision, the destinations, and the outcome recorded, and with a guarantee
worth having: no file that was already there was changed.

Conflicts are manual work. An operator who has an older copy of a plan in their
checkout must delete or move it themselves before the new one can be exported.
That is the cost of never guessing which of two files was wanted, and it is
charged every time rather than only when the guess would have been wrong.

Partial outcomes are visible and are not repaired. An interrupted export can
leave some approved files installed, some not, and directories that no file
ended up in. Nothing cleans those up, because "remove this path from the
operator's repository" is exactly the action this record exists to refuse.

Uncertainty is a terminal state. An export whose effects cannot be established
stays `unresolved` until an operator rechecks or acknowledges it, and it holds
the retained bytes and the task record until then. There is no timeout that
would quietly convert not-knowing into a conclusion.

The feature is Linux-specific in practice. `renameat2` with `RENAME_NOREPLACE`
is required, and a filesystem that does not implement it refuses export rather
than degrading. Ompire already targets a local Linux daemon, so this narrows
nothing that was not already narrow.

The bounds are the retained-result bounds. Exports carry the same file count,
size, path, and supported-type limits, and the same credential recognizer — now
applied to *destination* content too, so a conflict against a checkout file
holding recognizable secrets is reported without showing it.

## Alternatives considered

### Preview, then overwrite what the operator approved

Classify conflicts, show the difference, and replace on confirmation.

Rejected because the approval cannot be made honest. Between reading the diff
and the write, an editor can save the file; the operator would have approved
replacing content that no longer exists, and the work that replaced it would be
gone with no copy anywhere. Making that safe needs either a lock on the
operator's editors, which is not available, or a backup of every replaced file,
which is a second content store with its own retention and purge rules. A
create-only rule needs neither and is a promise that stays true under a
concurrent writer.

### Roll back the whole export on failure

Delete the installed files and created directories when an export cannot finish,
so a bundle looks atomic.

Rejected because the rollback is itself a destructive write into the operator's
directory, performed at exactly the moment Ompire is least sure what is going
on. A file created by this export and then edited by the operator would be
deleted along with their edit. And it cannot be complete anyway — a crash
between the effect and its journal entry leaves nothing to roll back from.
Reporting per-file outcomes describes what happened; a rollback would add a
second set of effects to be uncertain about.

### Automatically retry an interrupted export at startup

Finish the approved plan when the daemon comes back.

Rejected because the approval is bound to an observation that is now old. The
checkout has been through a crash and whatever the operator did afterwards, and
re-running writes that nobody has reviewed against the current state is the
opposite of what the preview exists for. Recovery classifies, and resuming is a
new review — which is cheap, because the already-delivered files classify as
no-ops.

### Let export commit, or add ignore rules

Stage the exported files, commit them, or write them into `.gitignore` so they
cannot be committed by accident.

Rejected in both directions. Committing makes export a publication, which is the
thing this epic exists to avoid needing. Adding ignore rules writes a *second*
kind of file into the operator's repository, unasked, to enforce a policy they
never stated: an operator exporting a plan into their checkout may very well
intend to commit it. Export delivers files and says plainly what they are.

### Reuse `handoff.install_attachments`

The launch path already installs retained bytes through descriptor-relative,
no-follow traversal with exclusive creation.

Rejected as a shared implementation, though its rules are reused. Its failure
contract is "leave a failed clone for cleanup to delete", which is correct for a
disposable workspace and unacceptable for a checkout. It installs whole bundles
at original paths with no subset, no prefix, and no conflict classification —
properties that are load-bearing for attachments, since a partial or relocated
handoff would break the publication protection derived from those exact
destinations. Widening it to serve both would weaken the attachment contract to
fit the export one.

### A single generic "write files somewhere" service

One trusted primitive both handoff and export call, parameterized by policy.

Rejected because the two boundaries differ in what may be lost, not in
mechanism. The shared parts — path validation, no-follow traversal, exclusive
creation, post-write verification — are already shared as small functions. What
is left is precisely the policy, and a service whose policy parameter decides
whether an operator's work can be destroyed is not an abstraction worth having.
