# Review and ship a task

When an agent has finished its work, two things stand between it and a pull
request: a review you control, and a publishing step the agent cannot perform
itself.

## Review

Starting a review opens a real review tool against the host side of the task's
clone. The agent being reviewed does not run it and cannot influence the
verdict.

To show the complete task delta rather than only the most recent commit, the
review temporarily resets the clone against the base branch. Before doing so it
records the original `HEAD` under the durable Git ref `refs/ompire/review-orig`.
That ref is the recovery artifact: if the daemon dies mid-review, the original
state is restored from it at the next startup, and the ref is deleted only
after a successful restore.

```sh
TOKEN=$(cat ~/.local/share/ompire/token)
curl -sS -X POST http://127.0.0.1:4173/api/tasks/42/review \
  -H "Authorization: Bearer $TOKEN"
```

While a review is open, the task's primary session reports `reviewing`.
Approving or aborting returns it to `idle`. Feeding a review comment back to
the agent moves it to `working` — the comment becomes the agent's next prompt,
and the review loop continues from there.

Cancel with `POST /api/tasks/{id}/review/cancel`. Cancellation restores the
clone from the saved ref.

## Ship

Shipping has two phases, deliberately separated so you see what will be
published before anything is.

### 1. Draft

```sh
curl -sS -X POST http://127.0.0.1:4173/api/tasks/42/ship/draft \
  -H "Authorization: Bearer $TOKEN"
```

The daemon prompts the task's primary session to draft a commit message and
pull-request title and body. This requires a live agent — drafting is the one
part of shipping the agent does. The result is returned for you to edit.

### 2. Commit, push, and open the pull request

```sh
curl -sS -X POST http://127.0.0.1:4173/api/tasks/42/ship/commit \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
        "mode": "squash",
        "message": "fix: stop the redirect loop after login",
        "pr_title": "Fix login redirect loop",
        "pr_body": "..."
      }'
```

The daemon performs the rest itself: signed commit, push, then pull-request
creation through `gh`. Progress is published per step — `commit`, `push`,
`pr` — and the pull-request URL is attached to the task when it succeeds.

## Ship modes

| Mode | Result |
|---|---|
| `squash` | The task's work becomes one signed commit. |
| `retain` | Individual commits are preserved and rewritten to be signed. |

`retain` checks its preconditions before starting and verifies commit count and
signatures after rewriting. `squash` is the simpler path and the default
choice.

Like review, shipping saves the original `HEAD` under a durable ref —
`refs/ompire/ship-orig` — before rewriting, and restores from it if the
sequence is interrupted.

## What blocks a ship

| Condition | Response |
|---|---|
| GPG key not `cached` | `409` with the current GPG state attached |
| A ship already in flight for this task | `409` |
| Mode other than `squash` or `retain` | `409` |
| `retain` preconditions unmet | `409` with the reason |
| No live agent (draft only) | `409` |

Every one of these is refused before any Git operation runs, so a rejected
ship leaves nothing to clean up.

## After the pull request

Ompire polls the pull request's state and records when it merges. The task
keeps its pull-request URL, state, and merge time.

## Cleaning up

Once the pull request has landed, `POST /api/tasks/{id}/cleanup` removes the
container, deletes the clone, and archives the task. The task's record and its
publishing history remain.
