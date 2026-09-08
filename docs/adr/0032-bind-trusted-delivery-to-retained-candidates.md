# ADR 0032: Bind trusted delivery to retained candidates and write-ahead action intent

- Status: Accepted
- Date: 2026-09-07

Supersedes the mutable live-task review mechanics of
[ADR-0011](0011-keep-review-and-publishing-authority-outside-agent-sandbox.md)
and carries its trusted boundary forward unchanged. Advances the delivery slice
of [ADR-0016](0016-persist-authority-bearing-task-history-and-provenance.md).
Preserves [ADR-0026](0026-resolve-launch-inputs-once-and-pin-them-to-the-task.md)'s
accepted-input boundary.

## Context

Publishing was one compound operation. `commit_and_ship` signed, pushed, and
opened a pull request in sequence, and the only durable trace it left was
`tasks.pr_url` on success. Everything else — that an operator had authorized
anything, which content was signed, whether a push had reached the remote — lived
in a dictionary in the daemon's memory.

Four problems follow from that, and they compound.

**An approval did not identify anything.** Review reset the task clone to its
merge-base so llmvet could read the whole delta, then restored it. The reviewer
therefore read the task's *live* working tree, and the approval that came out
said only "this task was approved" — a status, plus the old HEAD the restore ref
happened to hold. An agent that kept working, an untracked file that appeared, a
checkpoint that was amended: none of it invalidated the approval, and signing
later ran `git add --all` against whatever the workspace held at that moment. The
gap between what was reviewed and what was signed was real, unbounded, and
invisible.

**A lost response was indistinguishable from no effect.** If the daemon died
between `git push` returning and the state update, or between `gh pr create`
writing and its reply arriving, nothing on disk said an attempt had been made.
The only two available behaviors were both wrong: assume failure and retry — a
second signature, a force-push over an unknown head, a duplicate pull request —
or assume success and lose the work. Startup did the first, unconditionally
restoring `refs/ompire/ship-orig` before anything looked at what it was
overwriting.

**Every ending was a pull request.** An operator who wanted a signed local commit,
or a pushed branch to hand to someone else, had no way to ask for one. The
button said "Sign & commit" and opened a pull request, which meant the
confirmation named one effect and performed three.

**Safety lived in the REST layer.** The route checked the review, ran the GitHub
preflight, probed GPG, and only then called the manager. A direct service call
skipped all of it, and the "one ship at a time" check was an in-memory dictionary
lookup that a restart erased.

## Decision

Make the content a first-class retained object, make each privileged effect its
own journaled action, and let the operator choose how far a delivery goes.

**A candidate is what gets published.** Capturing one resolves the task's whole
publishable delta once: the pinned base branch, the base commit, the HEAD it was
captured at, the complete candidate tree — tracked edits, deletions, and
non-ignored untracked files together — and, for retain, the ordered source
commits with their trees and messages. Its identity is a SHA-256 over exactly
that normalized data. Not the rendered preview, not the agent's draft, not a
timestamp: an unchanged workspace captures to the same identity, and any change
to what would be published captures to a different one. Capture uses a
daemon-private index, so the task's own index and working tree are untouched, and
it runs with the clone's hooks disabled and refuses a clone that configures
content filters — the clone is agent-writable, and a filter or hook would
otherwise run task-authored code on the host as the operator.

**The candidate's objects live outside the task.** Each one gets an owner-private
bare repository under the daemon's data directory, holding only the base, the
original head, and the candidate tree. Not a second writable worktree of the
task's `.git`, and never a copy of clone-local configuration or credentials. This
is temporary operation evidence with its own lifecycle, not an artifact store and
not a second source checkout.

**Review grades a candidate, in isolation.** The reset dance is gone. llmvet runs
in a private checkout built from the candidate — HEAD and index at the base, the
working tree holding the candidate tree — which exposes the same full delta and
takes the task clone out of the loop entirely. The approval names the candidate
it graded, so "is this still the content that was approved?" is a comparison
rather than a hope. An agent that keeps working no longer changes what is under
review; it changes the task's current candidate, which makes the approval
visibly unusable instead of silently covering different code.

**Delivery is three admitted actions and a selectable ending.** `commit`, `push`,
and `pr` are separate operations with their own guards. An ending — local signed
commit, pushed branch, or pull request — authorizes a prefix of that sequence and
nothing beyond it. A preview resolves one requested ending read-only and returns
a fingerprint over its exact inputs; a confirmation carries that fingerprint back
and is refused if anything moved. A later push of an existing signed result, or a
later pull request for an existing pushed one, is separately previewed and
confirmed, and appends authority rather than rewriting what was already granted.

**Signing happens against the candidate, not the workspace.** The signed result
is built inside the candidate's own repository from the retained tree — squash as
one commit on the base, retain by replaying the captured range and rewriting only
identity and signature. Trees, count, parents and `%GF` signer are verified
before anything leaves that repository. Only then is the result installed into
the task clone, under a compare-and-swap against the HEAD the candidate was
captured at, and the index is synchronized to the signed tree only after
re-checking what the workspace holds. A workspace that moved on keeps its files
and blocks further publication; it is never reset over.

**Intent is journaled before the effect, and the result before the next one.**
Every action attempt records what it is about to do — the destination ref and the
object id, the observed pre-push head, the correlation marker that will be in the
pull-request body — and commits that before anything runs. A verified outcome and
the eligibility it grants commit together. An attempt reaches `failed` only when
non-execution or a verified rollback is established; anything less certain becomes
`needs_reconciliation`, which is neither success nor failure.

**Recovery observes the specific operation.** A signing attempt writes its result
under its own protected ref before it can return, so the absence of that ref is
proof no signature landed. A push compares the exact destination ref against the
authorized head and the recorded pre-push value, and leases with that recorded
value — never a tracking ref refreshed behind the operator's back. A pull request
is looked for by its correlation marker across every state, including closed and
merged, with a bounded search that reports its own incompleteness. Startup
performs no signing, no push and no forge write: it observes, records what it can
prove, and blocks the task with the evidence attached when it cannot. Remaining
work needs an explicit continuation afterwards.

**Admission moved into the operation.** Review binding, accepted target, mode,
credential availability, replay and exclusivity are checked inside the delivery
service, which re-reads its own contract from the durable record rather than
trusting a caller-supplied snapshot. REST parses and authenticates. There is no
no-review path and no tokenless path. One task-scoped ownership guard covers
review, drafting, delivery, agent turns, workflow steps and cleanup: it refuses a
new writer rather than interrupting the current one, and it is held while an
effect is executing or unresolved. It is mechanical workspace safety only —
what a *workflow* is permitted to publish is a separate concern that will admit
at this same boundary.

The trusted boundary itself is unchanged. Review and publishing still run on the
host, outside the agent sandbox; the signing key is still selected by the control
plane and verified afterwards; credentials still never enter the workshop; the
publishing identity is untouched, and this decision takes no position on
[ADR-0017](0017-use-dedicated-bot-as-default-publishing-identity.md).

## Consequences

An operator can stop after a local signed commit or a pushed branch, and that is
a successful delivery rather than a failed pull request. The UI, the pull-request
poller, and cleanup all had to learn that a completed delivery may have no PR.

An interrupted delivery is now a state an operator has to resolve. Before, a
crash produced a clean-looking task with no publication; now it can produce a
task that is explicitly blocked with an unresolved effect, offering a recheck, a
verified adoption, a retry only once non-execution is proven, and an
abandonment that leaves an unknown effect on record as still unknown. That is
more work than "just try again", and it is the whole point: the alternative was
signing twice or force-pushing over something nobody looked at.

Approvals expire against content rather than against time. An agent that keeps
working after a review invalidates it, and the operator has to review again. That
is a real cost in the loop where an agent is nudged after approval, and it is
what makes the approval mean anything.

Reviews recorded before this decision have no candidate binding. They stay
readable as history and are never backfilled from today's workspace, so a task
with an old approval needs a fresh review to deliver. Existing pull requests and
their poll state are untouched, and no successful delivery attempt is invented
for them: such a task shows a known publication with no delivery journal behind
it, which is exactly what is true.

Local storage grows. Each reviewed candidate holds a bare repository with the
objects it needs — not a workspace copy, and shared with nothing — and terminal
ones are removed once no active or unresolved work still needs them as evidence.
A task under repeated review accumulates a few of these until it finishes.

The clone is no longer parked during review, which removes a whole class of
"the daemon died and my working tree is at the merge-base" failures. Clones
parked by an older daemon are still recognized, and are restored only when the
restoration verifies — an unrestorable clone keeps its ref rather than losing the
evidence.

This advances only the delivery portion of ADR-0016's durability goal. Full
checkpoint-to-mainline lineage and transcript retention remain incomplete, so
that record stays `Proposed`.

## Alternatives considered

### Keep reviewing the live workspace and compare HEAD before signing

The smallest change, and it does not work. Most of a task's delta is routinely
uncommitted, so HEAD is unchanged by exactly the edits that matter; an untracked
file added after approval would sail through. Comparing the full publishable
content is the only comparison that answers the question, and once it is being
computed, retaining it costs almost nothing and buys isolation for free.

### Sign the live workspace, as before, but check identity first

Narrows the window without closing it. An agent turn, a file watcher, or an
editor save between the check and `git add --all` puts unreviewed content into a
signed commit. Signing from a retained tree removes the window instead of making
it small, and it is what lets a signature be verified against something other
than the thing that produced it.

### One generic durable job engine for every privileged effect

Attractive, and it makes recovery worse. The interesting part of reconciliation
is not "retry with backoff" — it is that each effect has *different* evidence: a
protected ref for a signature, an exact remote ref for a push, a correlated
all-state query for a pull request. A generic engine either does not model that
and retries blindly, or models it per operation anyway and adds a scheduler
nobody needed. Ompire has three effects, and they are enumerable.

### A universal event log instead of typed rows

A single append-only stream is easy to write and hard to ask questions of. Every
consumer — the projection, the preview, the recovery pass, cleanup — would fold
the log to reconstruct state, and the invariant that matters ("one unresolved
delivery per task") would become a fold result rather than something a
transaction can reserve. Typed rows with a write reservation make the invariant
enforceable at the point of writing.

### Restore parked refs unconditionally at startup, as before

What the previous implementation did, and the reason a completed signed result
could be discarded before anything looked at it. Ref restoration is now
journal-aware and verified: only recorded temporary state that still matches its
operation's evidence is restored, and a legacy ref that cannot be restored safely
is kept along with a visible block, rather than a blanket reset that destroys
what it cannot explain.

### Let the workflow result choose the ending

Would fold delivery into the workflow vocabulary immediately. It also conflates
"the work reached this outcome" with "the operator authorized publishing this
far", which are different decisions made by different parties. Endings are
delivery selections here; exposing review and delivery as workflow steps is a
separate change over this same service boundary.
