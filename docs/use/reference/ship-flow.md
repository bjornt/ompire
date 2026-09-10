# Ship flow

## Overview

Delivering a task turns its reviewed work into one of three endings — a local
signed commit, a pushed branch, or a pull request. The daemon performs each
with host-side credentials the agent never sees.

**The task's own workflow decides which endings are on offer**, and a person
decides which one happens. A workflow declares publication as steps; one of its
approval's answers names the exact chain it authorizes; and the run performs
that chain and nothing more
([ADR-0033](../../adr/0033-scope-trusted-delivery-authority-to-the-workflow-run.md)).
Ship flow is where that decision is made and where its progress is read — it is
a view and a control for the procedure, not a way around it.

The flow has four steps — Review, Deliver, Delivered, Cleanup — surfaced as a
stepper in the Ship Flow view. Opening the page, preparing text, an approval in
the review tool, and an agent reporting success all grant no publication
authority.

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
| No publication | The workflow reaches a named ending with no privileged effect at all. |

A local or push-only ending is a **successful delivery**, not a failed pull
request. So is finishing without publishing: it is an ending an author declared
and a person chose.

The packaged planning workflow reaches the no-publication ending through its
own result gate. It declares neither review nor delivery steps, so Results
acceptance and completing that gate cannot make Ship flow available.

**A completed ending does not grow.** The chain a person authorized is what
runs, and once it is done there is nothing further to authorize — asking for a
longer ending is refused, at the preview and at every action endpoint alike. If
you want a task's work pushed, authorize an answer that says so, or launch it
under a workflow that declares that ending.

Changing the content requires a fresh review and a new delivery authorization.

## Preview and confirmation

Nothing privileged happens without two steps.

`POST /api/tasks/{id}/ship/preview` resolves the delivery this run's procedure
currently permits, read-only. It captures nothing, writes no Git state, and
authorizes nothing.

The ending and the commit mode are **derived, not requested**: they come from
the chain the run's own answer would authorize. A caller may still name one, and
a disagreement is reported rather than obeyed. When the run is waiting at an
approval, the preview identifies which question and which answer it is about;
naming neither, or naming a stale attempt, an unknown answer, or an answer that
publishes nothing, is refused.

It returns:

- which decision it describes — the question, the answer, and the review
  attempt the grant is bound to;
- the candidate being delivered — its identity, base branch, base commit, tree,
  and commit count — and whether the approval covers it;
- the actions still to run and the ones already completed;
- the accepted destination, the signing identity, and the GitHub account, where
  each applies to the requested ending;
- the exact pull-request body Ompire will write, including the recovery
  correlation marker;
- **every** reason the delivery is currently refused, not just the first;
- a `preview_token` fingerprint over those exact inputs.

The confirmation carries that token back. Changing the answer, the metadata,
the content, or the destination invalidates it, and a replayed or conflicting
submission cannot start a second delivery. The token also covers *which
decision* this is, so a confirmation prepared against one question cannot be
replayed against the next one with identical content.

Confirming an approving answer is **one operation**, whichever page it came
from: the decision, the delivery authorization it produces, and the run's move
to its first action become durable together. Task detail shows the same pending
decision and links to it; a generic Resume there cannot answer a publishing
choice, because without the preview there is no evidence the operator saw the
content, the target, and the identities the authorization is about.

### Why a delivery is refused

| Blocker | Meaning |
|---|---|
| `no-delivery-vocabulary` | The task's pinned workflow declares no publication steps, so nothing can authorize signing, pushing, or opening a pull request. Launch a new task from a workflow that declares the delivery it should perform. |
| `not-at-gate` | The run is not waiting at an approval that authorizes publication, and has no authorized action outstanding. |
| `review-missing` | The task has no approved review. |
| `review-unrecorded` | The approval names a review attempt with no recorded verdict. Nothing is authorized against a review that did not happen. |
| `review-not-approved` | The review *this approval is about* ended in something other than approved. |
| `review-unbound` | The approval predates content-bound review, so it does not identify what was approved. It stays on record; delivering needs a fresh review. |
| `review-stale` | The content changed after the approval. Review the current content before delivering it. |
| `empty-candidate` | There is nothing to deliver. Ompire refuses rather than manufacturing a commit. |
| `candidate-unavailable` | The workspace could not be resolved into publishable content. |
| `signing-unavailable` | The signing key is not ready; the message names the specific state. |
| `github-unavailable` | GitHub identity or repository eligibility blocks a pushing ending. A local ending is unaffected. |
| `retain-dirty` / `retain-empty` / `retain-merges` | Retain publishes existing commits; this range cannot be retained as it stands. |
| `retain-protected-paths` | A commit this delivery would publish carries a [handoff input](#handoff-inputs). Names the paths and the commit. |
| `retain-protected-unreadable` | Ompire could not check every commit for handoff inputs, so it will not publish them. |
| `workspace-busy` | A review, draft, or delivery already owns the task workspace. |
| `unresolved-effect` | A previous privileged action's outcome is unknown; nothing dependent may run. |
| `already-delivered` | This ending's actions have all completed. |
| `archived` | The task is archived. |
| `predecessor-missing` | An action was asked for before the one it consumes completed. No action performs a missing predecessor. |
| `action-mismatch` / `not-at-action` | The action requested is not the one the run is at. |

### Handoff inputs

A task launched with [handoff inputs](task-spawn.md#handoff-inputs) may never
publish those files. The protected set is exactly that task's attached
destinations, read from its own accepted launch document — not a naming
convention, and not an ignore file the agent can edit. A task without handoff
inputs has an empty set and is completely unaffected.

Ordinary code ships normally while those files sit untracked and excluded in
the clone. What is refused is a *Git result* that carries one:

| When | What happens |
|---|---|
| The proposed tree contains a handoff destination | Capture refuses, so review will not start and no mode can deliver |
| Retain mode, and any commit in the published range contains one | The delivery is blocked with `retain-protected-paths`, naming the commit |
| Squash mode, with a clean final tree | Delivers normally, even if unpublished agent checkpoints carried one — those commits are not published |
| The merge-base itself tracks the path | Capture refuses rather than publishing an upstream-tracked handoff implicitly |
| A handoff file was replaced by a directory | Refused through its descendants; protection covers the path's namespace |

The check runs again at every trusted admission — before signing, before the
push, and before the pull request — against the objects that exist at that
moment. A continuation, a restart reconciliation, or a direct service call
inherits no earlier answer.

**Ompire never deletes a file or rewrites history to clear this.** The refusal
names the paths and, for retained history, the offending commit. Removing them
from what is published is yours to do, and afterwards the content has changed,
so a fresh review and a new delivery approval are required.

There is no override, force, or declassification. Deleting a working copy,
editing ignore rules, or accepting another result does not lift the
restriction. It is a destination-path contract, so it also does not catch text
deliberately copied or renamed into unrelated source files — ordinary code
review still has to.

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
| Waiting for your decision | The run is at its approval. The work is reviewed and nothing is published; the answer you give decides what happens. |
| Review | The recorded handoff has not reached an approved review of the current content. |
| Draft | Review is approved; publication text and an ending are still to be chosen. Only for a workflow that declares no publication of its own. |
| Deliver | A delivery is authorized or stopped and can be confirmed. |
| Finished without publishing | The run reached a named ending with no privileged effect — the ending it was written to reach. |
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

### 2. Publication text

**A workflow that declares its own publication declares its own text.** Its
approval gate renders a suggested commit message, pull-request title, and body
from the same frozen evidence the question was asked against; they arrive as
editable fields beside the decision, and what you confirm is what is published.
`POST /api/tasks/{id}/ship/draft` is refused for such a task — asking an agent
for a draft during an approval wait would be a turn nobody declared, changing
the very content the decision is about.

For a workflow that declares no publication of its own, drafting works as
before. `POST /api/tasks/{id}/ship/draft` is an idempotent **ensure draft**
command: it asks the task's primary session for a commit message and
pull-request title and body only when no ready draft exists, and repeated
bodyless requests return the existing draft without another agent turn. Send
`{"replace": true}` to regenerate.

`PUT /api/tasks/{id}/ship/draft` stores text written by hand, and is available
either way. Text is inert: it is editable at any time, it selects nothing, and
it authorizes nothing.

Agent drafting is best-effort. A transport error, timeout, missing agent text,
or invalid markers leaves the fields intact and records a retryable draft
error. A daemon restart during a draft turn records it as **interrupted**:
nothing is sent again on the operator's behalf, and the retry is explicit.

### 3. Deliver

Answer the question the run is asking. Each publishing answer names the exact
actions it authorizes; nothing is preselected, and the commit mode is what the
workflow declared rather than something chosen here. Review the resolved
preview, then confirm — the confirmation names every effect it permits.

While the run waits for that answer, daemon-managed writers are refused: a turn
started here would change the content the decision is about. Use the question's
own "request changes" answer instead, which is what the author declared for
exactly that.

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

Completed actions show their concrete results, read from the delivery journal
rather than from what the workflow calls its ending: the signed tip and commit
count, the pushed branch and head, or the pull-request link. A delivery that
ended before a pull request says so plainly — it is complete, not truncated.

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

For a workflow-authorized chain, that continuation is the run's own. An effect
that is proven to have happened is **adopted**: the step it belongs to finishes
with that recorded result and the run carries on, without repeating anything.
Otherwise the run holds the same attempt open and waits — the grant still
stands, and nothing is retried on your behalf. Confirm the remaining work
against a fresh preview of the same delivery, and the run resumes from where it
was rather than opening a second attempt at the same effect.

## Upgrading from an earlier Ompire

Existing review iterations and pull-request records are preserved and never
rewritten. Reviews recorded before content binding carry no candidate: they stay
visible as history and require a fresh review before a new delivery. A task that
shipped before the delivery journal existed shows its pull request as a known
fact with no recorded authorization behind it, which is exactly what is true.

**A task pinned to a workflow that declares no publication cannot publish.**
Older definitions have no step that could, and nothing infers one from the
workflow's name, a completed run, or a historical approval. Ship flow says so
rather than offering an ending it would then refuse. The path forward is to
launch a new task from a definition that declares the delivery you want; the
old task keeps its work, its history, and its workspace.

A delivery authorized *before* this rule existed is the one exception: it may
still finish the prefix it was actually granted, under the same content and
policy checks. It cannot be extended, and no new such grant can be created.

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
