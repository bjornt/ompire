# Review and ship a task

When an agent has finished its work, two things stand between it and a pull
request: a review you control, and a publishing step the agent cannot perform
itself.

## Review

Starting a review opens a real review tool against the host side of the task's
clone. The agent being reviewed does not run it and cannot influence the
verdict.

### Start, inspect, and continue from task detail

1. Open the task card, then its task detail.
2. In **Review**, wait for the primary session to become idle and select
   **Start review**. The panel keeps the action locked while the daemon starts
   the reviewer.
3. Select the full llmvet URL from the open Review panel to inspect the
   independent review. Use **Cancel review** only to stop an open review; the
   panel shows a failed command and allows retry when the daemon's state still
   permits it.
4. If comments return, let the primary agent address them. When it is idle
   again, select **Start another review**. The ordered history retains every
   iteration, including reviewer error detail.
5. After **Approved**, select **Continue to Ship flow**. It opens
   `/ship/<task-id>` directly at the task's publishing flow and automatically
   starts the first eligible publication draft.

To resume publishing later without returning through a task card, select
**Ship flow** in the global navigation. The chooser lists tasks that can enter
or resume the handoff before recent shipped history, identifies the next stage,
and opens the same task-specific flow. It never publishes anything by itself;
the review, draft, signing, push, pull-request, and cleanup controls remain on
that task's page.

The Review panel remains task-scoped when task detail is showing another
session tab, and it updates from the daemon stream without reloading the page.

### REST alternative

To show the complete task delta rather than only the most recent commit, the
review temporarily resets the clone against the base branch. Before doing so it
records the original `HEAD` under the durable Git ref `refs/ompire/review-orig`.
That ref is the recovery artifact: if the daemon dies mid-review, the original
state is restored from it at the next startup, and the ref is deleted only
after a successful restore.

The same action remains available through the authenticated REST API:

```sh
TOKEN=$(cat ~/.local/share/ompire/token)
curl -sS -X POST http://127.0.0.1:4173/api/tasks/42/review \
  -H "Authorization: Bearer $TOKEN"
```

While a review is open, the task's primary session reports `reviewing`.
Approving or aborting returns it to `idle`. Feeding a review comment back to
the agent moves it to `working` — the comment becomes the agent's next prompt,
and the review loop continues from there.

Cancel from task detail with **Cancel review**. The equivalent REST operation
is `POST /api/tasks/{id}/review/cancel`; cancellation restores the clone from
the saved ref.

## Ship

Shipping has two phases, deliberately separated so you see what will be
published before anything is.

### 1. Draft

On the task-specific Ship flow, an approved review normally starts the agent
draft automatically once the primary session is idle. The Commit step says
**Drafting…** immediately. You can write in the commit-message, pull-request
title, and pull-request body fields while it works; values you change are kept
when the agent's result arrives, while untouched fields are filled in for you.

If the agent is still working or reviewing, Ship flow waits for it to become
idle. If no live agent is available, enter the metadata by hand. A draft error
leaves those fields usable and provides an explicit retry; it never retries on
its own.

The authenticated REST command remains useful for automation or recovery. With
no body it safely ensures an initial draft — a repeated request returns the
current draft or current attempt rather than prompting the agent twice:

```sh
curl -sS -X POST http://127.0.0.1:4173/api/tasks/42/ship/draft \
  -H "Authorization: Bearer $TOKEN"
```

Use a deliberate replacement request to regenerate a ready draft or retry a
draft error:

```sh
curl -sS -X POST http://127.0.0.1:4173/api/tasks/42/ship/draft \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"replace": true}'
```

In the UI, **Re-draft via agent** asks for confirmation only when it would
replace metadata you edited. After confirmation, newer edits made while the
replacement is running remain yours.

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
creation through `gh`. Progress is published per step — `draft`, `commit`,
`push`, `pr` — and the pull-request URL is attached to the task when it
succeeds.

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
| A commit or push is already in flight | `409` |
| Mode other than `squash` or `retain` | `409` |
| `retain` preconditions unmet | `409` with the reason |
| No live or idle primary agent (new/replacement draft only) | `409` |
| Archived task, existing pull request, or completed ship (new/replacement draft only) | `409` |
| A draft is already in flight (replacement only) | `409` |

A bodyless draft request that finds an existing draft, terminal draft error, or
current attempt returns that observed state unchanged. Use `{"replace": true}`
or the UI's explicit retry only after the primary session is idle.

Every one of these is refused before any Git operation runs, so a rejected
ship leaves nothing to clean up.

## After the pull request

Ompire polls the pull request's state and records when it merges. The task
keeps its pull-request URL, state, and merge time.

## Cleaning up

Once the pull request has landed, `POST /api/tasks/{id}/cleanup` removes the
container, deletes the clone, and archives the task. The task's record and its
publishing history remain.
