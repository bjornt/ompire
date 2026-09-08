# Review

## Overview

Review runs a real review tool on the **host side**, driven by the daemon,
against a protected snapshot of the task's publishable work. The agent being
reviewed does not run it, does not see its output before the daemon does,
cannot influence the verdict, and — since the reviewer never reads the live
clone — cannot change what is being reviewed while it runs.

The review is the check that stands between agent output and publishing. An
agent that could grade its own work would make it ceremonial.

An approval **names the content it graded** (ADR-0032). That binding is what
delivery checks: an approval whose content has since changed stays on record as
history and cannot authorize publishing.

**Who starts a review depends on the task's workflow.** A workflow that
declares a `review` step starts the review when it reaches that step, records
the verdict as evidence, and routes on it — so what happens after a review is
written in the definition. A workflow that declares none is reviewed by the
operator, exactly as before. Task detail says which of the two you are looking
at.

## Using review

### Operator interface

The task-detail Review panel is the normal operator interface.

For a workflow that declares its own `review` step, the panel is a read-only
view of what the run is doing: it shows the open reviewer's link, the ordered
iterations, and each verdict. There is no **Start review** button, and the
panel says why — the run starts the review when it reaches the step that
declares one, and reviewing at another moment would grade content the run is
still changing.

For a workflow that declares none, the panel drives review. It always uses the
workflow's primary session, even when another session tab is selected. When
that session is idle with a live agent, use **Start review**. The action locks
until daemon state reports the result; a command failure is shown inline and
can be retried only after the panel returns to an eligible observed state.

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

`POST /api/tasks/{id}/review` opens a review by hand. It is refused with
`409 not-at-review` when the task's workflow declares its own review step and
the run is not at it — the run owns that decision. When the workflow declares
none, it requires the **primary session** to be `idle` with a live agent and no
review already open; other sessions of the task neither gate nor block it.

A review a *run* starts needs no live agent at all: it is a host-side operation
on the workspace, so a command-only workflow can review without inventing an
agent to hold the review open.

The daemon fetches the clone, captures the task's candidate, builds an isolated
checkout of it, and launches the configured llmvet command with
`-no-open -port <n>` as a supervised subprocess with that checkout as its
working directory.

The review is recorded as `open` with its `http://127.0.0.1:<n>` URL and
`review_started` is broadcast. The subprocess then runs in the background —
the request does not block on the operator's browser session.

`POST /api/tasks/{id}/review/cancel` terminates an open review's process
(interrupt first, kill as fallback), removes the isolated checkout, and records
the review aborted. The task clone needs no restoration — the reviewer never
wrote to it.

## States and behavior

### The candidate

Before launching the reviewer the daemon resolves what this task would publish:
the base branch it was accepted with, the base commit its delta is measured
from, the HEAD it was captured at, the complete candidate tree — committed
checkpoints, pending edits, deletions, and non-ignored new files together — and,
for retain delivery, the ordered source commits with their trees and messages.

The candidate's identity is a hash of exactly that normalized content. Capturing
an unchanged workspace twice yields the same identity; any change to what would
be published yields a different one. That is the whole approval binding.

Capture is read-only against the task: it uses a daemon-private index, so the
task's own index, working tree, and HEAD are untouched. It runs with the clone's
hooks disabled and refuses a clone that configures content filters or a hooks
path — the clone is agent-writable, and either would let task-authored code run
on the host as the operator.

The candidate's Git objects are copied into an owner-private repository under
the daemon's data directory, outside the workshop mount, so the task cannot
rewrite or garbage-collect what the review graded.

### What the reviewer sees

The reviewer's checkout has its HEAD and index at the candidate's base commit
and the candidate's tree on disk. `git status` and `git diff` therefore expose
the **full task delta** — exactly what the earlier in-clone reset exposed — and
nothing about the task's live state can change it mid-review.

Reviewing only the uncommitted remainder would silently hide checkpoint commits,
which is why the whole delta is the unit.

Starting a review is refused when the delta is empty, when another daemon-managed
writer owns the task's workspace, and when a previous privileged effect's outcome
is unresolved.

### Crash safety

The isolated checkout is disposable: a crash mid-review leaves the task clone
exactly as it was, with no parked or detached `HEAD` to restore. The review's own
status and history are restored from the database — see
[Retention and restart](#retention-and-restart).

Clones parked by an older Ompire's in-clone review are still recognized on
startup and restored, and the marker is removed only when the restoration
verifies.

### Outcomes

The outcome is interpreted from the **process**, never from the agent:

| Exit | stdout | Outcome |
|---|---|---|
| `0` | empty or whitespace | approved |
| `0` | non-empty | comments submitted |
| `130` | any | aborted |
| anything else | any | error, with captured stderr |

For comments, the raw stdout *is* the reviewer's report. It is retained whole
on the iteration, together with a state saying what was kept:

| `findings_state` | Meaning |
|---|---|
| `complete` | The whole report |
| `empty` | An approval with nothing to say |
| `truncated` | A report too large to retain whole |
| `unavailable` | No report could be captured |

A comment count is derived best-effort for display only — the count is
cosmetic, the text is authoritative, and a correction that runs automatically
should require `complete` rather than acting on a fragment.

Each outcome is recorded as a review iteration. Where the task's workflow
declares a review step, the iteration also names the attempt that asked for it,
so a verdict belongs to a question rather than to a task in general.

### Where comments go

**A workflow that declares review** routes them. The report is evidence, and
the definition's own edge carries it back to a working step — so the loop is
visible in the flow, spends that step's declared visit budget, and can be
routed around, bounded, or sent somewhere else entirely. Nothing prompts an
agent behind the run's back.

**A workflow that declares none** keeps the older behavior: when an iteration
reports comments and the primary session still has a live agent, the daemon
sends the raw stdout to that agent as a prompt over RPC. The agent addresses
them in its own session and returns to `idle`, ready for a fresh review. Those
definitions have no correction route of their own, so comments reaching nobody
would strand the task.

Either way, re-reviewing records a further iteration in the same review's
history, so the loop is visible rather than being a sequence of unrelated
reviews. Each round captures the corrected content and binds its own iteration
to it, so a second approval covers what actually changed.

Ownership of the workspace is released before any correction turn: the reviewer
is finished with it, and the turn is admitted on its own.

## Failures and recovery

| Condition | Response |
|---|---|
| Unknown task | `404` |
| Primary session not `idle`, or no live agent | `409`, no process launched |
| A review is already open | `409`, no second process launched |
| Cancel with no open review | `409` |
| The task's launch configuration is not confirmed yet | Refused; confirm the task's configuration first (see [States](states.md)) |
| Comments arrive but the primary session has no live agent | Review recorded `error` naming the missing agent; the session is left unchanged |
| Nothing to review — the task's content matches its base | `409` naming the empty delta |
| Another writer owns the workspace, or an unresolved delivery effect blocks it | `409` naming the holder or the effect |
| The clone configures content filters or a hooks path | `409` naming the settings Ompire refuses to capture under |

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

An approval recorded before content-bound review carries no candidate. It is
preserved exactly as it was and shown as historical evidence; it is never
backfilled from today's workspace, and delivering that task needs a fresh
review.

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
| `review_started` | `{task_id, url, port, candidate_id}` |
| `review_iteration` | `{task_id, iteration}` |
| `review_finished` | `{task_id, status}` |

The snapshot carries a `reviews` map from task id to `{status, url, port,
candidate_id, iterations}` for every task with a review, including cleaned-up
tasks. `url` and `port` are `null` whenever no reviewer process is live — always
the case after a restart. Each iteration carries the `candidate_id` it graded,
`null` for history recorded before content binding.

An iteration's `outcome` is one of `approved`, `comments`, `aborted`, `error`,
or `interrupted`. The last is restart-only and always accompanies an `aborted`
review; the review's own `status` remains one of `open`, `approved`,
`aborted`, or `error`.
