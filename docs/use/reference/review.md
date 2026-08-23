# Review

## Overview

Review runs a real review tool against the **host side** of a task's clone,
driven by the daemon. The agent being reviewed does not run it, does not see
its output before the daemon does, and cannot influence the verdict.

The review is the check that stands between agent output and publishing. An
agent that could grade its own work would make it ceremonial.

## Using review

`POST /api/tasks/{id}/review` opens a review for a task whose **primary
session** is `idle` with a live agent and no review already open. Other
sessions of the task neither gate nor block it.

The daemon fetches the clone, runs the reset dance, and launches the
configured llmvet command with `-no-open -port <n>` as a supervised
subprocess with the clone as working directory.

The review is recorded as `open` with its `http://127.0.0.1:<n>` URL and
`review_started` is broadcast. The subprocess then runs in the background —
the request does not block on the operator's browser session.

`POST /api/tasks/{id}/review/cancel` terminates an open review's process
(interrupt first, kill as fallback), restores the clone, and records the
review aborted.

## States and behavior

### The reset dance

Before launching the reviewer the daemon saves the clone's current `HEAD` as
the Git ref `refs/ompire/review-orig`, then runs `git reset --mixed` to the
merge-base of `origin/<base>` and `HEAD`.

Two properties matter:

- **The working tree is never modified.** `--mixed` moves `HEAD` and the index
  but leaves file contents byte-for-byte intact.
- **The full task delta becomes visible.** The reviewer sees everything from
  the merge-base, whether or not the agent committed along the way. Reviewing
  only the uncommitted remainder would silently hide checkpoint commits.

When the reviewer exits, the daemon restores the clone with
`git reset --mixed refs/ompire/review-orig` and deletes the ref.

### Crash safety

The ref is the recovery artifact. On startup, any non-archived task whose
clone still carries `refs/ompire/review-orig` is restored — reset to the ref,
ref deleted — before the daemon serves its first snapshot.

A crash mid-review therefore never leaves a clone with a detached or parked
`HEAD`.

### Outcomes

The outcome is interpreted from the **process**, never from the agent:

| Exit | stdout | Outcome |
|---|---|---|
| `0` | empty or whitespace | approved |
| `0` | non-empty | comments submitted |
| `130` | any | aborted |
| anything else | any | error, with captured stderr |

For comments, the raw stdout *is* the review prompt. A comment count is
derived best-effort for display only — the count is cosmetic, the text is
authoritative.

Each outcome is recorded as a review iteration and drives the `reviewing`
transition on the primary session.

### Comment loop-back

When an iteration reports comments and the primary session still has a live
agent, the daemon sends the raw stdout to that agent as a prompt over RPC. The
agent addresses the comments in its own session, moving from `reviewing` to
`working` through normal frame handling, and returns to `idle` when the turn
ends — ready for a fresh review.

Re-triggering review records a further iteration in the same review's history,
so the loop is visible rather than being a sequence of unrelated reviews.

## Failures and recovery

| Condition | Response |
|---|---|
| Unknown task | `404` |
| Primary session not `idle`, or no live agent | `409`, no process launched |
| A review is already open | `409`, no second process launched |
| Cancel with no open review | `409` |
| Comments arrive but the primary session has no live agent | Review recorded `error` naming the missing agent; the session is left unchanged |

Review state is held **in memory**. It does not survive a daemon restart — a
recovered task's primary session presents as `idle` rather than `reviewing`,
and the operator re-triggers. The clone's Git state is restored from the
durable ref regardless.

Cleanup discards the review entry and terminates any open reviewer process.

## Configuration

| Key | Effect |
|---|---|
| `llmvet_command` | The review command; must be non-empty |
| `review_port_range` | Range probed for a free port, default `[7180, 7280]`. Probed with an ephemeral bind so concurrent reviews do not collide. |

## Interfaces

| Method | Path |
|---|---|
| `POST` | `/api/tasks/{id}/review` |
| `POST` | `/api/tasks/{id}/review/cancel` |

| Event | Payload |
|---|---|
| `review_started` | `{task_id, url, port}` |
| `review_iteration` | `{task_id, iteration}` |
| `review_finished` | `{task_id, status}` |

The snapshot carries a `reviews` map from task id to `{status, url,
iterations}` for every task with a live or completed review that has not been
cleaned up.
