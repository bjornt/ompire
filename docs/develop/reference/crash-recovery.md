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

A resumed agent is **not** re-prompted with the task's stored prompt. Whether
and what to re-deliver is the workflow engine's per-step decision — see
[restart recovery](../../use/reference/workflow-engine.md#restart-recovery).

A recovered session presents as `starting` while its agent is being resumed
and lands `idle` once ready. The in-flight turn is lost; the session is not.

A task with a present container but no recorded sessions is recovered with
zero resumes. That is not an error: sessions are spawned lazily, and a
command-only workflow may never create one.

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
