# Ship flow

## Overview

Shipping turns a task's work into a signed commit, a pushed branch, and a pull
request. The agent drafts the text; the daemon does everything else with
host-side credentials the agent never sees.

The flow has four steps — Review, Commit, Push + PR, Cleanup — surfaced as a
stepper in the Ship Flow view.

## Using ship flow

### 1. Draft

`POST /api/tasks/{id}/ship/draft` asks the task's **primary session** to draft
a commit message and a pull-request title and body. The agent has full task
context, which is exactly what makes it good at this and nothing else in the
flow.

The draft is recorded and broadcast as `ship_draft`, prefilling editable
fields.

Drafting is best-effort. If the reply cannot be obtained or parsed, the ship
state is recorded `error` with a reason and the operator supplies the fields
by hand. The draft turn drives the primary session `idle → working → idle`
through normal frame handling, with no bespoke transition.

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

Draft is refused with `409` when the primary session has no live agent, and
`404` for an unknown task.

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

**Commit** shows commit-mode radios with Squash as default, editable message
and pull-request fields prefilled from the draft, a "Re-draft via agent"
control, and "Sign & commit". When the shared GPG state is `locked` it renders
an amber blocked banner with the terminal-helper unlock instruction and a
"Re-check key" control, and disables Sign & commit.

**Push + PR** reflects progress from `ship_step` events and shows the
resulting pull-request link.

**Cleanup** shows the deferred, ready, or cleaned-up state per the task's
pull-request state.

All four update without a page reload.

## Configuration

| Key | Effect |
|---|---|
| `gpg_signing_key` | The signing key. Required before any ship. |
| `gh_command` | The forge CLI; must be non-empty |

## Interfaces

| Method | Path |
|---|---|
| `POST` | `/api/tasks/{id}/ship/draft` |
| `POST` | `/api/tasks/{id}/ship/commit` |

| Event | Payload |
|---|---|
| `ship_draft` | `{task_id, draft}` |
| `ship_step` | `{task_id, step, status, detail}` for commit, push, and pr |
| `ship_finished` | `{task_id, status, pr_url}` — `shipped` or `error` |

The snapshot carries a `ships` map from task id to the current status, mode,
draft, commit sha, pull-request URL, and error.

Ship state other than the persisted `pr_url` is held in memory and discarded
when the task is cleaned up or purged.
