# Review and ship a task

When an agent has finished its work, two things stand between it and a
published result: an independent review, and a decision only you can make. How
far a delivery goes — a local signed commit, a pushed branch, a pull request,
or nothing at all — is what the task's own workflow declares and what your
answer authorizes.

There is **one path**. The workflow says what may happen; you answer its
question; and the run performs exactly the effects your answer named. Task
detail and Ship flow are two views of that same decision, not two ways to make
it.

## Review

Starting a review opens a real review tool against the host side of the task's
clone. The agent being reviewed does not run it and cannot influence the
verdict.

### When the workflow owns the review

Both packaged workflows declare their own `review` step, and so does any
workflow you author with one. There is nothing to start:

1. Open the task card, then its task detail.
2. When the run reaches its review step it starts the reviewer itself. The
   **Review** panel shows the llmvet URL; select it to inspect the review.
3. If comments return, the workflow's own route carries the reviewer's report
   back to the step the author named — usually the one that did the work — and
   opens the next review when that step finishes. You do not drive the loop,
   and its bound is what ends it.
4. After **Approved**, the run reaches its approval and waits for you. Select
   **Continue to Ship flow**.

The panel says plainly that the run owns the review, so a missing **Start
review** button is an answer rather than a puzzle.

### When you own the review

A workflow that declares no review step — an older definition, or one you wrote
without one — is reviewed by you:

1. In **Review**, wait for the primary session to become idle and select
   **Start review**. The panel keeps the action locked while the daemon starts
   the reviewer.
2. Select the full llmvet URL to inspect the review. Use **Cancel review** only
   to stop an open review; the panel shows a failed command and allows retry
   when the daemon's state still permits it.
3. If comments return, let the primary agent address them. When it is idle
   again, select **Start another review**. The ordered history retains every
   iteration, including reviewer error detail.

Such a workflow cannot publish: it has no step that could. See
[Older tasks](#older-tasks-cannot-publish).

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

### 1. The publication text

A workflow that declares its own publication also declares its own text. Its
approval renders a suggested commit message, pull-request title, and body from
the run's own evidence, and Ship flow shows them as editable fields. Change
whatever you like: what you confirm is what gets published.

Asking an agent to draft the text is refused for such a task, and deliberately:
during an approval wait a turn would change the very content the decision is
about. **Save text** stores what you wrote by hand, and is always available.

For a workflow that declares no publication of its own, **Draft via agent** is
still there and works as before — with a repeated request returning the current
draft rather than prompting twice:

```sh
curl -sS -X POST http://127.0.0.1:4173/api/tasks/42/ship/draft \
  -H "Authorization: Bearer $TOKEN"
```

Those tasks cannot publish, so the draft is only ever text you copy out by
hand.

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

### 2. Preview the decision

Ship flow shows the question the run is asking and the answers that publish
something, each naming the actions it authorizes. Nothing is preselected.
Choose one, then select **Review this delivery**. Ompire resolves it read-only
and shows exactly what a confirmation would permit: which question and answer
it is about, the review attempt the grant rests on, the actions still to run,
the reviewed content being delivered, the signing identity, the account and
destination, the pull-request body it will write — marker included — and every
reason it is currently refused.

The same over REST. The preview names the decision; the ending and the commit
mode come from the workflow's own chain rather than from the request:

```sh
curl -sS -X POST http://127.0.0.1:4173/api/tasks/42/ship/preview \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
        "gate_seq": 7,
        "choice_id": "open-pr",
        "message": "fix: stop the redirect loop after login",
        "pr_title": "Fix login redirect loop",
        "pr_body": "...",
        "request_id": "a-stable-client-identifier"
      }'
```

The response carries a `preview_token`, a `version`, the derived `ending` and
`actions`, and a `deliverable` flag with a list of `blockers`. Nothing has been
authorized.

### 3. Confirm it

```sh
curl -sS -X POST http://127.0.0.1:4173/api/tasks/42/ship/commit \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
        "gate_seq": 7,
        "choice_id": "open-pr",
        "note": "why I am publishing this",
        "message": "fix: stop the redirect loop after login",
        "pr_title": "Fix login redirect loop",
        "pr_body": "...",
        "request_id": "a-stable-client-identifier",
        "preview_token": "<from the preview>",
        "expected_version": 3
      }'
```

That single call records the decision, the authorization it produces, and the
run's move to its first action — together. The run then performs the actions
the answer authorized, in order, and nothing else. Changing anything between
the preview and the confirmation invalidates the token, and resending the same
confirmation cannot start a second delivery.

To stop at a local signed commit or a pushed branch, answer with the choice
that says so. Which endings exist is the workflow's, not the request's.

### Answers that publish nothing

"Finish without publishing" and "send it back with changes" are answered from
task detail like any other gate answer — they authorize nothing, so they need
no preview. Finishing without publishing is a complete, named ending.

### Continuing an interrupted delivery

If a daemon restart or a failure interrupts an authorized chain, the run holds
that attempt open rather than retrying it. The grant still stands. Preview the
delivery again — Ship flow says it is continuing an authorized action — and
confirm; the run resumes the same attempt against the same journal, and an
effect that is proven to have already happened is adopted instead of repeated.

### Older tasks cannot publish

A task pinned to a workflow that declares no publication steps has no ending to
authorize. The preview refuses with `no-delivery-vocabulary`, and Ship flow
says so instead of offering a control that would then be declined. Launch a new
task from a workflow that declares the delivery you want; the old task keeps
its work, its history, and its workspace.

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
