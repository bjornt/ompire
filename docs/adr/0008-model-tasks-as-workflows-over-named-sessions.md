# ADR 0008: Model tasks as workflows over named sessions

- Status: Accepted
- Date: 2026-07-17

## Context

An operator asks Ompire to accomplish a task, such as investigating a defect, producing a change, reviewing the result, or publishing approved work. The lifetime and authority of that request are broader than any one coding-agent conversation. A task owns an isolated workspace, may require several agents with different conversational contexts, may execute deterministic commands without an agent, may wait for an operator decision, and may continue through review and publishing after an agent turn ends.

Treating an agent session as the top-level unit conflates conversational state with operational state. It makes multi-role work appear as unrelated jobs, leaves no natural owner for shared workspace changes and deterministic steps, and makes task-scoped recovery, attention, review, and publishing ambiguous. Conversely, using one long-lived agent as an implicit workflow manager would delegate routing and completion claims to an untrusted, probabilistic component when the daemon can make many transitions deterministically.

Ompire therefore needs a task-level execution model that can coordinate several kinds of work while preserving useful agent context. Named sessions provide that context selectively: steps that benefit from a shared conversation reuse a session, while steps that need independence use another session. All sessions under a task operate on the same isolated workspace, allowing files and Git state to remain the primary artifact handoff.

The implemented engine defines workflows in daemon-owned Python and validates their session and step declarations at startup. It currently executes `agent`, `command`, `decision`, and `gate` steps sequentially, persists run state and repeated step records, lazily starts named sessions, and re-drives interrupted runs after daemon restart. Durable product direction also calls for workflow definitions to become versioned and declarative. This ADR does not decide the workflow representation or versioning scheme; it records the task, workflow, step, and named-session ownership model shared by both designs. The representation conflict requires a separate decision before externally authored or mutable workflow definitions are introduced.

The task-workflow model was recorded as an architectural decision on 2026-07-17 and is now implemented. This ADR backfills that accepted decision using its recorded acceptance date.

## Decision

The task is Ompire's top-level unit of operator work and execution ownership. Every task owns one workflow run, its isolated workspace, its execution history, its operator-facing lifecycle, and the task-scoped review and publishing operations that follow from that work. Agent sessions are subordinate resources of the task; a session is never the authority-bearing unit of work by itself.

A workflow declares a set of uniquely named sessions and an ordered set of typed steps. The durable step vocabulary separates these responsibilities:

- an `agent` step performs a bounded turn on one declared session;
- a `command` step executes deterministic work in the task environment;
- a `decision` step chooses an explicit route from recorded evidence; and
- a `gate` step parks execution for an operator decision or acknowledgement.

Workflow execution is daemon-owned. At most one step in a task runs at a time under the current execution model. Successful steps fall through in declaration order unless a decision explicitly selects another declared step; reaching the end completes the run. A later decision may add richer transition or concurrency semantics, but it must retain explicit step state and deterministic daemon control rather than making transcript interpretation authoritative.

Sessions are addressed by `(task, session name)`, declared by the workflow, and spawned lazily on first use. Reusing a name deliberately preserves conversational context across agent steps; choosing a different name creates an independent context. Spawned sessions share the task's clone and execution environment and remain available until task cleanup. A workflow designates one declared session as primary for task-scoped operations that require an agent, such as review feedback or drafting publishing text. Command-only workflows need not start an agent session.

The task records the workflow identity selected for that run. Run status, current step, and an ordered record of each step execution are durable control-plane state. Repeated visits to a step produce distinct records. After a daemon restart, the engine resumes or re-drives an incomplete step from persisted state according to the step kind; completed and failed runs are not silently replayed. Commands that can be re-driven after interruption must therefore be idempotent, and ambiguous outcomes must stop or escalate rather than be guessed.

This ADR does not make the current Python definition format permanent. Replacing it with a versioned declarative format does not supersede this decision if tasks still own workflow runs, steps remain explicit and durable, sessions remain named task resources, and the daemon retains routing and recovery authority.

The invariant is that Ompire orchestrates operator work as a durable task-owned workflow. Agent sessions contribute bounded conversational execution to that workflow; they do not define task identity, own routing, or become the source of truth for task completion.

## Consequences

The operator sees one coherent task even when several agents, commands, decisions, and human gates contribute to it. Workflows can preserve context where it is valuable, isolate it where independence matters, and run deterministic or human-controlled work without inventing an agent session. Task-scoped review, publishing, attention, cleanup, and history have a stable owner independent of which session is active.

Daemon-owned typed steps make authority visible. Deterministic commands and routing do not depend on an agent claiming success in prose, and unresolved evidence can become an explicit gate. The working tree remains the common artifact channel, while durable step records provide execution order, outcomes, errors, and recovery position. These records also let reconnecting clients reconstruct workflow state without replaying every session transcript.

The model adds control-plane complexity. Every task may have several child processes and session identities, while APIs, events, attention aggregation, recovery, and presentation must distinguish task scope from session scope. Workflow authors must choose session reuse deliberately, designate a primary session, keep recoverable commands idempotent, bound loops, and make routing failures visible. Sharing one workspace also means concurrent step execution cannot be added safely without explicit rules for filesystem conflicts, ordering, cancellation, and evidence invalidation.

Persistence makes recovery possible but does not make arbitrary side effects exactly-once. Re-driving an interrupted command or agent turn can repeat work. Authority-bearing external actions such as commit rewriting, push, and pull-request creation require their own idempotency and audit rules and remain outside agent sessions. A workflow failure leaves the task workspace and sessions available for diagnosis rather than equating a failed run with safe cleanup.

Lazy sessions reduce process cost for unused roles and permit workflows containing only deterministic steps. The tradeoff is that session startup can fail partway through a run and must be represented as a step failure. Keeping spawned sessions alive preserves context and supports manual recovery, but consumes resources until task cleanup.

The current in-process Python registry is compact, reviewable, and suitable while workflows are built-in and few. It cannot by itself pin a task to an immutable definition after code changes, safely admit operator-authored definitions, or provide a portable schema. A separate ADR is required before workflows become externally configurable. That decision should introduce versioned snapshots and a constrained declarative or trusted-plugin boundary when mutable workflow authoring, historical replay across upgrades, or third-party workflow distribution becomes a requirement.

This decision should be revisited through a superseding ADR only if the task ceases to be the useful unit of operator intent and authority, if workflows need independently owned workspaces rather than a task-shared workspace, or if required execution semantics cannot preserve explicit task-owned state and daemon-controlled routing. Changing the workflow serialization format alone is not a reason to supersede it.

## Alternatives considered

### Treat each agent session as a task

A one-to-one model is simple: lifecycle, transcript, status, and workspace can all be keyed by one identifier. It matched Ompire's earliest single-agent behavior and remains adequate for a trivial one-turn workflow. It was rejected as the architecture because reproducing, fixing, validating, reviewing, and publishing may need different contexts and non-agent steps while still belonging to one operator request and one workspace. Making each session a task would require another orchestration object above tasks or would fragment recovery, attention, and publishing across unrelated records.

### Let a manager agent orchestrate other agents and tools

A manager session could choose subagents, interpret their transcripts, retry work, and report completion with little daemon machinery. It was rejected for authority-bearing orchestration because routing and completion would depend on probabilistic interpretation inside the same sandbox that performs the work. Daemon-owned typed steps allow deterministic commands, explicit evidence, durable recovery, and human escalation without preventing an agent step from performing open-ended reasoning where that is useful.

### Model workflows as arbitrary graphs or parallel jobs immediately

A general graph or concurrent scheduler can express fan-out, joins, and complex transitions directly. It was rejected for the current architecture because the implemented workflows are predominantly linear with a small number of explicit decision jumps, while parallel steps sharing one mutable workspace introduce conflict, cancellation, and evidence-ordering semantics that the product has not defined. Sequential execution is the safer initial invariant. A future superseding decision may add structured concurrency when a concrete workflow requires it and the workspace and recovery rules are explicit.

### Require a versioned declarative workflow format immediately

Declarative, immutable workflow versions would improve validation, portability, auditability, and safe operator configuration. That remains the durable direction, not a rejected goal. It was deferred because the initial built-in workflows were needed to discover the stable step and outcome contracts, and introducing a DSL before those contracts existed would freeze speculation into a public format. The current Python representation is acceptable only while definitions are trusted, built in, startup-validated, and changed with the daemon. The representation and versioning decision must be reconciled separately before that boundary expands.
