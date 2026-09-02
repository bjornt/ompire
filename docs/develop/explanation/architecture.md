# Architecture overview

Ompire is a Python daemon that owns everything consequential and a React
frontend that owns nothing. Almost every structural decision follows from
that split, so it is the thing to understand first.

## The shape

```text
  browser                     daemon (trusted)              per task
  ┌──────────┐  REST      ┌────────────────────┐        ┌──────────────┐
  │  React   │ ─────────▶ │  commands          │        │  clone       │
  │  UI      │            │  registry (SQLite) │ ─────▶ │  container   │
  │          │ ◀───────── │  supervision       │        │  agent       │
  └──────────┘  WebSocket │  review, publish   │ ◀───── └──────────────┘
                snapshot  │  credentials       │  stdio NDJSON
                + deltas  └────────────────────┘
```

## The frontend owns nothing

The React layer is presentation only. It holds no authoritative state, makes
no decisions, and can be closed and reopened at any point without affecting
running work.

This is what makes the rest tractable. Because the daemon is the only place
state lives, there is no reconciliation problem, no split-brain, and no
question about which side is right after a disconnect. The client re-reads a
snapshot and renders it.

It also means the frontend is untrusted in the same sense the agent is: it
receives what the daemon chooses to send and can request what the daemon
chooses to allow.

See [ADR-0002](../../adr/0002-run-as-local-daemon-with-stateless-web-ui.md).

## Commands and observation are separate

REST for anything that changes state. WebSocket for observing it — an
authoritative snapshot, then deltas.

Keeping mutation off the socket means connection state can never influence
daemon state. A reconnecting client cannot replay a command, and there is one
path to audit for mutations rather than two.

Raw agent transcript events use separate per-session channels, so a dashboard
watching ten tasks does not receive every frame of ten agents.

See [ADR-0004](../../adr/0004-use-rest-and-websocket-snapshot-deltas.md) and
[WebSocket protocol](../reference/websocket-protocol.md).

## Python, and a small dependency set

The control plane is Python 3.12 with asyncio, FastAPI, Pydantic, SQLAlchemy,
and Alembic. Bun/TypeScript and Go were both considered; operator
auditability decided it.

Everything in the daemon sits inside the trust boundary. A dependency added
here is a dependency that handles credentials, so the set stays deliberately
small.

See [ADR-0003](../../adr/0003-implement-trusted-control-plane-in-python.md).

## State is explicit SQLite

One owner-private SQLite database in WAL mode, accessed through SQLAlchemy
Core rather than an ORM, with reviewed Alembic migrations applied at startup.

Core rather than an ORM is the notable choice: queries and schema behavior
stay visible at the call site, which matters more here than the convenience an
ORM buys, because this is the state that has to be correct after a crash.

See [ADR-0005](../../adr/0005-persist-local-state-with-sqlite-core-and-alembic.md)
and [Database schema](../reference/database-schema.md).

## Every task is isolated

Each task gets a local hardlink clone of the project checkout and its own
container.

Git worktrees were rejected deliberately. They share writable Git metadata
with the main repository, so work inside one can affect state outside it, and
they are not self-contained at a container mount path. A clone is heavier and
correct.

Cleanup removes the container before deleting the clone, and refuses any path
outside the configured task root.

See [ADR-0006](../../adr/0006-give-every-task-a-separate-clone-and-workshop.md).

## Agents are supervised child processes

One agent process per named session, supervised over stdio NDJSON. Requests
are correlated by ID, and push events interleave freely with responses.

Frames are treated as opaque by default. Only the fields orchestration
actually needs — asks, approvals, lifecycle, state — are validated. This keeps
the daemon from breaking every time the agent's frame vocabulary grows.

ACP, PTY scraping, and an in-process SDK were the alternatives.

See [ADR-0007](../../adr/0007-use-native-omp-rpc.md).

## Tasks execute workflows over sessions

The task is the top-level unit of work, not the agent session. Sessions are
resources a workflow uses, addressed as `(task_id, session_name)` and spawned
lazily. One is declared primary, and task-scoped operations — review, ship —
target it.

Workflows are startup-validated Python definitions with sequential `agent`,
`command`, `decision`, and `gate` steps. Workflow state and step records are
durable; in-memory runners re-drive them after a restart.

Python definitions are the current form, not the intended end state.
[`VISION.md`](../../VISION.md) calls for versioned declarative workflows.

See [ADR-0008](../../adr/0008-model-tasks-as-workflows-over-named-sessions.md).

## Attention is derived centrally

One state machine interprets agent lifecycle into a session status. One pure
function maps status to an attention tier. Task attention aggregates across
sessions and gates. Clients render the result.

The alternative — each component deciding when to shout — produces a system
that notifies constantly and is therefore ignored.

See [The attention model](../../use/explanation/attention.md).

## Review and publishing sit outside the sandbox

Review runs against the host side of the clone, so the reviewed agent cannot
mediate its own verdict. The agent may draft commit and PR text; the daemon
performs the signed commit, the push, and the PR creation with host-side
credentials.

Both operations protect their temporary Git state with durable refs —
`refs/ompire/review-orig` and `refs/ompire/ship-orig` — so an interrupted
sequence is restorable at the next startup rather than lost.

See [Why the control plane is trusted and the agent is not](trust-model.md).

## Where the design is unsettled

Documentation that only described the intended architecture would mislead. Three
areas are known-unreconciled and tracked in `ADR.PLAN.md`:

**[The durability boundary](../../adr/0016-persist-authority-bearing-task-history-and-provenance.md).**
Workflow steps, session identity, tasks, settings, and PR state are durable.
Session status, review history, attention state, and most ship progress are not.
ADR-0016 proposes enough durable history to resume safely and explain external
side effects.

**[Publishing identity](../../adr/0017-use-dedicated-bot-as-default-publishing-identity.md).**
Shipping currently inherits the host identity and is documented as producing
operator-authored signed commits. ADR-0017 proposes a dedicated bot as the
default automation identity.

**Workflow format.** Python definitions are unversioned by design. The vision
calls for versioned declarative workflows.

Each needs an explicit decision rather than a silent choice. Do not resolve one
incidentally while implementing something else.
