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

A workflow definition is a **document**, not code: a bounded YAML subset
declaring sequential `agent`, `command`, `decision`, `gate`, `review`, and
`delivery` steps, with a content-derived revision as its identity. The document carries its own
semantics version, so a change to what a retained document *means* is a new
format rather than a silent reinterpretation; two versions execute side by
side. Workflow state and step records are
durable; in-memory runners re-drive them after a restart.

The definition and the engine are separate on purpose. `workflow_definitions.py`
answers "what does this document mean" — data model, loader, canonical
identity, bounded evaluator — and imports nothing from the registry or the task
model. `workflows.py` answers "how is that carried out".

The engine consumes no model of its own. When the evidence a step or a route
needs is missing or unreadable, the run stops at that attempt with the reason
recorded, rather than asking a model to classify it. An operator retry re-enters
the blocked step; it never continues past it.

### Evidence is bound to the attempt that used it

A step's result is a *declared* one — a name its definition listed, carrying
the artifact fields that name promised — so a workflow can distinguish "could
not reproduce" from "something went wrong" instead of encoding both as a
failure flag. A declared negative is data and follows its own route.

Which prior attempts a step was handed is resolved once, when the attempt
opens, and recorded on it. That is what makes a finished run explainable: the
history says which reproduction a fix was given and which fix a verification
checked, by attempt rather than by recency, so a stale approval is detectable
rather than merely unlikely.

### A human transition is a committed decision

A gate is a question with declared answers, and the question is persisted
before anyone can answer it — so a decision stays readable after the definition
changes. Answering commits the choice, the gate's completion, and either the
next attempt or the run's named ending in one transaction, and only then wakes
the run. The alternative, acknowledging first and advancing after, loses a
decision a person already made to any crash in between.

Human edges are ordinary routes: they pass through the same visit bounds, so a
loop built out of answers is as finite as one built out of results, and no
answer grants authority the definition did not declare.

### The workflow owns authority; the trusted services own the operations

Review and each privileged publication effect are typed steps
([ADR-0033](../../adr/0033-scope-trusted-delivery-authority-to-the-workflow-run.md)),
and three separate things have to line up before one happens:

- the **definition requests** it — a `delivery` step naming one effect, its
  predecessor, and the approval that can permit it;
- a **person grants** it — an approving choice naming the exact contiguous
  chain, against a preview of the real content, destination, and identities;
- a **trusted service performs** it — `ReviewManager` and `ShipManager` remain
  the only owners of capture, signing, push leases, forge writes, and
  reconciliation.

The runner decides *when* an operation is eligible and never learns how one is
carried out; `runauthority.py` answers "may this happen now" from durable state
alone, for the runner, REST, Ship flow, and any direct service caller alike, and
its refusals are what the UI renders. A caller supplies identities and expected
versions, never a verdict.

Two transactions span both registries: a gate answer with the grant it produces
and the run's move to its first action, and a succeeded action's journal result
with the step transition it produces. Each effect is linked to the attempt that
asked for it *before* it happens, so an interrupted one is adopted rather than
repeated.

See [ADR-0008](../../adr/0008-model-tasks-as-workflows-over-named-sessions.md),
[ADR-0028](../../adr/0028-retain-declarative-workflow-revisions.md),
[ADR-0029](../../adr/0029-declare-domain-outcomes-and-evidence-handoffs.md),
[ADR-0030](../../adr/0030-commit-human-decisions-before-advancing.md),
and [ADR-0033](../../adr/0033-scope-trusted-delivery-authority-to-the-workflow-run.md).

## A task executes the definition it accepted, not the one deployed today

The task's pinned revision is the *only* way a runtime consumer resolves its
workflow: the runner, recovery, session admission, the primary session behind
review and shipping, and the REST and WebSocket projections all go through
`taskdefinition.py`. Looking the workflow's name up in the library is reserved
for two prospective questions — what a new launch would pin, and what an old
task is offered as a continuation candidate.

Retained revisions are append-only and hold the whole document, not just its
identifier: a definition nobody could still read would not explain anything. A
stored row is decoded, re-validated, and re-hashed back to its key before it is
executed, and a row that fails is reported as unavailable rather than run.
Blocking is per task — one damaged row must not take the dashboard with it.

The migration is deliberately incomplete by itself. Every pre-upgrade task
recorded a workflow *name*, and no honest value exists for its revision, so the
binding is null until a person confirms a continuation against a compatibility
check.

## Operators own the library; the daemon owns the revisions

Above those append-only revisions sits one mutable row per workflow name: the
raw draft text an operator is editing, the current revision a new launch would
pin, an archive flag, and an edit version
([ADR-0031](../../adr/0031-let-operators-own-a-workflow-library-above-retained-revisions.md)).
Packaged definitions are read-only entries in that same table, synchronized at
startup; duplication is how they are customized.

Three properties are what the split buys, and each is easy to lose by
collapsing it back:

**A draft is inert.** Any text is stored, nothing parses it, and only an
explicit executable save validates — the exact text it was handed, not one an
earlier validation blessed. So a bad paste cannot reach the catalog, cannot
displace a launchable revision, and cannot keep the daemon from starting.

**The edit version is not the content revision.** A revision is a digest of
normalized semantics, so a comment-only change does not move it; concurrency
safety has to be about edits. Every mutation submits the version it loaded, the
comparison happens inside the write reservation that performs the write, and a
conflict changes nothing. There is no force and no automatic merge.

**Selection is transactional.** There is no process-local catalog. A launch
resolves the name through the connection it was given, which at acceptance is
the one holding the write reservation, so an archive or a save committing
alongside cannot land between the check and the pin. An archived, draft-only,
or unreadable entry refuses the launch and says which — it never substitutes
another revision.

Clients receive the library the way they receive every other registry: a
snapshot plus one full-entry upsert per committed change, ordered by the edit
version, with the launch catalog derived from the same payload.

## A launch is resolved once and pinned to the task

Starting a task is three choices — a workflow, a project, and a model profile
— with no saved preset in between. The project supplies workspace and prompt
defaults a task may override; the profile binds four abstract model roles to
concrete models, and a workflow step names a role rather than a model.

Preview and acceptance run the same resolution, and acceptance re-runs it under
the registry's write reservation and compares the reviewed fingerprint, so what
was approved is what is stored. The result is one immutable document on the
task, and every later stage — the spawn pipeline, the engine, recovery, review,
shipping — reads it instead of re-reading mutable configuration. Editing a
project or a profile changes the next launch and nothing already accepted.

The document pins the workflow revision and one complete binding per *model
consumer* — every declared agent step, and there are no others — rather than one
policy per task, so a step can be sent to a different profile or role without
touching the workflow or anything else. Runtime lookup is exact and fails
closed; there is no task-wide fallback to substitute.

Templates were the previous form. Retiring them meant an upgrade that
preserves every old value as inert evidence and asks the operator wherever it
would otherwise have had to guess.

See [ADR-0026](../../adr/0026-resolve-launch-inputs-once-and-pin-them-to-the-task.md)
and [ADR-0027](../../adr/0027-hand-off-model-policy-between-turns.md).

### What a launch decides, and what a session remembers

Immutable launch intent and mutable applied state are different facts, and
keeping them apart is what makes a restart honest.

The task says what each consumer *may* run. The session records what actually
took effect — its last verified policy, written before the turn that depends on
it. Two steps sharing a session can pin different bindings, so nothing but the
session can answer "what was this conversation configured with", and a
step-start record is not an answer: a configuration can fail after the step
opened.

Putting a live session on the next consumer's policy is a supervised handoff at
a turn boundary. Changing only the active pair is done in place over omp's
acknowledged controls; changing an auxiliary role requires replacing the
process, because those are start-time flags — so the native session is resumed
under the same identity and the conversation carries over. Nothing is
interrupted, nothing falls back, and a failure leaves no process that may be
prompted.

See [ADR-0027](../../adr/0027-hand-off-model-policy-between-turns.md).

## Attention is derived centrally

One state machine interprets agent lifecycle into a session status. One pure
function maps status to an attention tier. Task attention aggregates across
sessions and gates. Clients render the result.

The alternative — each component deciding when to shout — produces a system
that notifies constantly and is therefore ignored.

See [The attention model](../../use/explanation/attention.md).

## Review and publishing sit outside the sandbox

Review runs on the host side, so the reviewed agent cannot mediate its own
verdict. The daemon performs the signed commit, the push, and the pull-request
creation with host-side credentials.

*When* either happens is the workflow's, not the page's: a definition that
declares review starts it at the step that declares one, and publication
happens only where a `delivery` step says so and a person has authorized that
exact chain. A definition that declares neither can do neither — which is what
older retained revisions are, and the trusted service refuses rather than
inferring authority nobody wrote down.

Both are bound to *content* rather than to a task's live state
([ADR-0032](../../adr/0032-bind-trusted-delivery-to-retained-candidates.md)).
Review captures a candidate — the whole publishable delta, identified by a hash
of its normalized content — into an owner-private repository, and reads an
isolated checkout of it. Signing then happens against that same candidate, and
the result is installed into the clone under a compare-and-swap against the HEAD
it was captured at. An agent that keeps working therefore cannot change what is
under review or what gets signed; it can only make its own approval visibly
unusable.

Publishing is three independently admitted operations — commit, push, pull
request — and how far a delivery goes is the chain the workflow declared and a
person authorized, fixed at the moment they answer rather than extended
afterwards. Every attempt journals what it intends to write before it runs, so
an interrupted sequence is reconciled against the specific result it was going
for rather than repeated or assumed lost.

See [Why the control plane is trusted and the agent is not](trust-model.md).

## Where the design is unsettled

Documentation that only described the intended architecture would mislead. Two
areas are known-unreconciled and tracked in `ADR.PLAN.md`:

**[The durability boundary](../../adr/0016-persist-authority-bearing-task-history-and-provenance.md).**
Workflow steps, session identity, tasks, settings, PR state, review history and
reports, and delivery authorization, intent and outcomes are durable, each
linked to the run attempt that produced it. Session status and
attention state are not, and neither is full commit lineage or transcript
retention — which is the part ADR-0016 still names and this design has not
reached. The gap is narrower than it was, not closed.

**[Publishing identity](../../adr/0017-use-dedicated-bot-as-default-publishing-identity.md).**
Shipping currently inherits the host identity and is documented as producing
operator-authored signed commits. ADR-0017 proposes a dedicated bot as the
default automation identity.

Each needs an explicit decision rather than a silent choice. Do not resolve one
incidentally while implementing something else.
