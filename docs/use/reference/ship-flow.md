# Ship flow

## Overview

Delivering a task turns its reviewed work into one of three endings — a local
signed commit, a pushed branch, or a pull request — and the operator chooses
which. The primary agent can draft the publication text; the daemon does
everything else with host-side credentials the agent never sees.

The flow has four steps — Review, Deliver, Delivered, Cleanup — surfaced as a
stepper in the Ship Flow view. Opening a task-specific Ship flow prepares an
eligible agent draft; opening the page, generating a draft, or finishing agent
work grants no publication authority.

Every delivery is content-bound (ADR-0032). Review captures a *candidate* — the
task's whole publishable delta against its accepted base — and an approval names
the candidate it graded. What gets signed is that candidate, taken from a
protected copy, not whatever the workspace happens to hold when signing starts.

## Endings

| Ending | What the confirmation permits |
|---|---|
| Local signed commit | Sign locally and stop. Nothing is pushed and no pull request is opened. |
| Pushed branch | Sign, then push to the task's accepted destination. No pull request is opened. |
| Pull request | Sign, push, and open a pull request. |

A local or push-only ending is a **successful delivery**, not a failed pull
request. A completed ending can be extended later: an operator can authorize
pushing an existing signed result, or opening a pull request for an existing
pushed result, without repeating what already completed. Each further ending is
previewed and confirmed on its own and appends its authorization; the earlier
one is never rewritten.

Changing the content requires a fresh review and a new delivery authorization.

## Preview and confirmation

Nothing privileged happens without two steps.

`POST /api/tasks/{id}/ship/preview` resolves one requested ending read-only. It
captures nothing, writes no Git state, and authorizes nothing. It returns:

- the candidate being delivered — its identity, base branch, base commit, tree,
  and commit count — and whether the approval covers it;
- the actions still to run and the ones already completed;
- the accepted destination, the signing identity, and the GitHub account, where
  each applies to the requested ending;
- the exact pull-request body Ompire will write, including the recovery
  correlation marker;
- **every** reason the delivery is currently refused, not just the first;
- a `preview_token` fingerprint over those exact inputs.

The confirmation carries that token back. Changing the ending, the mode, the
metadata, the content, or the destination invalidates it, and a replayed or
conflicting submission cannot start a second delivery.

### Why a delivery is refused

| Blocker | Meaning |
|---|---|
| `review-missing` | The task has no approved review. |
| `review-unbound` | The approval predates content-bound review, so it does not identify what was approved. It stays on record; delivering needs a fresh review. |
| `review-stale` | The content changed after the approval. Review the current content before delivering it. |
| `empty-candidate` | There is nothing to deliver. Ompire refuses rather than manufacturing a commit. |
| `candidate-unavailable` | The workspace could not be resolved into publishable content. |
| `signing-unavailable` | The signing key is not ready; the message names the specific state. |
| `github-unavailable` | GitHub identity or repository eligibility blocks a pushing ending. A local ending is unaffected. |
| `retain-dirty` / `retain-empty` / `retain-merges` | Retain publishes existing commits; this range cannot be retained as it stands. |
| `workspace-busy` | A review, draft, or delivery already owns the task workspace. |
| `unresolved-effect` | A previous privileged action's outcome is unknown; nothing dependent may run. |
| `already-delivered` | This ending's actions have all completed. |
| `archived` | The task is archived. |

### Credentials by ending

A local signed commit needs signing readiness and nothing else — no GitHub
availability at all. Any ending that pushes keeps the trusted-target and GitHub
eligibility preflight, and pull-request creation repeats the GitHub
identity/target check immediately before its write.

That check demonstrates GitHub **API** identity and repository eligibility only.
It is not proof of the SSH key or HTTPS credential used for Git transport, and
the preview says so: the Git transport identity is recorded as unattributed
rather than invented.

A later push or pull request of an already-verified signed result does not
require re-signing or an unlocked signing key.

## Ship flow index

The global **Ship flow** navigation item opens `/ship`, a chooser for the
existing task-specific publishing workflows. It sends no command and relaxes no
precondition.

The chooser waits for the current daemon snapshot before deciding what is
available. Once it arrives, non-archived tasks with an approved review, a
delivery record, or a pull request appear in **Ready or in progress**, and
archived ones in **Recently shipped**, each ordered by `updated_at`, newest
first.

| Label | Meaning |
|---|---|
| Needs a decision | An effect's outcome is unknown and an operator decision is required. |
| Review | The recorded handoff has not reached an approved review of the current content. |
| Draft | Review is approved; publication text and an ending are still to be chosen. |
| Deliver | A delivery is authorized or stopped and can be confirmed. |
| Signed locally | A local ending completed. Nothing was pushed. |
| Pushed | A push ending completed. No pull request was opened. |
| Wait for merge | A pull request exists but has not resolved. |
| Cleanup | Delivery is finished and the workspace can be removed. |
| Cleanup complete | An archived task remains as delivery history. |

## Using ship flow

### 1. Review

Delivery needs an approved review of the content being delivered. See
[Review](review.md) for how an approval is bound to a candidate and when it
stops being usable.

### 2. Draft

`POST /api/tasks/{id}/ship/draft` is an idempotent **ensure draft** command. It
asks the task's primary session for a commit message and pull-request title and
body only when no ready draft exists; repeated bodyless requests return the
existing draft without another agent turn. Send `{"replace": true}` to
regenerate, and `PUT /api/tasks/{id}/ship/draft` to store text written by hand.

The draft is inert. It is editable at any time, it selects nothing, and it
authorizes nothing.

Drafting is best-effort. A transport error, timeout, missing agent text, or
invalid markers leaves the fields intact and records a retryable draft error. A
daemon restart during a draft turn records it as **interrupted**: nothing is
sent again on the operator's behalf, and the retry is explicit.

While the primary session is working, reviewing, starting, retrying, or
waiting, the step says drafting is waiting for it. With no live primary agent,
every field stays usable for manual text.

### 3. Deliver

Choose an ending, choose squash or retain, review the resolved preview, then
confirm. The confirmation names every effect it permits.

**Squash** delivers exactly the reviewed candidate tree as one signed commit on
the candidate's base.

**Retain** replays the reviewed range commit by commit, preserving each message
and tree and rewriting only the identity and signature. It refuses a dirty tree,
an empty range, and merge commits.

Both modes sign against the protected candidate — never a freshly staged live
workspace — using the operator's own signing configuration, never the task
clone's. Trees, commit count, parents, and the signing key are all verified
before the result leaves its protected store.

The signed result is then installed into the task clone under a compare-and-swap
against the HEAD the candidate was captured at, and the index is synchronized to
the signed tree only after re-checking what the workspace holds. A workspace
that moved on keeps its files, and the delivery stops for a decision rather than
resetting over new work.

### 4. Delivered

Completed actions show their concrete results: the signed tip and commit count,
the pushed branch and head, or the pull-request link. A delivery that ended
before a pull request says so plainly, and offers the further endings.

### 5. Cleanup

Cleanup removes the workspace. It is refused while any writer owns the task and
while a privileged effect's outcome is unknown, and it warns about what it is
about to remove:

- A **local-only** result lives in the clone alone. Removing it removes the only
  Ompire-managed Git copy of that commit. The delivery record is retained, and a
  record is not a backup.
- A **push-only** result names the remote branch it left behind and the absence
  of a pull request. No remote branch or pull request is ever deleted.

A pull-request delivery stays deferred until the pull request resolves; see
[merge polling](merge-poll.md).

## Interrupted effects

Every action attempt records what it is about to do — the destination ref and
object id, the observed pre-push head, the correlation marker that will be in
the pull-request body — and commits that *before* anything runs. An attempt is
only recorded as failed when its non-execution or a verified rollback was
established. Anything less certain becomes an **unresolved** effect, which is
neither success nor failure.

While an effect is unresolved, no dependent action runs, cleanup is refused, and
the task shows the action, its expected target, what was observed, and why
Ompire cannot safely continue. Four decisions are available, and none of them
writes anything privileged:

| Decision | Effect |
|---|---|
| Recheck | Observe again. A now-verifiable result is adopted. |
| Adopt | Adopt a result Ompire can verify. An unverifiable one is refused. |
| Retry | Only once non-execution is proven; it makes the action eligible for a fresh preview and confirmation, not an unconditional write. |
| Abandon | Record that no further authority was granted. An unknown effect stays unknown, and cleanup stays refused. |

How each action is observed:

- **Commit** — the attempt writes its signed result under its own protected ref
  before it can return, so the absence of that ref proves no signature landed. A
  result that exists is verified against the reviewed tree, range, and signer.
- **Push** — the exact destination ref is compared with the authorized head and
  the recorded pre-push value. The lease is that recorded value, never a
  tracking ref refreshed behind the operator. An unexpected head is a conflict,
  not permission to force over it.
- **Pull request** — a deterministic correlation marker in the authorized body
  is looked for across every pull-request state, including closed and merged,
  with a bounded search that reports its own incompleteness. Exactly one
  verified match is adoptable; ambiguous, incomplete, or unavailable stays
  unresolved. `gh pr create` is never replayed just because a listing found
  nothing.

A daemon restart performs no signing, no push, and no forge write. It restores
safe state, reconciles what it can observe, and requires an explicit
continuation for remaining work.

## Upgrading from an earlier Ompire

Existing review iterations and pull-request records are preserved and never
rewritten. Reviews recorded before content binding carry no candidate: they stay
visible as history and require a fresh review before a new delivery. A task that
shipped before the delivery journal existed shows its pull request as a known
fact with no recorded authorization behind it, which is exactly what is true.

Clones parked by an older daemon's signing dance are still recognized on
startup, and are restored only when the restoration verifies; one that cannot be
restored safely keeps its marker rather than losing the evidence.

## Configuration

| Key | Effect |
|---|---|
| `gpg_signing_key` | The signing key. Selectable in Settings, which takes precedence over this file; auto-detected when the host holds exactly one. |
| `gh_command` | Non-empty GitHub CLI prefix used for bounded, non-interactive version, API, PR-create, PR-list, and PR-watch calls. |

## Interfaces

| Method | Path |
|---|---|
| `GET` | `/api/tasks/{id}/ship` — the current delivery projection, read-only |
| `POST` | `/api/tasks/{id}/ship/draft` — body omitted or `{"replace": false}` ensures one draft; `{"replace": true}` regenerates |
| `PUT` | `/api/tasks/{id}/ship/draft` — store operator-written publication text |
| `POST` | `/api/tasks/{id}/ship/preview` — resolve one ending read-only |
| `POST` | `/api/tasks/{id}/ship/commit` — authorize a delivery and run its action prefix |
| `POST` | `/api/tasks/{id}/ship/push` — push an existing verified signed result |
| `POST` | `/api/tasks/{id}/ship/pr` — open a pull request for an existing verified pushed result |
| `POST` | `/api/tasks/{id}/ship/reconcile` — record one decision about an unresolved effect |
| `GET` | `/api/gh` — latest safe in-memory identity and target status |
| `POST` | `/api/gh/recheck` — body omitted for global identity, or `{"task_id": id}` for the task's registered upstream |

| Event | Payload |
|---|---|
| `ship_updated` | The whole versioned per-task delivery projection, published after the daemon commits it |
| `gh_status` | `{gh: {identity, targets}}` after every completed GitHub probe |

The snapshot carries a `ships` map from task id to that same projection, so a
reconnecting client sees the current state without replaying anything. Every
projection carries a per-task monotonic `version`; command responses go through
the same reducer as the deltas, and an older or duplicated version is dropped
rather than applied.

Unlike earlier releases, delivery state is durable. The authorization, the
candidate it names, every action attempt with the exact refs it intended to
write, and every reconciliation decision survive a daemon restart, and they
survive cleanup — a cleaned-up task keeps the record of what it published and
under whose authorization. Only purge deletes them.
