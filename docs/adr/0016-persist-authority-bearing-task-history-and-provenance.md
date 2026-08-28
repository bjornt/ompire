# ADR 0016: Persist authority-bearing task history and provenance

- Status: Proposed
- Date: 2026-08-23

## Context

Ompire owns work that crosses several failure and trust boundaries. A task can survive browser disconnection, span daemon restarts, resume agent sessions and workflow steps, obtain human decisions, run independent review, rewrite Git history, push a branch, and create or observe a pull request. Some of those actions are repeatable; others have external effects that cannot be made safe merely by restarting the same code. The daemon therefore needs more than enough current state to redraw the UI. It needs durable evidence from which it can resume without duplicating authority-bearing effects and later explain what happened.

The current persistence boundary is partial. Task identity and lifecycle, selected workflow name and current position, workflow step records and structured outcomes, named agent-session identities, settings overrides, pull-request URL, and observed pull-request state survive daemon restart. Recovery can resume native agent sessions and re-drive selected workflow steps. Those records already demonstrate the value of keeping semantic execution state in the control-plane database rather than reconstructing it from browser events.

Review status and iteration history are durable: they survive restart and task cleanup, and an open review whose reviewer died with the daemon is reconciled into an explicit interrupted outcome rather than disappearing. That is one slice of the boundary below, not the whole of it. Other facts with equal or greater authority remain transient. Live session state, pending agent questions and approvals, ship drafts and progress, the rewritten commit identity, publishing errors, and most attention state exist only in memory. Raw agent events have a bounded live buffer. Native session material remains coupled to the task environment and is removed with it. A successful commit, push, or pull-request creation can occur before the corresponding durable task update, leaving restart recovery unable to distinguish "not attempted" from "completed but not recorded." Git recovery refs protect a temporarily rewritten clone, but they are not an audit history and disappear with workspace cleanup.

This conflicts with the durable product direction. A task is intended to own durable runs, artifacts, review history, publishing state, human decisions, session evidence, and commit lineage. That history must explain privileged actions after workspace cleanup, retain attribution through Git rewriting, and prevent blind replay of non-idempotent effects. Persisting every protocol frame or UI projection would not solve the problem cleanly: much of that data is derived, high-volume, or sensitive, while an unstructured log alone cannot provide the explicit recovery checkpoints needed at an external side-effect boundary.

The conflict is only partly resolved in the implementation. This ADR records the proposed durability boundary and remains proposed until authority-bearing publishing, decision, evidence, and provenance records also drive recovery and survive task cleanup.

## Decision

Ompire persists the semantic history and provenance needed to resume, authorize, and explain a task. Current-state rows may remain the efficient source for snapshots, but they are supplemented by durable, attributable records for authority-bearing transitions and evidence. In-memory managers, WebSocket events, process handles, and workspace files are projections or execution mechanisms; none may be the sole record of a fact required for safe recovery or later explanation.

For each task and workflow run, the durable record includes, when applicable:

- the accepted input and effective execution snapshot, including an immutable workflow identity once workflow versioning is available;
- every step attempt, its producing session, state transitions needed for recovery, structured outcome, errors, timestamps, and declared artifacts or evidence;
- human questions, answers, approvals, denials, gate resolutions, and the actor or authority responsible for each decision;
- independent review attempts, verdicts, findings, evidence identity, and the transition that allowed or refused publishing;
- agent-session identities and durable references to the transcript and tool-event evidence retained outside the disposable task environment;
- each privileged or non-idempotent operation's intent, authorizing actor, execution identity, stable correlation or idempotency data, observed result, error, and reconciliation status; and
- Git and forge lineage from the task's base and workspace checkpoints through rewritten, pushed, pull-request, and reconciled mainline commit identities, using content fingerprints where a changed SHA alone cannot preserve correlation.

Before invoking a privileged external effect, the daemon durably records enough intent and correlation data to identify that operation after a crash. It durably records the observed result before advancing to a dependent operation. Recovery consults those records and reconciles them with local Git and the external system. It may retry only when the operation is intrinsically idempotent, carries an effective idempotency key, or can be proven not to have happened. If completion cannot be proved or safely adopted, recovery stops at a human gate rather than repeating the action or guessing its outcome.

Not every live signal is durable. Process presence, stream buffers, transient tool activity, progress animation, notification delivery, attention tiers, and other pure projections may be reconstructed from durable facts and current processes. High-volume transcripts and tool-event logs may use an owner-private artifact store rather than relational rows, but their identity, integrity metadata, producing task, run, step, and session, retention state, and location are durable control-plane records. Semantic facts needed for attribution may not exist only inside an opaque transcript.

Task cleanup may remove the clone, Workshop, and native session storage only after required evidence has been captured or explicitly marked unavailable and every authority-bearing external operation is terminal or reconciled. Durable history is removed only through an explicit retention or purge operation. Missing legacy history and failed captures remain explicit gaps; Ompire never invents provenance by inference.

The invariant is that a daemon restart or task-workspace cleanup cannot erase a fact needed to decide whether an authority-bearing action is safe to perform, identify who authorized and executed it, or trace its inputs and outputs to the resulting external state.

## Consequences

Recovery becomes evidence-driven. Completed step attempts and privileged effects are not replayed merely because their in-memory manager disappeared. An interrupted operation can be reconciled against a durable intent and external identifier, while an ambiguous operation stops visibly. This reduces duplicate commits, pushes, comments, and pull requests and makes crash behavior independent of whether a browser observed the original events.

Task history remains useful after cleanup. Operators can relate a pull request or known commit to its task, run, steps, agent sessions, structured outcomes, review evidence, human decisions, publishing identity, and later mainline result even when Git rewriting changed commit SHAs. Persisted authorization and execution identities also make unattended and human-gated runs distinguishable without trusting agent-authored prose.

The database schema and artifact lifecycle become broader. Review, decision, publishing-operation, artifact, and provenance records require stable identities, migrations, transaction boundaries, retention behavior, and snapshot or history APIs. Transcript and tool logs are sensitive and potentially large, so the owner-private data boundary must cover the database, artifact store, backups, exports, and purge paths. Integrity metadata detects loss or substitution but does not make untrusted agent evidence truthful.

Write-ahead operation records narrow but do not eliminate distributed-systems ambiguity. A process can still fail between the external effect and recording its response. Recovery therefore needs operation-specific reconciliation against Git or the forge and a human path where no reliable query or idempotency mechanism exists. New privileged integrations must define that reconciliation contract before they are allowed in an unattended workflow.

The durability boundary deliberately excludes ephemeral projections. Session attention and live progress may reset or be recomputed after restart without corrupting history. If a currently derived value later becomes an authorization input or the only explanation for a transition, it crosses the boundary and must become durable before that behavior ships.

Migration is forward-only and explicit. Existing task, workflow, session, and pull-request rows seed the facts they actually contain. They do not justify backfilling missing review verdicts, human decisions, commit lineage, or transcript evidence from mutable workspaces or forge guesses. Historical tasks are marked as having incomplete provenance where appropriate. New durable writers are introduced before recovery or cleanup begins relying on their records, and schema migrations preserve existing task history. Until the full boundary is implemented, authority-bearing flows that lack a durable intent and reconciliation contract remain restart-sensitive and this ADR cannot become accepted.

This decision should be revisited if Ompire stops owning privileged external effects, if an external workflow system becomes the authoritative durable run and provenance store, or if storage volume requires a different database or artifact backend. A replacement must preserve explicit authority, safe recovery, post-cleanup traceability, visible uncertainty, and owner-controlled retention.

## Alternatives considered

### Persist only latest task state

Keeping one mutable row per task and selected child rows is compact and makes current snapshots simple. Git, native agent sessions, and the forge could be queried when more detail is needed. It was rejected because latest state cannot explain superseded decisions or intermediate identities, workspace cleanup removes two of those evidence sources, and external queries cannot reliably reconstruct authorization or distinguish an unrecorded success from an unattempted operation.

### Make an append-only event log the sole source of truth

A universal event stream could retain every transition and rebuild current state by replay, giving one chronological audit substrate. It was rejected as the only control-plane model because safe recovery also needs constrained, queryable operation state and explicit uniqueness and transaction rules at authority boundaries. Replaying an evolving untyped or weakly typed log would make migrations and startup recovery harder to audit. Ompire instead keeps explicit current-state records and semantic history; an append-only audit stream may complement them but does not replace them.

### Persist every daemon and agent event

Recording every RPC frame, status sample, WebSocket event, and notification would maximize raw observability and avoid deciding which signals matter. It was rejected because volume, sensitive content, protocol evolution, and derived-event duplication would increase operational cost without guaranteeing semantic attribution. Bounded or archived raw logs remain useful evidence, but durable control-plane records capture the validated decisions, outcomes, authority, and correlations on which recovery depends.

### Retry interrupted effects and reconcile only after an error

Blind retry keeps the implementation small, and force-with-lease or forge "already exists" responses can make common repeats appear recoverable. It was rejected because not every provider offers effective idempotency, an error may not carry the created resource identity, and a repeated action can succeed differently from the first. Durable pre-operation intent plus explicit reconciliation makes ambiguity visible before another privileged effect is attempted.
