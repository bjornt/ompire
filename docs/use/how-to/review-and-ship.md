# Review and ship a task

When an agent has finished its work, two things stand between it and a
published result: a review you control, and a delivery the agent cannot perform
itself. You also choose how far that delivery goes — a local signed commit, a
pushed branch, or a pull request.

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
   `/ship/<task-id>` directly at the task's delivery flow.

An approval names the content it graded. If the agent changes the workspace
afterwards, the approval stays on record as history and the panel says it no
longer covers what would be published — start another review of the current
content before delivering it.

To resume publishing later without returning through a task card, select
**Ship flow** in the global navigation. The chooser lists tasks that can enter
or resume the handoff before recent shipped history, identifies the next stage,
and opens the same task-specific flow. It never publishes anything by itself;
the review, draft, signing, push, pull-request, and cleanup controls remain on
that task's page.

The Review panel remains task-scoped when task detail is showing another
session tab, and it updates from the daemon stream without reloading the page.

### What the reviewer reads

To show the complete task delta rather than only the most recent commit, Ompire
captures a *candidate*: the whole publishable delta against the task's accepted
base, including uncommitted edits and new files. The reviewer reads an isolated
checkout of that candidate, not the task's live clone — so an agent that keeps
working cannot change what is under review, and a daemon that dies mid-review
leaves no parked working tree behind.

What such activity does change is the task's current candidate, which makes the
resulting approval unusable for delivery. That is a visible refusal rather than
a silent substitution.

Starting a review is refused when there is nothing to review, when another
writer already owns the workspace, or when a previous privileged effect's
outcome is unknown.

### REST alternative

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
is `POST /api/tasks/{id}/review/cancel`; the isolated review checkout is
removed and the task clone is untouched throughout.

## Deliver

Delivery has three phases, deliberately separated so you see exactly what will
happen before anything does.

### 1. Draft

On the task-specific Ship flow, select **Draft via agent** to have the primary
session write the publication text. You can write in the commit-message,
pull-request title, and pull-request body fields while it works; values you
change are kept when the agent's result arrives, while untouched fields are
filled in for you. **Save text** stores what you wrote by hand.

The draft is inert: it selects nothing and authorizes nothing.

If the agent is still working or reviewing, Ship flow waits for it to become
idle. If no live agent is available, enter the text by hand. A draft error
leaves those fields usable and provides an explicit retry; it never retries on
its own. A daemon restart during a draft turn is reported as an interruption
rather than silently starting another turn.

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

### GitHub preflight

Resolving an ending that pushes checks the daemon's current GitHub CLI identity
against the task's registered upstream target. It names the selected account and
canonical `host/owner/repository`; select **Re-check GitHub** after correcting
authentication or repository access. The confirmation stays disabled until this
check and the signing check are both ready.

A **local signed commit** needs neither: it does not touch the forge at all.

For CLI-stored credentials, run `gh auth login` or `gh auth switch` for the
named host and recheck. If the panel identifies `GH_TOKEN` or `GITHUB_TOKEN`,
that environment variable overrides stored accounts; correct the daemon's
launch environment and restart it. Ompire does not change accounts itself.

The check is a read-only GitHub API eligibility check. It does not test the SSH
key or HTTPS credential used by `git push`; fix a later push authentication
failure separately.

### 2. Preview the ending

Choose an ending, then select **Review this delivery**. Ompire resolves it
read-only and shows exactly what a confirmation would permit: the actions still
to run, the reviewed content being delivered, the signing identity, the account
and destination, the pull-request body it will write — marker included — and
every reason it is currently refused.

```sh
curl -sS -X POST http://127.0.0.1:4173/api/tasks/42/ship/preview \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
        "ending": "pr",
        "mode": "squash",
        "message": "fix: stop the redirect loop after login",
        "pr_title": "Fix login redirect loop",
        "pr_body": "...",
        "request_id": "a-stable-client-identifier"
      }'
```

The response carries a `preview_token`, a `version`, and a `deliverable` flag
with a list of `blockers`. Nothing has been authorized.

### 3. Confirm the ending

```sh
curl -sS -X POST http://127.0.0.1:4173/api/tasks/42/ship/commit \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
        "ending": "pr",
        "mode": "squash",
        "message": "fix: stop the redirect loop after login",
        "pr_title": "Fix login redirect loop",
        "pr_body": "...",
        "request_id": "a-stable-client-identifier",
        "preview_token": "<from the preview>",
        "expected_version": 3
      }'
```

The daemon then runs only the actions that ending authorizes. Changing anything
between the preview and the confirmation invalidates the token, and resending
the same confirmation cannot start a second delivery.

Use `ending: "commit"` to stop at a local signed commit, or `ending: "push"` to
stop at a pushed branch.

### Going further later

An existing signed result can be pushed later, and an existing pushed result can
get a pull request later, each on its own preview and confirmation:

```sh
curl -sS -X POST http://127.0.0.1:4173/api/tasks/42/ship/push \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"ending": "push", "request_id": "…", "preview_token": "…",
       "delivery_id": 7, "expected_version": 5}'
```

Neither re-signs, and neither starts an implicit earlier action.

## Delivery modes

| Mode | Result |
|---|---|
| `squash` | The reviewed content becomes one signed commit. |
| `retain` | Individual commits are preserved and rewritten to be signed. |

`retain` reports its preconditions in the preview and verifies commit count,
trees, messages, and signatures after rewriting. `squash` is the simpler path
and the default choice. A delivery's mode is fixed once it has a signed result.

Signing happens against the reviewed content in its own protected store, and the
result is installed into the clone only under a compare-and-swap against the
HEAD it was captured at. Your working files are never reset over.

## What blocks a delivery

The preview lists every reason at once, so you fix them together rather than one
per attempt. The full list is in the
[Ship flow reference](../reference/ship-flow.md#why-a-delivery-is-refused);
the common ones are a missing, unbound, or stale approval, a signing key that is
not ready, GitHub access for an ending that pushes, retain preconditions, and a
workspace another writer already owns.

A confirmation is refused when its token no longer matches what was previewed,
when the delivery moved on, or when a request identifier is reused with
different inputs. Every one of these is refused before any Git operation runs,
so a rejected delivery leaves nothing to clean up.

## When Ompire cannot tell what happened

If the daemon dies, or a response is lost, between launching a privileged action
and recording its result, the task shows an **unresolved effect** rather than
guessing. Nothing dependent runs, cleanup is refused, and you are shown the
action, its expected target, what was observed, and why it cannot continue.

Choose **Recheck** to observe again, **Adopt the result** for something Ompire
can verify, **Allow a retry** once it has proved the action did not happen, or
**Abandon** to grant no further authority. None of these writes anything
privileged, and abandoning leaves an unknown effect on record as still unknown.

A restart never signs, pushes, or creates a pull request on your behalf.

## After the pull request

Ompire polls the pull request's state and records when it merges. The task keeps
its pull-request URL, state, and merge time. A delivery that ended before a pull
request is not polled and waits for no merge.

## Cleaning up

`POST /api/tasks/{id}/cleanup` removes the container, deletes the clone, and
archives the task. A pull-request delivery waits for the pull request to
resolve; an earlier ending is ready as soon as it completes.

Cleanup names what it removes. A **local-only** result lives in the clone alone,
so removing it removes the only Ompire-managed Git copy of that commit — the
delivery record is retained, and a record is not a backup. A **push-only**
result names the remote branch it left behind and the absence of a pull request;
no remote branch or pull request is ever deleted.

Cleanup is refused while a delivery is still running and while any effect's
outcome is unknown. The task's record, its review history, and its delivery
journal all survive cleanup; only purge deletes them.
