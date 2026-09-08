# States

Every state Ompire reports, and what it means.

## Task states

| State | Meaning |
|---|---|
| `created` | The task exists. Spawning has run or is running. |
| `failed` | A spawn step failed. The step name and its stderr are attached. |
| `archived` | Cleaned up. The clone and container are gone; the record remains. |

Task state is durable and survives a daemon restart.

A task also reports whether its launch configuration still needs confirming.
Tasks accepted before launch inputs were pinned to the task carry no recorded
model policy or workspace inputs, so they are shown as needing configuration
until you confirm one. That is not a task state of its own and never becomes
`failed`: the task keeps its position, sessions, workspace and history. While
it is pending, automatic recovery skips the task and continuing, prompting,
reviewing and shipping it are refused; reading, inspecting and cleaning it up
are not. Archived tasks stay readable and need no confirmation. See
[Task detail](task-detail.md) for the confirmation itself.

Projects report launch-configuration reconciliation separately, and separately
again from checkout setup — see [Projects](projects.md). A project awaiting
reconciliation cannot start new tasks; other projects are unaffected.

## Session statuses

A task runs one agent process per workflow-declared named session, addressed
as `(task_id, session_name)`.

| Status | Meaning |
|---|---|
| `starting` | The process is launching, or is being resumed after a restart. |
| `working` | The agent is in a turn. |
| `idle` | At a turn boundary, awaiting the next instruction. |
| `waiting-input` | The agent asked a question and is blocked on your answer. |
| `waiting-approval` | The agent requested approval for an action. |
| `reviewing` | A review is open against this session's task. |
| `stalled` | Silent past the stall threshold. A heuristic, not a fact. |
| `retrying` | A step is being retried within its declared bound. |
| `failed` | The process died or failed to start. |

Two properties are worth knowing:

- **Session status is in-memory.** It does not survive a daemon restart. After
  a crash it is rebuilt from recovery, not replayed.
- **Exit wins.** Every transition is guarded, so a process exit during the idle
  debounce or a late frame after teardown resolves deterministically rather
  than racing.

`reviewing` is the exception to the pattern: it is driven by the review
manager rather than by agent activity, entered only from `idle` on the task's
primary session.

## Attention tiers

Session statuses map to exactly one attention tier. The mapping is a pure
function, applied once, centrally — clients render the result rather than
inventing their own.

| Status | Tier |
|---|---|
| `starting`, `working` | `silent` |
| `idle`, `retrying` | `badge` |
| `waiting-input`, `stalled`, `reviewing` | `notify` |
| `waiting-approval`, `failed` | `interrupt` |

What each tier does:

| Tier | Desktop | Sound | Badge |
|---|---|---|---|
| `silent` | no | no | no |
| `badge` | no | no | yes |
| `notify` | yes | no | yes |
| `interrupt` | yes | yes | yes |

These are defaults, not fixed behavior. All twelve cells are settings —
`tier.<tier>.<channel>` — changeable from the UI without a restart, and read
at fire time so a change applies to the next transition.

An unrecognized status defaults to `silent`. The mapping fails closed: Ompire
would rather stay quiet about something new than over-notify.

Task attention is the highest tier across the task's sessions and any open
gate. Ranking is `silent` < `badge` < `notify` < `interrupt`.

An unanswered `notify` or `interrupt` entry re-notifies at the re-notification
interval until it is dealt with.

## GPG states

| State | Meaning | Shipping |
|---|---|---|
| `ready` | The selected key can sign now — cached, or unprotected | Allowed |
| `locked` | Protected key present, passphrase not cached | Refused |
| `ambiguous` | Several usable signing keys, none selected | Refused |
| `no_key` | No signing-capable secret key in the keyring | Refused |
| `missing` | `gpg` or `gpg-connect-agent` is not executable | Refused |
| `agent_unavailable` | The tools run but `gpg-agent` is unreachable | Refused |
| `error` | Any other indeterminate result; carries a reason | Refused |
| `unknown` | No probe has completed yet | Refused |

Only `ready` allows a signed commit; every other state fails closed and carries
its own recovery action. An unprotected key is `ready` rather than `locked`: it
has nothing to cache. See [GPG signing](gpg-signing.md).

Signing readiness is required for the commit action alone. Pushing an existing
verified signed result, or opening a pull request for one, needs no signing key.

## GitHub states

GitHub state is a current daemon observation held only in memory. Restarting
the daemon begins at `unknown` and probes again; a failed recheck replaces a
previous ready result rather than leaving stale authorization visible.

### Identity

| State | Meaning |
|---|---|
| `unknown` | No GitHub CLI check has completed. |
| `missing` | The configured GitHub CLI executable cannot run. |
| `unauthenticated` | The CLI runs but its effective credential is missing or rejected. |
| `ready` | `gh api --hostname github.com user` safely returned the selected login. |
| `error` | A timeout, network error, malformed response, or other indeterminate check result occurred. |

### Repository eligibility

Each canonical `host/owner/repository` result is bound to the host, login, and
credential-source tuple that produced it.

| State | Meaning | Endings that push |
|---|---|---|
| `unchecked` | The target has not been checked under a ready identity. | Refused |
| `allowed` | Read-only repository, pull-request policy, and effective-access checks passed. | Allowed |
| `denied` | The known account cannot use the registered upstream target. | Refused |
| `error` | Target response or eligibility evidence was incomplete or indeterminate. | Refused |

GitHub availability gates only the endings that reach the forge. A local signed
commit is unaffected by every state in this table.

This check is GitHub **API** identity and repository eligibility. It is not
proof of the SSH key or HTTPS credential `git push` uses, and Ompire records
that transport identity as explicitly unattributed rather than inventing one.

## Where a run stands with publication

Four things are distinct, and Ompire reports them separately rather than
collapsing them into one "shipped or not"
([ADR-0033](../../adr/0033-scope-trusted-delivery-authority-to-the-workflow-run.md)):

| Observation | Meaning |
|---|---|
| Working | The run is executing its own steps. Nothing has been reviewed or authorized. |
| Waiting for review | A `review` step is running: an independent reviewer is reading the protected candidate. |
| Waiting for your decision | The run is at an approval. The work is reviewed, nothing is published, and the answer you give decides what happens. |
| Published, so far as it went | One or more privileged effects are on record — a signed commit, a pushed branch, a pull request. |

And two endings that are complete rather than stalled:

| Observation | Meaning |
|---|---|
| Completed work without publication | The run reached a named ending with no privileged effect, because that is the ending it was written to reach or the answer a person gave. |
| Blocked publication | Something stopped a publication that was authorized. Its reason and the eligible recovery action are shown. |

A run's own result name — `validated`, `published`, `stopped-unpublished` — is
the *workflow's* word for its ending. It is never what says an effect happened:
whether anything was signed, pushed, or opened is read from the delivery
journal, which records what actually occurred.

## Delivery states

A delivery is the operator's authorization to publish one reviewed candidate as
far as the chain their answer named. Its disposition is durable
([ADR-0032](../../adr/0032-bind-trusted-delivery-to-retained-candidates.md)):

| Disposition | Meaning |
|---|---|
| `open` | A record exists with nothing authorized. |
| `authorized` | A confirmation stands and actions remain. |
| `completed` | Every action the answer authorized is on record. Nothing further can be authorized for this run. |
| `blocked` | A safe stop: nothing is in an unknown state, and a fresh preview and confirmation may continue. |
| `unresolved` | An effect's outcome could not be established. Nothing dependent runs and cleanup is refused. |
| `abandoned` | The operator granted no further authority. |

Each action attempt has its own phase: `prepared`, `executing`, `succeeded`,
`failed`, or `needs_reconciliation`. `failed` means Ompire established that the
effect did not happen; `needs_reconciliation` means it could not tell, which is
neither success nor failure.

An attempt also names the workflow step that asked for it, so an effect belongs
to a decision rather than to a task in general. An interrupted attempt whose
effect is proven to have happened is *adopted* by that step; one whose effect
did not happen waits for an explicit continuation, keeping the grant it already
has.

## Pull-request states

A task that opened a pull request records its URL, state, and merge time.
Ompire polls until the pull request reaches a terminal state. Tasks are
considered active while their pull-request state is unset or `open`. A delivery
that ended earlier has no pull request and is not polled.

## Advisories

Advisories are observations, not statuses. They ride alongside a session
without changing it — `context-high` fires when context use crosses the
configured threshold. They are advisory precisely because acting on them is
your decision.
