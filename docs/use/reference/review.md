# Review

## Overview

Review runs a real review tool against the **host side** of a task's clone,
driven by the daemon. The agent being reviewed does not run it, does not see
its output before the daemon does, and cannot influence the verdict.

The review is the check that stands between agent output and publishing. An
agent that could grade its own work would make it ceremonial.

## Using review

### Operator interface

The task-detail Review panel is the normal operator interface. It always uses
the workflow's primary session, even when another session tab is selected.
When that session is idle with a live agent, use **Start review**. The action
locks until daemon state reports the result; a command failure is shown inline
and can be retried only after the panel returns to an eligible observed state.

An open review is labelled **Review open**, keeps the full llmvet URL as its
external action, and offers **Cancel review**. If an iteration submits
comments, the panel labels it **Comments submitted** and says that the primary
agent is addressing them. Once that agent becomes idle, **Start another
review** is available. Terminal labels are **Approved**, **Aborted**, and
**Error**. Approved task detail also links directly to its Ship flow.

Tasks cards and the Ship flow use the same labels and ordered iteration
formatting, but task detail owns the complete start, reopen, cancel, retry,
and ship handoff. Iteration rows show their recorded time, optional comment
count, and expandable captured stderr.

### REST interface

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

The ref is the recovery artifact for the clone. On startup, any non-archived
task whose clone still carries `refs/ompire/review-orig` is restored — reset
to the ref, ref deleted — before the daemon serves its first snapshot.

A crash mid-review therefore never leaves a clone with a detached or parked
`HEAD`. The review's own status and history are restored separately, from the
database — see [Retention and restart](#retention-and-restart).

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

### Retention and restart

Review status and the ordered iteration history are **durable**. They survive
a graceful shutdown, a crash, and a browser reconnect, and are restored before
the daemon serves its first snapshot. An approval earned before a restart
still stands afterwards and still opens the Ship flow; a multi-pass comment
history comes back in order, and starting another review appends to it rather
than beginning a new one.

The reviewer process is not durable, and Ompire never relaunches llmvet on
your behalf. A restored review therefore reports no URL or port, and no
external review link is offered for it.

A graceful shutdown cancels an open review normally, so it lands **Aborted**
before the daemon exits. A review that is **still open at the next startup** —
the daemon crashed or was killed — is closed honestly instead: an
`Interrupted by daemon restart` iteration is appended and the
review lands `Aborted`, which is why the panel says a restart interrupted the
reviewer rather than that you cancelled it. The recovered primary session
presents as `starting`, `idle`, or `failed` per normal session recovery —
never `reviewing` — and once it is idle you can start a fresh review.

A review left open because its comments went back to the agent is *not* a
restart casualty: its reviewer had already exited, so it comes back exactly as
it was, still labelled **Comments submitted**.

A task that never ran a review has no review entry. Ompire does not infer one
from Git state or from an existing pull request.

Cleanup terminates any open reviewer process, records that review **Aborted**,
and **keeps** the review history: a shipped, cleaned-up task retains the
evidence explaining why it was allowed to publish, and never shows as still
under review. Purging the task deletes that history along with its other
records.

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

The snapshot carries a `reviews` map from task id to `{status, url, port,
iterations}` for every task with a review, including cleaned-up tasks. `url`
and `port` are `null` whenever no reviewer process is live — always the case
after a restart.

An iteration's `outcome` is one of `approved`, `comments`, `aborted`, `error`,
or `interrupted`. The last is restart-only and always accompanies an `aborted`
review; the review's own `status` remains one of `open`, `approved`,
`aborted`, or `error`.
