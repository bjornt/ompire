# Workshop lifecycle

## Overview

Every task runs its agent inside its own container, launched during spawn and
removed during cleanup. The container is what makes an agent's actions
recoverable: it is disposable, and nothing in it is shared with another task
or with the operator's checkout.

## States and behavior

### Launch

The workshop step runs after the branch step succeeds. The configured
my-workshop command is invoked as a subprocess with an argument list — never
through a shell — with the clone directory as working directory, bounded by
`workshop_step_timeout`.

That timeout is deliberately much larger than the git-step timeout, because
launching a container includes SDK installation.

On success the daemon reads the workshop identity from `.workshop.lock` in the
clone and records it on the task.

### Existence

The daemon never persists live container status. "Does this container still
exist" is answered on demand, by invoking the workshop CLI in the task's clone
with a short timeout, whenever the answer is needed — task detail, cleanup.

| Result | Meaning |
|---|---|
| `present` | The container exists |
| `absent` | No container behind this clone |
| `unknown` | The tool is missing, errored, or timed out |

A status check never writes to the registry, and a tool failure degrades to
`unknown` rather than failing the request that asked.

This is the right trade for a fact that can change without the daemon's
involvement: a persisted status would be authoritative-looking and wrong.

### Removal

Cleanup of a task with a recorded workshop identity runs `workshop remove` in
the clone **before** deleting the clone directory.

An already-removed container is treated as success, so cleanup is idempotent.

Idempotence depends on distinguishing "no container behind this clone" from a
real failure, which is read from the tool's stderr. That marker set was
settled against a specific workshop version and is re-validated during
dogfooding — a changed message would turn a benign absence into an abort.

## Failures and recovery

| Condition | Result |
|---|---|
| Launch exits non-zero or exceeds its timeout | Pipeline stops, task `failed`, stderr stored |
| Launch exits zero but no non-empty `.workshop.lock` | Step treated as failed, error names the missing lock file |
| Status check fails or times out | Reported as `unknown`; the enclosing request still succeeds |
| `workshop remove` fails for any reason other than absence | Cleanup aborts, the clone directory is **not** deleted, and the task stays un-archived |

The removal-failure behavior is deliberate: deleting the clone while its
container still exists would orphan the container, leaving something running
that nothing knows how to remove.

Startup reconciliation treats a spawn-completed task whose container no longer
exists as `failed`, since the workspace can no longer be resumed.

## Configuration

| Key | Effect |
|---|---|
| `my_workshop_command` | The launch command; must be non-empty |
| `workshop_step_timeout` | Bound on the launch step |

## Interfaces

The workshop identity is a task field, populated after a successful launch and
null before it. Container status is not a registry field and appears only in
responses that derive it on demand.
