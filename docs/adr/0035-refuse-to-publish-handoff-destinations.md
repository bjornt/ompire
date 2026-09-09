# ADR 0035: Refuse to publish handoff destinations

- Status: Accepted
- Date: 2026-09-09

Extends [ADR-0026](0026-resolve-launch-inputs-once-and-pin-them-to-the-task.md)
with exact artifact and target-source pinning, and
[ADR-0034](0034-retain-durable-task-results-outside-the-workspace.md) with a
reference-protected result lifetime. Constrains the candidate boundary of
[ADR-0032](0032-bind-trusted-delivery-to-retained-candidates.md) and the run
authority of
[ADR-0033](0033-scope-trusted-delivery-authority-to-the-workflow-run.md), which
continue to own immutable publication evidence and who may authorize an effect.
Advances the artifact-to-consumer slice of
[ADR-0016](0016-persist-authority-bearing-task-history-and-provenance.md); that
record stays `Proposed` for its transcript and lineage gaps. Preserves
[ADR-0006](0006-give-every-task-a-separate-clone-and-workshop.md)'s isolation
and its existing shared-object caveat, and
[ADR-0011](0011-keep-review-and-publishing-authority-outside-agent-sandbox.md)'s
credential boundary.

## Context

Retained results made exploration a durable outcome. Handing one to a *second*
task is what makes it useful: an accepted epic or change proposal is worth
having because some later task implements it.

That handoff creates a problem the earlier records do not solve. The recipient
is an ordinary code task. It reviews, it commits, it pushes, it opens pull
requests — and the plan it was given is sitting in its clone as ordinary files.
The existing publisher stages the whole delta with `git add --all` against a
scratch index. Nothing in that path knows the difference between a source file
the task wrote and a planning document Ompire installed for it to read.

So the files would ride along. Not through malice or a prompt-injection story:
through the ordinary, correct behavior of a publisher that stages everything.

Three weaker answers were available and each fails somewhere specific.

*Clone-local excludes* keep the files out of `git status` and out of
`git add --all`. They are also a file inside the agent-writable clone. An agent
can edit `.git/info/exclude`, `git add -f` a path, or simply
`git commit` one directly. An exclude improves the common case; it proves
nothing about the proposed tree.

*Checking the final tree only* misses retained history. A handoff file added in
one agent checkpoint and deleted before HEAD leaves a clean final tree — and, in
retain mode, a published commit anyone can read the file out of.

*Path patterns* — protect `epics/`, `changes/`, `PLAN.md` — protect the wrong
thing. A repository may legitimately track those paths, and a handoff may
legitimately live somewhere else. The protected set is a property of *this
task's accepted inputs*, not of a naming convention.

Two further questions had to be settled alongside it, because answering either
one alone leaves the feature incoherent.

**Which bytes does a consumer get?** A pointer to "that task's result" floats:
a successor capture would change what a launch means after it was reviewed. And
a launch resolved against "the base branch" is resolved against whatever the
branch points at when the clone lands, which is not what the operator compared
the plan with.

**When may retained bytes be purged?** A consumer that materialized a bundle
does not keep a live link to it — it has its own copies. But its *record* says
what it ran with, and an operator inspecting that record later has to be able to
read those files. A purge that silently removed them would turn honest
provenance into a dangling reference.

## Decision

**Publication protection is a destination-path contract derived from the task's
own accepted inputs, enforced against the actual proposed Git result.**

- The protected set is exactly the destinations of the task's pinned
  attachments, read out of its immutable execution-inputs document. An ordinary
  task's set is empty, so its candidate identity and its delivery are unchanged.
  A stored attachment whose classification cannot be read is refused, never
  decoded as "no protection".
- Candidate capture checks the scratch-index tree *and* the base tree,
  mode-neutrally, before anything is retained or reviewed. Review therefore
  refuses contamination before llmvet starts; it cannot be hidden behind a
  successful content review.
- Retain mode additionally inspects every commit tree that would be published.
  Squash does not, because unpublished checkpoints are not published — a clean
  squash of a contaminated history is a legitimate delivery.
- Every trusted admission asks again, against the objects that exist then:
  signing verifies the real signed range in the candidate store, and push and
  pull-request admission re-check the range in the clone. A continuation,
  restart reconciliation, or direct service call inherits no earlier answer.
- Checks use literal repository paths and recurse, so a protected file later
  replaced by a directory is caught through its descendants, and a filename
  containing Git pathspec metacharacters stays a path rather than becoming a
  pattern.
- Clone-local excludes are still written, by one shared code path, for both
  spawn and delivery. They are a convenience. Nothing treats them as evidence.
- **Ompire never deletes a file or rewrites history to make a delivery
  publishable.** A refusal names the paths and, for retained history, the
  offending commit. Correction is the operator's, and it requires a fresh review
  and a new delivery approval.
- There is no declassification, override, or force. Deleting a working copy,
  editing ignore rules, or accepting another result does not lift the
  restriction.

**A launch pins exact revisions and an exact target commit.**

- An attachment names `(producer task, result id, manifest id)`. The manifest id
  is what makes it a revision rather than a pointer: a successor capture does not
  match it, so a stale selection is refused instead of silently upgraded.
- An attachment launch resolves and pins the exact commit the clone will be
  built from, and branches from that commit. Preview reads it; acceptance
  re-checks it; the spawn pipeline verifies the ref still resolves there and
  fails visibly if it moved. SQLite cannot freeze a Git ref, and nothing here
  pretends it did.
- The producer's recorded `capture_merge_base` is compared with that target, per
  attachment, and labelled as the capture-time observation it is — never as the
  producing task's launch commit. A different or unknown base requires an
  explicit acknowledgement bound by the preview token to this exact selection
  and target. A match is not evidence the plan is correct.
- Destinations are the manifest's original paths. There is no remapping and no
  partial selection, so a conflict is resolved by choosing differently, never by
  overwriting, skipping, or merging.
- Bytes are installed before the workshop starts, into the recipient's own
  clone, through descriptor-relative `O_NOFOLLOW` traversal with exclusive
  creation, and the complete installed set is re-hashed against the accepted
  manifest before the pipeline reports success. A failure leaves a failed,
  inspectable task — never a runnable one with partial inputs.

**A pinned revision cannot be purged while a consumer exists.**

- Accepting a launch and reserving its result references is one transaction, so
  a purge racing it either loses the reservation and is refused, or wins and
  leaves no consumer behind.
- A purge refusal names the consumer tasks. References survive consumer failure,
  cleanup, and archival, and are released only when that consumer's own task
  record is explicitly purged, in the transaction that deletes it. A refused
  consumer purge releases nothing.

## Consequences

An operator can hand an accepted plan to a fresh isolated task and ship that
task's code, with the plan provably out of both the proposed tree and the
retained commit history. The recipient's task record keeps saying what it ran
with after the producer is cleaned up.

Contamination is a refusal, not a repair, so an operator who committed a handoff
file has manual work to do. That is deliberate: the alternative is Ompire
deleting files or rewriting commits in a workspace it does not own.

Retention grows. A failed or archived consumer still pins its inputs, and the
only release is an explicit task purge. The blockers are shown exactly, with the
consumer tasks named; there is no automatic eviction.

The protected set binds into the candidate identity for tasks that have one, so
such a task's reviewed candidate is bound to the policy it was reviewed under.
Ordinary tasks keep byte-identical identities, so nothing already reviewed goes
stale.

This is a *destination-path* contract. It is not semantic data-loss prevention:
text an agent deliberately copies or renames into an unrelated source file is
ordinary content that ordinary code review has to catch. Nor does it forbid
agent-local checkpoint commits, or claim to — that would need an enforceable
sandbox capability, not a policy at the delivery boundary.

## Alternatives considered

### Ignore-only protection

Write the handoff destinations into the clone's excludes and trust them. Cheap,
and it does keep the files out of ordinary staging.

Rejected because the exclude file lives inside the agent-writable clone. It can
be edited, bypassed with `git add -f`, or ignored entirely by a direct commit.
Trusting it would make the guarantee depend on the sandbox behaving, which is
exactly the assumption the trusted boundary exists to avoid. Excludes are kept —
as a convenience that improves the common case and proves nothing.

### Silently filtering or rewriting contaminated Git content

Drop the protected paths from the staged tree, or rewrite the commits that carry
them, and publish what remains.

Rejected because it publishes something the operator never reviewed. A filtered
tree is not the tree that was approved, and a rewritten range is not the history
the operator wrote. It also risks destroying real work: "remove this path" is a
guess about intent whenever the file was deliberately placed. Refusing keeps the
operator in control of their own repository, at the cost of manual correction.

### Making handoff destinations publishable by default

Let the recipient publish planning files like anything else, and rely on review
to catch them.

Rejected because it inverts the reviewed-input boundary. These files entered the
workspace as *daemon-installed inputs*, not as the task's work; publishing them
would mean an operator's decision to hand over a plan silently became a decision
to commit it upstream. Review is a content judgement made by a model and a
person under time pressure; it is not the place to rest a structural guarantee
that the daemon can make exactly.
