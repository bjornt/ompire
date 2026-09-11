# Agent integration

## Overview

Each named session of a task is one supervised `omp --mode rpc-ui` child
process running inside the task's container, spoken to over stdio NDJSON.

The integration is deliberately shallow. The daemon validates only what
orchestration needs and passes everything else through untouched, so the
agent's frame vocabulary can grow without breaking Ompire.

## States and behavior

### Spawning

The child is started through the resource boundary's transport —
`isolation.start_sandbox_process` wraps the native argv in `workshop exec`
in the task's clone (argument list, never a shell) with PIPE stdio and a
stream limit of at least 4 MiB. The agent owner builds the *native* argv —
`omp --mode rpc-ui ...` with the accepted model flags — and adopts the
started process for the ready handshake and supervision; which transport
runs it is the resource boundary's decision, not the protocol's
([ADR-0039](../../adr/0039-own-workspace-resources-behind-isolation.md)).

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

### Model policy

Every child is started under the task's accepted model policy — there is no
"unset means omp's default", because inheriting the host's model settings is
what a globally reusable profile exists to prevent
([ADR-0026](../../adr/0026-resolve-launch-inputs-once-and-pin-them-to-the-task.md)).
The argv carries the active pair as `--model provider/model-id` and
`--thinking LEVEL`, plus all three auxiliary roles as
`--smol/--slow/--plan provider/model-id:LEVEL`, so omp's own subagent and
planning operations see the same policy the daemon resolved. Identifiers are
split at the *first* slash only, keeping provider-specific names that contain
further slashes intact.

After the ready handshake, and before any prompt or resume nudge, the daemon
confirms what the child is actually running:

- On a **resumed** process the pair is reasserted with `set_model` and
  `set_thinking_level`. A resumed child is restored from its session file, so
  `--model` on the argv is not by itself proof of the model in force.
- Either way the active model identity is read back and compared exactly. omp
  fuzzy-matches `--model`, so a typo or a retired id would otherwise quietly
  run a neighbouring model.

A child that cannot be put on the accepted model is killed rather than
prompted. Thinking is a policy, not a resolved value: omp may resolve `auto`
and `max` to a model-specific level, and the accepted policy and the observed
resolved level stay separately visible rather than one overwriting the other.

### Changing policy on a live session

Consumers of one session can pin different policies
([ADR-0027](../../adr/0027-hand-off-model-policy-between-turns.md)), and the
native contract is asymmetric: `set_model` and `set_thinking_level` exist,
while `--smol`, `--slow`, and `--plan` are start-time flags with no setter
(verified against omp v18.1.10). The supervisor therefore owns a **per-session
boundary** — per session, never per task, and never held across a turn, since a
turn can wait for an operator's answer — and inside it:

| Difference from what the session is running | Action |
|---|---|
| None | Keep the process; still reassert and read back the active pair, because a cached handle is not evidence |
| Active pair only | Keep the process; `set_model`, then `set_thinking_level`, then read back and compare exactly |
| Any auxiliary pair | Capture the native session id off the running child, stop it gracefully so container-side omp flushes its session file, and start a replacement with `--resume` and all four new pairs |

A transition is refused unless the child is at a turn boundary: `get_state`'s
`isStreaming`, `isCompacting`, and `queuedMessageCount`, plus any tracked
unanswered question, all mean the change fails through the ordinary
infrastructure path rather than aborting work.

A replacement is verified before it may be prompted: the resumed session id
must equal the captured one — omp opens a *new* session when the recorded id
names nothing, which would look like continuity and be a fresh context — and
the active pair is read back as usual. The verified policy is recorded durably
before the turn that depends on it. Any refusal, timeout, identity mismatch, or
failed record leaves no promptable handle: the candidate is stopped and the
consumer fails.

The replacement inherits the logical session: the `(task_id, session)` key, the
tracker entry, and the bounded event ring buffer carry over, and the retired
child is flagged so its exit is not published as a crash and cannot unregister
or fail its successor. Model choice is never part of session identity.

### Request correlation

Requests are NDJSON frames with daemon-generated unique ids. `response` frames
are correlated to pending requests by id. Prompt requests use the `message`
field.

Push events interleave freely on stdout and are never treated as responses —
strict request/response pairing would be wrong for this stream, since the
agent emits events whenever it likes.

A `response` reporting `success: false` fails the request with the frame's
error text.

### File mentions in a prompt

The `message` field is not opaque text to omp. It runs through omp's own
`@file` parser: `@relative/path` at a word boundary becomes a `fileMention`
carrying the file's content, resolved against the child's working directory —
the task's clone. `@` inside a word, such as an email address, is left as
prose. Verified against omp 17.4.0.

**An unresolvable mention is dropped silently.** The request still answers
`success: true`, no `fileMention` is produced, and nothing reports the missing
file. That is why the workflow engine resolves the operator's mentions against
the clone before delivering a prompt, and fails the step rather than sending
one omp would quietly strip — see [task spawn](../../use/reference/task-spawn.md#file-mentions).

`daemon/tests/test_omp_file_mentions.py` holds this contract against the real
binary, driving it through `AgentHandle` with a local capture endpoint in place
of a model provider.

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
only as part of daemon-startup recovery, or as the second half of a deliberate
policy handoff. An agent that died while the daemon was healthy died for a
reason the daemon does not understand, and restarting it would hide that.

An exit that *is* part of a handoff publishes no `agent_exited` and does not
fail the session: the replacement owns the session from that point. The
per-session event channel closes with code `4409` ("agent replaced") instead of
`1000`, so a connected client reconnects to the replacement and replays the
carried-over transcript rather than treating it as finished.

## Failures and recovery

| Condition | Result |
|---|---|
| Child exits before the handshake — missing credentials, for instance | Start fails with the child's captured stderr; no live agent registered |
| No `ready` frame within the timeout | Child killed, start fails with a timeout error |
| `response` reports failure | The request fails with the frame's error text |
| omp refuses the model or thinking level, reports no active model, or resolves to a different one | Start fails with a model-configuration error and the child is killed; no prompt is sent under substituted settings |
| A policy change is due while the session is streaming, compacting, holds queued messages, or has an unanswered question | Refused as a session-busy error; the turn in flight is untouched |
| A process must be replaced but omp does not name its native session | Refused; a fresh conversation is never substituted for a resume |
| A replacement resumes a different native session, or its applied policy cannot be recorded | The candidate is stopped and the consumer fails; the previous durable record and conversation stand |
| Stop on a session with no live agent | `409` |
| Stop for an unknown task or undeclared session name | `404` |

## Configuration

| Key | Default | Effect |
|---|---|---|
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
