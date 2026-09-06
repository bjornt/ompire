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

### Review and ship recovery

Review and ship protect their temporary Git state with durable refs —
`refs/ompire/review-orig` and `refs/ompire/ship-orig` — written before any
rewrite.

On startup, any non-archived task whose clone still carries a review ref is
restored before serving: reset to the ref, ref deleted. A crash mid-review
never leaves a detached or parked `HEAD`.

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
| `open`, marker set | Reviewer died with the daemon: append an `interrupted` iteration, land the review `aborted`, clear the marker |
| `open`, marker clear | Comments are with the agent: restored untouched |
| Terminal (`approved`/`aborted`/`error`) | Restored untouched |
| No row | No review ran; nothing is inferred |

A recovered task's primary session presents as `starting`, `idle`, or
`failed`, never `reviewing`, and can start a fresh review that appends to the
same history. Operator-facing detail is in
[Review](../../use/reference/review.md#retention-and-restart).

Ship progress other than `pr_url` remains transient.

## What is not recovered

| State | Behavior after restart |
|---|---|
| Session status | Rebuilt by recovery, not replayed |
| Reviewer process, its URL and port | Discarded; the review's history is restored, the clone's Git state too |
| Ship progress other than `pr_url` | Discarded |
| Attention entries | Rebuilt from recovered session status |
| A task with no confirmed launch configuration | Skipped, not failed; run position, sessions and workspace are kept until the operator confirms |

The durable boundary is still narrower than [`VISION.md`](../../VISION.md)
calls for. Review history now sits inside it; human decisions,
publishing-operation intent records, and commit lineage do not, so
[ADR-0016](../../adr/0016-persist-authority-bearing-task-history-and-provenance.md)
remains proposed.

## Configuration

| Key | Default | Effect |
|---|---|---|
| `recovery_concurrency` | `4` | Concurrent session resumes at startup |
| `shutdown_grace` | `10.0` | Seconds before a forced kill on shutdown |
