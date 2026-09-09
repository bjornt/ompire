# Crash recovery

## Overview

Closing the browser or restarting the daemon must not erase the meaning of a
run. Ompire persists enough to resume live tasks, and reconciles the ones it
cannot resume into an honest `failed` rather than leaving them ambiguous.

The rule throughout: **recover what can be recovered, fail loudly what
cannot, and never guess.**

## States and behavior

### Session identity capture

After a session agent's ready handshake **on a fresh spawn**, the daemon
captures the identity of the agent session being written — backed by the
host-mounted agent home — and persists it on that session's registry row,
keyed `(task, session name)`.

Capture is best-effort. If the identity cannot be read, the daemon logs it and
leaves the persisted identity null without failing the step or the agent.
Losing the ability to resume is worse than a failed step, but it is not worth
failing a working agent over.

A resumed agent does not re-capture. It already carries the identity it was
resumed with.

### Startup recovery

For every non-archived task whose spawn completed and whose container is still
present, the daemon resumes each recorded session by starting the agent with
`--resume` against that session's recorded identity, inside the task's
container, and re-establishes session tracking.

Each session is resumed under **its own recorded applied policy** — what that
session's child last verifiably ran — not the task's first step's policy and
not today's profiles
([ADR-0027](../../adr/0027-hand-off-model-policy-between-turns.md)). Two steps
sharing a session can pin different bindings, so the task document alone cannot
answer the question; the session's `applied_policy_json` can.

A session with no recorded policy is **not** resumed. Choosing one for a
conversation already in progress is the guess per-consumer pinning exists to
avoid, so the session is left alone with the reason reported, its workspace and
history intact, and the engine spawns it fresh if the run needs it. The one
exception is a step interrupted before its prompt went out: its accepted
binding is the decision the run is about to make anyway.

A resumed session's record is re-committed as verified once the process is
actually running under it — a policy an upgrade derived stops being a
derivation the moment a child has been put on it.

A resumed agent is **not** re-prompted with the task's stored prompt. Whether
and what to re-deliver is the workflow engine's per-step decision — see
[restart recovery](../../use/reference/workflow-engine.md#restart-recovery).
Recovery re-drives the attempt that was already open rather than appending
another, so a restart never costs a step its declared visit bound and re-binds
no evidence — an attempt keeps the records it froze when it opened. A run
stopped on an [uncertainty
pause](../../use/reference/workflow-engine.md#uncertainty-pauses) is re-armed
exactly as persisted, with no prompt and no automatic retry.

A run waiting at a **gate** is re-armed as the same question: the stored
snapshot is re-broadcast, not re-rendered from a history that has since grown.
An *answered* gate is never re-armed. Because a decision and the successor it
authorized commit in one transaction, a restart finds either the untouched
question or the attempt the answer already opened — never a decision to make
twice, and never one that was made and lost
([ADR-0030](../../adr/0030-commit-human-decisions-before-advancing.md)).

A recovered session presents as `starting` while its agent is being resumed
and lands `idle` once ready. The in-flight turn is lost; the session is not.

A task with a present container but no recorded sessions is recovered with
zero resumes. That is not an error: sessions are spawned lazily, and a
command-only workflow may never create one.

A task whose **pinned workflow definition cannot be resolved** is skipped
before anything else happens — before any session is resumed and before any
step is chosen. That covers a revision that is absent, a stored document that
fails its integrity check, and one written for a format this daemon does not
implement, which usually means a downgrade
([ADR-0028](../../adr/0028-retain-declarative-workflow-revisions.md)). A
definition that cannot be read cannot say where the run is, so nothing is
resumed, no prompt is sent, and nothing is published. The skip is per task: one
damaged row does not stop the fan-out or hide anything from the snapshot.

Only sessions the pinned definition declares are resumed. The retired `judge`
session on an older task is left alone — nothing will prompt it again — while
its transcript and its last applied policy stay on record.

A task carrying no confirmed launch configuration is skipped for the same
reason, one layer down. These are tasks accepted before launch inputs were
pinned to the task
([ADR-0026](../../adr/0026-resolve-launch-inputs-once-and-pin-them-to-the-task.md)):
resuming one would need a model policy nobody recorded, and the project's
settings today are not evidence of what it ran under. Skipping keeps the run
at its position with its sessions, workspace and history untouched, and task
detail offers the operator a confirmation. Confirming pins the inputs and
enables an explicit Continue, which runs the same per-task recovery routine a
restart would. It governs future turns only — it is never a claim about what
an earlier turn used.

Recovery runs in the background. The daemon serves REST requests and WebSocket
snapshots while it proceeds, bounded by `recovery_concurrency` — deliberately
small, because each resume is a real container-side agent startup.

### Startup reconciliation

Every non-archived task that cannot be resumed becomes `failed` with a reason
naming the cause, **before the first WebSocket snapshot is served**. A client
never sees a task in a state the daemon is about to correct.

| Condition | Result |
|---|---|
| Spawn never completed — restarted mid-spawn | `failed`, restart-related reason |
| Spawn completed, container gone | `failed`, reason names the missing container |
| Spawn completed, no recorded session identity | **Not** failed — recovered instead |
| Resume attempted, agent cannot be started | That session becomes `failed` with a resume-failure reason |
| Already `failed` or `archived` | Left untouched |

Project setup is reconciled in the same pass, and for the same reason
([ADR-0022](../../adr/0022-create-or-adopt-base-checkouts-without-mutating-them.md)).
Every project left `cloning` is resolved against the filesystem:

| Condition | Result |
|---|---|
| A valid checkout with the expected fetch remote is at the destination | `ready` — the clone finished before the daemon stopped |
| Anything else | `failed`, "interrupted by daemon restart"; the staging tree is removed |

The clone is never restarted automatically; retry is the operator's decision.
This is possible because the setup job builds the clone at a staging sibling
and moves it onto the destination with one rename, so the destination is
either absent or complete — there is no partial tree to classify.

### Graceful shutdown

On shutdown the daemon terminates each live agent child with a signal that
lets it flush its session file, waits a bounded `shutdown_grace`, and forces a
kill only as a fallback.

Tasks are **not** marked `failed`. Their registry state stays `created` and
their workflow run state persists, so the next startup recovers them. A
shutdown-driven agent exit is not reported as a crash.

Without this, every restart would produce a screen of red failures for work
that was fine.

### Review recovery

The reviewer reads an isolated checkout of the task's candidate, not the task
clone, so a crash mid-review leaves nothing in the workspace to restore. The
disposable checkout is simply gone.

Review status and iteration history are durable rows (`reviews` and
`review_iterations`, behind `registry/reviews.py`) and are restored before the
first snapshot. The reviewer process is not: llmvet is never adopted or
relaunched, so a restored review carries no URL or port.

Telling an interrupted reviewer from a review that is legitimately still open
needs more than status, because a review whose comments went back to the agent
stays `open` while its process has already exited. The `reviews` row therefore
carries a `process_started_at` write-ahead marker, stamped before llmvet is
launched and cleared when the process is observed exiting:

| Persisted state | Startup behavior |
|---|---|
| `open`, marker set | Reviewer died with the daemon: append an `interrupted` iteration — bound to the candidate it was reading and the workflow attempt it was launched for — land the review `aborted`, clear the marker |
| `open`, marker clear | Comments are with the agent: restored untouched |
| Terminal (`approved`/`aborted`/`error`) | Restored untouched |
| No row | No review ran; nothing is inferred |

A recovered task's primary session presents as `starting`, `idle`, or
`failed`, never `reviewing`, and can start a fresh review that appends to the
same history. Operator-facing detail is in
[Review](../../use/reference/review.md#retention-and-restart).

A run that owns its review reads that recorded iteration when it re-drives the
waiting `review` step, and consumes it exactly once: an interrupted review is
an honest `interrupted` result the definition's own routes handle, and llmvet
is never relaunched on anyone's behalf. Because `restore_reviews` runs before
task classification, the verdict is already on record by the time the runner
looks for it.

### Delivery recovery

Delivery is journaled
([ADR-0032](../../adr/0032-bind-trusted-delivery-to-retained-candidates.md)).
Every action attempt records what it is about to write and commits that *before*
the effect runs, so an interrupted attempt is a row that says what was tried and
against which exact refs — not silence that looks identical to "never started".

Reconciliation runs in `ShipManager.restore()` before the parked-clone pass and
before task classification hands anything to session recovery, so a completed
signed result is never reset away before something has looked at it, and no
agent turn can be prompted into a task whose delivery is unresolved.

**Startup performs no signing, no push, and no forge write.** It observes, and
each action kind has its own evidence:

| Attempt | How it is classified |
|---|---|
| `commit` | The attempt writes its signed result under its own protected ref before it can return, so the absence of that ref proves no signature landed. A result that exists is verified against the reviewed tree, range structure, count, and signer before it is adopted. |
| `push` | The exact destination ref is compared with the authorized head and the recorded pre-push value. Equal to the authorized head is a completed push; equal to the pre-push value proves non-execution; anything else is a conflict. A destination that cannot be read is unknown, not absent. |
| `pr` | The authorized body carries a deterministic correlation marker, looked for across every pull-request state — closed and merged included — with a bounded search that reports its own incompleteness. Exactly one verified match is adoptable. `gh pr create` is never replayed because a listing found nothing. |

An outcome that cannot be established becomes `needs_reconciliation` and blocks
the task: no dependent action runs, cleanup is refused, and the projection shows
the action, its expected target, the observed evidence, and why Ompire will not
continue. The operator resolves it explicitly with a recheck, a verified
adoption, a retry that only becomes available once non-execution is proven, or
an abandonment that leaves an unknown effect on record as still unknown.

Authorized work that simply did not finish is *not* resumed on the operator's
behalf. The delivery moves to `blocked`, and a fresh preview and confirmation
decide whether the rest still applies. A draft interrupted mid-turn becomes an
explicit retryable interruption rather than a new agent turn.

### Workflow-owned delivery

When the run itself performs the actions
([ADR-0033](../../adr/0033-scope-trusted-delivery-authority-to-the-workflow-run.md)),
each attempt also names the delivery step that asked for it, and the runner's
recovery has exactly two honest moves once `ShipManager.restore()` has
classified the journal:

| Recorded state | What the run does |
|---|---|
| The action succeeded | **Adopt it.** The step finishes with that recorded result and the run continues. This closes the crash window between the journal write and the step transition, where re-running the step would sign or push a second time. |
| Anything else | **Wait.** The same attempt stays open as a *continuation*, keeping its grant and its journal context, until the operator confirms the remaining work against a fresh preview. |

The continuation is deliberately not `retry_paused_step`. A retry opens a *new*
attempt, which is right for work that can simply be done again; a privileged
action's attempt owns a write-ahead intent and possibly a partially observed
effect, so `resume_paused_attempt` returns the same row to `running` and the
per-attempt uniqueness that prevents a second signature still applies. An
attempt whose non-execution was *proven* is excluded from that uniqueness, so a
continuation after one opens a fresh attempt and both stay on record.

The crash window after a gate answer commits but before any effect runs lands
in the same place: the grant is real, no effect exists, and the run waits for a
confirmation rather than performing something nobody has seen a preview of.

### Durable result recovery

A result capture is either committed whole or not at all
([ADR-0034](../../adr/0034-retain-durable-task-results-outside-the-workspace.md)).
The retained bytes, the manifest, and the `ready` state land in one transaction,
so there is no state in which a revision is complete with some of its files.

`ResultManager.restore()` runs before any result command is accepted and before
the first snapshot is served. It turns every row still marked `capturing` into a
visible interrupted failure, with a reason saying the daemon restarted and that
nothing was retained.

**It re-reads no workspace.** The files that capture was reading may have been
edited or deleted since, and the clone may be gone entirely. Retaining today's
bytes under yesterday's request id would silently substitute content for the
content the operator asked about — which is exactly what the request-id replay
contract exists to prevent. A retry is an explicit new capture with a new id.

A capture cancelled before its supervising job ever ran never reaches its own
interruption handler, so shutdown reconciles those the same way rather than
leaving a row that looks in flight.

Committed results need no recovery at all. A `ready` revision, an acceptance,
and a completed purge are durable facts; a lost response to any of them is
recovered by reading the task's result history. Purge is either committed or
not, and its expectations mean a retried purge can never remove a different
revision. A storage failure leaves an older retained result untouched and never
marks incomplete bytes accepted.

Integrity is checked at the read boundary rather than at startup: a revision
whose bytes no longer match its manifest is classified unavailable when
something tries to read it, keeping its acceptance and its history, and is never
reconstructed from the workspace.

### Legacy parked clones

A clone parked by an older Ompire's in-clone review or signing still carries
`refs/ompire/review-orig` or `refs/ompire/ship-orig`. Startup answers one of
three ways for each, and the distinction matters:

| Outcome | Meaning |
|---|---|
| `absent` | There is no legacy ref. Nothing to do. |
| `restored` | The clone verifiably came back to its parked head; only then is the marker removed. |
| `unsafe` | A ref survives that could not be honoured. The marker is **kept**, and the task is blocked with a reason. |

A boolean would conflate "no legacy ref" with "a legacy ref Ompire could not
honour", and only the second is a reason to stop working on a task. An `unsafe`
outcome blocks that task alone: its workspace is not trustworthy, so review,
drafting, delivery and cleanup all refuse and say why, while every other task is
unaffected.

New deliveries never park the clone — signing happens in the candidate's own
repository — and reviews never park it either.

## What is not recovered

| State | Behavior after restart |
|---|---|
| Session status | Rebuilt by recovery, not replayed |
| Reviewer process, its URL and port | Discarded; the review's history and its candidate binding are restored, and the task clone was never modified |
| A delivery's in-flight coordination | Discarded; the journal, its authorization, and every action attempt are restored and reconciled |
| Attention entries | Rebuilt from recovered session status |
| A task with no confirmed launch configuration | Skipped, not failed; run position, sessions and workspace are kept until the operator confirms |
| An unfinished result capture | Failed as interrupted, retaining nothing; the workspace is never re-read to complete it |

The durable boundary is still narrower than [`VISION.md`](../../VISION.md)
calls for. Review history, delivery authorization, intent and outcomes, and now
captured result bytes with their manifests and decisions sit inside it; full
commit lineage and transcript retention do not, so
[ADR-0016](../../adr/0016-persist-authority-bearing-task-history-and-provenance.md)
remains proposed.

## Configuration

| Key | Default | Effect |
|---|---|---|
| `recovery_concurrency` | `4` | Concurrent session resumes at startup |
| `shutdown_grace` | `10.0` | Seconds before a forced kill on shutdown |
