# Workshop lifecycle

## Overview

Every task runs its agent inside its own container, launched during spawn and
removed during cleanup. The container is what makes an agent's actions
recoverable: it is disposable, and nothing in it is shared with another task
or with the operator's checkout.

The container and the clone under it are owned by the resource boundary
(`isolation/`): `launch_workshop` and `remove_workshop` take resolved values
and an opaque owner id, so a workspace can be prepared, launched, inspected,
and destroyed with no task row behind it. `application/spawn.py` and
`application/cleanup.py` decide when a task's accepted work uses those
operations
([ADR-0039](../../adr/0039-own-workspace-resources-behind-isolation.md)).

## States and behavior

### Launch

The workshop step runs after the branch step succeeds. The configured
my-workshop command is invoked as a subprocess with an argument list — never
through a shell — with the clone directory as working directory, bounded by
`workshop_step_timeout`.

The invocation and the lock-file contract belong to `isolation.launch_workshop`;
the preparation coordinator emits the step events and records the identity on
the task only after the launcher leaves a non-empty lock.

That timeout is deliberately much larger than the git-step timeout, because
launching a container includes SDK installation.

On success the daemon reads the workshop identity from `.workshop.lock` in the
clone and records it on the task.

#### Additions source

The task's accepted Workshop additions source — `project` or `global` — is
made to apply by staging, because my-workshop resolves additions itself and
its rule is local-first with no source-selecting flag: a `workshop.my.yaml`
beside the resolved `workshop.yaml` always wins over the operator's
`~/.config/my-workshop/my.yaml`. Leaving the argv alone would therefore
silently fall back to whichever source happened to exist, which
[ADR-0026](../../adr/0026-resolve-launch-inputs-once-and-pin-them-to-the-task.md)
forbids.

So the daemon puts the *selected* source's content at the clone's local
additions path before the launcher runs, invokes the launcher unchanged, and
restores the clone's original file — or its original absence — whether the
launcher succeeded or failed, before any agent starts. A selected source that
is absent is staged as an explicitly empty file, so the launcher's local-first
rule cannot reach the other source; that is reported as no additions, never as
a successful application of the other one.

The original content and the fact that staging is in progress live in the
daemon's data directory, never in the clone: a backup inside the task's
working tree would be readable by the agent and would surface in status,
diffs, and reviews. Unfinished staging is reconciled at startup before agents
run. The operator's own files are never modified — the registered checkout is
untouched and the global additions file is only ever read.

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

### Removal

Cleanup of a task with a recorded workshop identity runs `workshop remove` in
the clone **before** deleting the clone directory. The teardown is
`isolation.destroy_workspace`: it independently validates that the resolved
clone is strictly below the configured task root — the root itself and
anything outside it cannot be deleted, with no task lookup involved — then
removes the container, then the clone.

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
| The selected additions source is unreadable or escapes its expected location | Workspace setup fails before an agent starts; the clone's original additions file is restored |
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

The resource entry points, all on the declared `isolation` public surface:
`launch_workshop` (stages additions around the launcher, reports the source
disclosure, returns the identity), `workshop_status`, `remove_workshop`,
`destroy_workspace` (confined, container-first teardown), and
`recover_pending` (startup restoration of interrupted additions staging —
the journal's filenames, backup layout, and integer owner field are
unchanged, so records written before the package move are restored the same
way). The additions source/note disclosure is reported to the coordinator
before the launcher runs and published as the `workshop_additions` event,
not as a pipeline step.
