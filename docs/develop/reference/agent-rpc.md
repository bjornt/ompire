# Agent integration

## Overview

Each named session of a task is one supervised `omp --mode rpc-ui` child
process running inside the task's container, spoken to over stdio NDJSON.

The integration is deliberately shallow. The daemon validates only what
orchestration needs and passes everything else through untouched, so the
agent's frame vocabulary can grow without breaking Ompire.

## States and behavior

### Spawning

The child is spawned as an asyncio subprocess with PIPE stdio, via
`workshop exec` in the task's clone — argument list, never a shell — with the
configured agent environment injected and a stream limit of at least 4 MiB.

The large stream limit matters: agent frames routinely exceed the default
64 KiB line limit, and a truncated frame is an unrecoverable protocol error.

Session files are not disabled. They are the escape hatch and the substrate
crash recovery resumes from.

Sessions of the same task run concurrently as independent children sharing the
container and clone. Each is registered under its `(task, session)` key, and
one's exit never deregisters another.

When starting in resume mode, `--resume <session-id>` is appended so the child
continues the recorded session rather than starting fresh.

### Ready handshake

An agent counts as started only after a `ready` frame is read from stdout,
bounded by `agent_ready_timeout`. A timeout kills the child and fails the
start.

### Request correlation

Requests are NDJSON frames with daemon-generated unique ids. `response` frames
are correlated to pending requests by id. Prompt requests use the `message`
field.

Push events interleave freely on stdout and are never treated as responses —
strict request/response pairing would be wrong for this stream, since the
agent emits events whenever it likes.

A `response` reporting `success: false` fails the request with the frame's
error text.

### Opaque passthrough

The daemon validates with typed models only the interpreted subset:

| Frame | Used for |
|---|---|
| `ready` | Start handshake |
| `response` | Request correlation |
| `agent_start`, `agent_end` | Session status |
| `extension_ui_request` | Ask and approval classification |
| `tool_execution_start`, `tool_execution_end` | Ask-vs-approval classification |

Each model validates **only the fields the daemon acts on** and tolerates
unknown fields, so the rest of the payload passes through for rendering.

Every frame — interpreted ones included — is forwarded untouched to the
session's event channel. An unknown frame type reaches the channel
byte-faithfully with no validation applied.

Child stderr lines are wrapped as `agent_stderr` events on the same channel.
They are diagnostic gold on crashes.

### Event buffering

Each session's handle owns a ring buffer of `agent_ring_buffer_size` raw event
frames, replayed to a client connecting to that session's channel before live
events begin.

The buffer bounds memory and smooths reconnects. Events older than the buffer
are gone — the channel is a live view, not a transcript store. The agent's own
session files are the archive.

### Exit

The daemon observes every child exit, any cause and any code, publishes
`agent_exited` with the task id, session name, and exit code on the main
stream, and deregisters the agent.

**A mid-run exit is never auto-restarted or auto-resumed.** Resuming happens
only as part of daemon-startup recovery. An agent that died while the daemon
was healthy died for a reason the daemon does not understand, and restarting
it would hide that.

## Failures and recovery

| Condition | Result |
|---|---|
| Child exits before the handshake — missing credentials, for instance | Start fails with the child's captured stderr; no live agent registered |
| No `ready` frame within the timeout | Child killed, start fails with a timeout error |
| `response` reports failure | The request fails with the frame's error text |
| Stop on a session with no live agent | `409` |
| Stop for an unknown task or undeclared session name | `404` |

## Configuration

| Key | Default | Effect |
|---|---|---|
| `agent_env` | `{}` | Forwarded into the agent's command line. See [the trust boundary](../../use/explanation/trust-boundary.md) before using it. |
| `agent_ready_timeout` | `30` | Bound on the ready handshake |
| `agent_ring_buffer_size` | `1000` | Retained raw events per session |
| `shutdown_grace` | `10.0` | SIGTERM-to-SIGKILL grace on daemon shutdown |

## Interfaces

The daemon exposes no REST endpoint to start or prompt an agent. Starting
sessions and delivering prompts belong to the [workflow
engine](../../use/reference/workflow-engine.md).

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/tasks/{id}/sessions/{name}/agent/stop` | Terminate the child |

Other session-addressed endpoints are covered in [agent
interaction](../../use/reference/agent-interaction.md).
