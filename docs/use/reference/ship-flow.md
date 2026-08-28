# Ship flow

## Overview

Shipping turns a task's work into a signed commit, a pushed branch, and a pull
request. The primary agent can draft the publication text; the daemon does
everything else with host-side credentials the agent never sees.

The flow has four steps — Review, Commit, Push + PR, Cleanup — surfaced as a
stepper in the Ship Flow view. Opening a task-specific Ship flow prepares an
eligible agent draft automatically; it never signs, pushes, or creates a pull
request without the operator's explicit action.

## Ship flow index

The global **Ship flow** navigation item opens `/ship`, a chooser for the
existing task-specific publishing workflows. It sends no command and does not
relax review, signing, Git, forge, or cleanup preconditions.

The chooser waits for the current daemon snapshot before deciding what is
available. Until then it shows loading rather than an empty or missing-task
state. Once the snapshot arrives, it shows non-archived tasks with an approved
review, recorded ship state, or pull request in **Ready or in progress**, then
the remaining pull-request records — including archived records — in **Recently
shipped**. A task appears once in its most relevant group; each group is
ordered by the task's `updated_at` value, newest first.

Each row links to `/ship/<task-id>` and names the next stage from daemon state:

| Label | Meaning |
|---|---|
| Review | The recorded handoff has not reached approved review. |
| Draft | Review is approved but publication text is not ready. |
| Sign | A draft is ready, or the signed commit is in progress. |
| Push / PR | A signed commit exists or publication is in progress. |
| Wait for merge | A pull request exists but has not resolved. |
| Cleanup | A merged or closed pull request can have its workspace removed. |
| Cleanup complete | An archived task remains as shipped history. |

A failed ship state remains visible at its retry stage with the daemon's
captured error. The chooser updates from snapshot deltas without reloading. If
it has no qualifying task after a snapshot, it links back to Tasks.

## Using ship flow

### 1. Draft

After its initial daemon snapshot, `/ship/<task-id>` automatically starts one
draft when the task has no ship state, is neither archived nor already attached
to a pull request, its primary session is both live and `idle`, and review is
either approved or absent. Review approval is the normal handoff. The
no-review case preserves the explicit Ship flow path for legacy tasks.

The Commit step shows **Drafting…** immediately. Its commit-message,
pull-request-title, and pull-request-body fields remain editable while the
agent works. A value the operator changes while that request is running is not
replaced by the arriving draft; untouched fields are seeded independently.
Signing and another draft request remain unavailable until the request is
terminal.

When the primary session is working, reviewing, starting, retrying, or waiting,
the step says that drafting is waiting for it to become idle. When no live
primary agent is available, the step says so and keeps all three fields usable
for manual text. A review record that is present but not approved prevents only
the automatic trigger; it does not erase the manual fields or change the
ordinary command guards.

`POST /api/tasks/{id}/ship/draft` is an idempotent **ensure draft** command.
It asks the task's primary session for a commit message and pull-request title
and body only when no ship attempt exists; repeated bodyless requests return
the existing draft or current attempt without another agent turn. Send
`{"replace": true}` to explicitly regenerate an existing draft or retry a
terminal draft error. Replacement is refused while any draft, commit, or push
is in flight.

The daemon broadcasts a parsed successful draft as `ship_draft`, prefilling
editable fields. It also publishes draft lifecycle through `ship_step`; see
[Interfaces](#interfaces). The draft turn drives the primary session
`idle → working → idle` through normal frame handling, with no bespoke
transition.

Drafting is best-effort. A transport error, timeout, missing agent text, or
invalid markers leaves the fields intact, records a retryable Draft-stage
error, and does not retry automatically. Correct the fields manually or use
the explicit retry action once the session is again eligible.

### 2. Commit

`POST /api/tasks/{id}/ship/commit` accepts `mode` of `squash` or `retain`,
plus the final message and pull-request fields. Both modes produce
operator-authored, GPG-signed commits in the host clone.

**Squash** fetches origin, computes the merge-base of `origin/<base>` and
`HEAD`, runs `git reset --soft` to that merge-base so every agent checkpoint
and uncommitted edit is staged as one set, and commits with `-S` using the
supplied message.

**Retain** instead rewrites the existing range with amend-and-sign, so every
commit keeps its message and tree but gains operator authorship and a good
signature. The rewritten tip sha is recorded.

In the UI, selecting Retain disables the commit-message field with a hint that
per-commit messages are retained. Pull-request fields stay editable in both
modes.

### 3. Push and open the pull request

After the signed commit the daemon pushes and opens the pull request with host
credentials only:

- Push goes to the project's `fork_url` when one is set, otherwise to
  `origin`.
- Push uses `--force-with-lease`, so a re-ship after review comments can
  rewrite the squashed commit safely.
- `gh pr create` runs against the project's upstream repository with the
  operator's title and body and the correct head reference — fork
  owner-qualified when a fork is configured.

On success the pull-request URL is persisted on the task row.

### 4. Cleanup

Cleanup is deferred until the pull request resolves. See
[merge polling](merge-poll.md).

## States and behavior

### Preconditions

Ship commit is refused, before any Git operation runs, when:

| Condition | Response |
|---|---|
| Unknown task | `404` |
| GPG signing key not `cached` | `409` with the lock detail |
| Mode other than `squash` or `retain` | `409` |
| A ship is already in flight | `409` |
| Retain with a dirty working tree | `409` naming the dirty tree |
| Retain over a range containing a merge commit | `409`; merges are unsupported |

Draft is refused with `404` for an unknown task. A new or explicit replacement
draft receives `409` when the task is archived, has a pull request, has already
shipped, has a draft/ship operation in flight, has no live primary agent, or
that session is not `idle`. A bodyless request that observes an existing draft,
draft error, or in-flight attempt returns it unchanged; `{"replace": true}` is
the explicit retry or regeneration path.

### Failure restores exactly

Ship rewrites Git history, so every failure path restores.

- A squash commit that fails after the soft reset restores `HEAD` to its
  pre-ship commit and records the ship state `error` with git's stderr.
- A retain rewrite that fails mid-range, or fails post-rewrite verification,
  aborts any in-progress rebase and restores `HEAD` and the working tree to
  the pre-ship state.

Push or pull-request failure records `error` with the captured stderr and
broadcasts `ship_finished` with status `error`. The commit is not rolled back
at that point — it is a legitimate local commit whose publication failed.

### Ship Flow view

The stepper renders live:

**Review** shows the review status, a `reopen 127.0.0.1:<port>` link while a
review is open, and the per-iteration history.

**Commit** starts an eligible initial draft automatically after the snapshot.
It shows **Drafting…** while that request is active, then shows commit-mode
radios with Squash as default, editable message and pull-request fields, a
**Re-draft via agent** control, and **Sign & commit**. Fields remain editable
while drafting; an arriving draft only fills fields the operator has not edited
since the request began. Re-drafting asks for confirmation only if it would
replace edited values, and preserves changes made after confirmation while the
replacement is running. A failed draft shows its captured reason and an
explicit retry alongside usable manual fields. When the shared GPG state is
`locked` it renders an amber blocked banner with the terminal-helper unlock
instruction and a **Re-check key** control, and disables **Sign & commit**.

**Push + PR** reflects progress from `ship_step` events and shows the
resulting pull-request link.

**Cleanup** shows the deferred, ready, or cleaned-up state per the task's
pull-request state.

All four update without a page reload.

Direct `/ship/<task-id>` navigation also waits for the current snapshot. An
unknown or non-numeric id after that snapshot shows **Task not found** with
links to both Ship flow and Tasks, rather than a transient false 404.

## Configuration

| Key | Effect |
|---|---|
| `gpg_signing_key` | The signing key. Required before any ship. |
| `gh_command` | The forge CLI; must be non-empty |

## Interfaces

| Method | Path |
|---|---|
| `POST` | `/api/tasks/{id}/ship/draft` — body omitted or `{"replace": false}` ensures one initial draft; `{"replace": true}` explicitly regenerates or retries |
| `POST` | `/api/tasks/{id}/ship/commit` |

| Event | Payload |
|---|---|
| `ship_step` | `{task_id, step, status, detail?}`; `step` is `draft`, `commit`, `push`, or `pr`, and `status` is `started`, `ok`, or `failed` |
| `ship_draft` | `{task_id, draft}` after a parsed successful draft |
| `ship_finished` | `{task_id, status, pr_url}` — `shipped` or `error` |

A draft begins with `ship_step` `draft`/`started`. Success publishes
`ship_draft`, then `draft`/`ok`; failure publishes `draft`/`failed` with its
reason. The snapshot carries a `ships` map from task id to the current status,
mode, draft, commit sha, pull-request URL, error, and latest transient
`last_step`, so a reconnect can render the same retry stage even if it missed a
delta. REST responses and deltas can arrive in either order; clients render the
daemon state rather than treating a response as the source of form values.

Ship state other than the persisted `pr_url` is held in memory and is lost on a
daemon restart as well as when the task is cleaned up or purged. A durable
approved review can make a newly opened Ship flow request a fresh draft after a
restart; no commit, push, or pull request is repeated automatically.

