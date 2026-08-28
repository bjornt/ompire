# States

Every state Ompire reports, and what it means.

## Task states

| State | Meaning |
|---|---|
| `created` | The task exists. Spawning has run or is running. |
| `failed` | A spawn step failed. The step name and its stderr are attached. |
| `archived` | Cleaned up. The clone and container are gone; the record remains. |

Task state is durable and survives a daemon restart.

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
| `cached` | Passphrase cached in the agent | Allowed |
| `locked` | Key present, passphrase not cached | Refused |
| `unknown` | Key, agent, or probe unresolved | Refused |

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

| State | Meaning | Shipping |
|---|---|---|
| `unchecked` | The target has not been checked under a ready identity. | Refused |
| `allowed` | Read-only repository, pull-request policy, and effective-access checks passed. | Allowed with a ready GPG gate |
| `denied` | The known account cannot use the registered upstream target. | Refused |
| `error` | Target response or eligibility evidence was incomplete or indeterminate. | Refused |

## Pull-request states

A shipped task records its pull-request URL, state, and merge time. Ompire
polls until the pull request reaches a terminal state. Tasks are considered
active while their pull-request state is unset or `open`.

## Advisories

Advisories are observations, not statuses. They ride alongside a session
without changing it — `context-high` fires when context use crosses the
configured threshold. They are advisory precisely because acting on them is
your decision.
