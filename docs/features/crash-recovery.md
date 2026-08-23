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
[restart recovery](workflow-engine.md#restart-recovery).

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

Review state itself is transient and does not survive a restart. A recovered
task's primary session presents as `idle`, not `reviewing`, and the operator
re-triggers.

## What is not recovered

| State | Behavior after restart |
|---|---|
| Session status | Rebuilt by recovery, not replayed |
| Review status and iterations | Discarded; the clone's Git state is still restored |
| Ship progress other than `pr_url` | Discarded |
| Attention entries | Rebuilt from recovered session status |

The durable boundary is narrower than [`VISION.md`](../../VISION.md) calls
for. Widening it is an open architectural decision rather than a settled
design.

## Configuration

| Key | Default | Effect |
|---|---|---|
| `recovery_concurrency` | `4` | Concurrent session resumes at startup |
| `shutdown_grace` | `10.0` | Seconds before a forced kill on shutdown |
