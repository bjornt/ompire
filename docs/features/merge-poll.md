# Merge polling

## Overview

After a task ships, Ompire tracks its pull request until it resolves, and uses
that state to decide when cleanup becomes available.

The purpose is a grace period. A shipped task's clone and container stay on
disk while the pull request is open, so the operator can still go back to the
workspace if review turns up something. Cleanup unlocks only once the work has
actually landed — or been closed.

## States and behavior

### The watcher

A background watcher runs every `pr_poll_interval` seconds. On each tick it
queries every non-archived task that has a `pr_url` and whose pull request is
not yet terminal — never polled, or `open` — using
`gh pr view <pr_url> --json state,mergedAt`.

The first poll runs **immediately at daemon startup**, so a merge that landed
while the daemon was down is picked up without operator action.

GitHub's `OPEN`, `MERGED`, and `CLOSED` are recorded lowercase as `open`,
`merged`, and `closed`. A `merged` or `closed` pull request is never polled
again.

### Cleanup is never automatic

The daemon does not delete a shipped task's clone on its own, ever. Cleanup
remains an explicit operator action through the cleanup endpoint.

| Pull-request state | Cleanup surface |
|---|---|
| null or `open` | "awaiting merge · cleanup deferred", no action offered |
| `merged` | Cleanup offered behind explicit confirmation |
| `closed` | Same, labeled closed-unmerged so the operator knows the work did not land |
| task archived | Cleaned-up state, no action |

Confirmation names the clone path, and the container when one is recorded.

Labeling `closed` distinctly matters: a closed-unmerged pull request is the
case where the operator most likely still wants the workspace.

## Failures and recovery

A failed poll — non-zero exit, unparseable output, timeout — is logged and
retried on the next tick.

It never changes task state, never skips the remaining tasks, and never
crashes the watcher. A forge that is briefly unreachable must not be able to
make Ompire forget what it knew.

## Configuration

| Key | Default | Effect |
|---|---|---|
| `pr_poll_interval` | `60` | Seconds between ticks; must be positive |
| `gh_command` | `["gh"]` | The forge CLI |

## Interfaces

Polling writes `pr_state` and `pr_merged_at` to the task row and broadcasts
`task_updated` with the change. These are durable task fields, unlike most
ship state.

The Tasks view's Shipped section and the Ship Flow view's Cleanup step both
render from them.
