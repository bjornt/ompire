# ADR 0009: Use structured, Git-excluded files for agent-step outcomes

- Status: Proposed
- Date: 2026-08-22

## Context

An agent turn has two distinct products. It may change the shared working tree, which is the primary handoff between workflow steps, and it may report a bounded result needed for routing, recovery, history, or a later prompt. A transcript is useful evidence but is not a reliable result interface: prose varies by model and prompt, completion language can be ambiguous, and reconstructing a result from an event stream after a restart would make control-plane behavior depend on heuristics.

The result interface must work through the agent's native session without adding a manager-only RPC tool or coupling the daemon to provider-specific model APIs. It must survive session resume and remain accessible from both the sandbox and the host side of the task clone. At the same time, control-plane handoffs must not enter the task's source diff, automated review input, commit, or pull request.

The implemented convention asks outcome-bearing agent steps to write a versioned JSON document at a fixed path under a daemon-owned, per-clone directory. The daemon removes stale output before a new prompted step, validates a fresh document at the turn boundary, and persists the parsed result with the step record. Missing and malformed documents remain explicit rather than becoming implicit success. Decision functions consume those records before any semantic classification is attempted.

The implementation and current workflow documentation also use an engine-reserved LLM session as an undeclared fallback when an outcome is absent or a deterministic decision cannot route. The fallback's transcript is inspectable and its synthesized result is marked on the affected step, but the judgment has no workflow step record of its own. Durable product direction requires every semantic judge to be a declared workflow step whose inputs, output, confidence, and routing effect are recorded, and says such a judge must never be a hidden fallback. This conflict does not change the structured-file boundary, but it prevents acceptance of the complete routing policy in this ADR until judge declaration and provenance are reconciled.

This ADR is therefore a proposed backfill of the implemented outcome contract. Its date is the record's creation date, not an invented historical acceptance date.

## Decision

Outcome-bearing agent steps communicate their terminal result through a fresh, versioned JSON document at `.ompire/outcome.json` in the task clone. The daemon owns the convention and the containing `.ompire/` namespace. Each task clone must exclude `.ompire/` through its clone-local Git exclude file so control-plane outcomes and supporting evidence remain available to workflow steps without entering source status, diffs, reviews, commits, or pull requests.

The daemon must tell an outcome-bearing step the fixed path and schema, remove any pre-existing outcome before delivering a new step prompt, and read the document only after the supervised turn reaches its completion boundary. A deliberately unprompted step produces no outcome and must not consume a file left by another step. Recovery of an interrupted prompted turn must preserve a possibly fresh outcome rather than deleting it merely because the daemon restarted.

The outcome document has a versioned envelope with:

- `version`, identifying the contract version;
- `status`, distinguishing successful and failed work;
- `summary`, providing a bounded human-readable result; and
- optional `artifacts`, a string-keyed map whose values are defined by the workflow that consumes them.

The daemon validates the envelope before persisting it with the step record. A missing, unreadable, unsupported, or schema-invalid document is recorded as an absent outcome with the validation reason. It is data for subsequent routing and operator diagnosis, not success and not, by itself, an infrastructure failure. The working tree remains authoritative for source artifacts; the outcome document is the explicit control-plane handoff and must not be treated as proof for claims the producing step did not substantiate.

Routing is deterministic first. A decision function evaluates persisted step outcomes and other declared evidence before any probabilistic classification. Transcript prose must never be the authoritative normal-path completion signal.

If semantic judgment is used to recover an absent or malformed outcome or an otherwise unresolved route, it may run only as a conservative fallback. Its input and effect must be inspectable, any synthesized outcome or route must be marked as judge-produced and validated against the same outcome or route contract, and uncertainty, invalid output, or judge failure must preserve the absence and escalate to a human gate rather than guess or fail the run. The unresolved choice between an engine-reserved fallback and a declared judge step must be settled in favor of a provenance model consistent with durable product direction before this ADR becomes Accepted.

The invariant is that workflow routing consumes validated, persisted, explicitly attributable evidence. Agent prose is not silently promoted to a result, workflow metadata does not pollute Git, and uncertainty remains visible to the operator.

## Consequences

Steps receive a small, provider-independent interchange contract. The same file is writable by the sandboxed agent, readable by the host control plane, available to later steps, and durable in workflow history after the transient file or task workspace disappears. Fixed paths and versioned validation make stale, malformed, and incompatible results detectable. Clone-local exclusion avoids changing the project's tracked ignore policy and keeps internal workflow material out of review and publishing surfaces.

Deterministic routing becomes testable and auditable because it consumes bounded records rather than free-form transcripts. A later prompt can render the exact structured handoff, while an operator can distinguish an agent-authored result, a missing result, and a judge-synthesized result. Human gates provide a fail-closed recovery path when evidence remains ambiguous.

The file is produced by an untrusted agent and must be treated as untrusted input. Schema validity does not prove factual correctness, artifact values cannot grant authority, and consumers must validate any value before using it as a path, command, route, or privileged input. Secrets must not be written into the outcome, supporting `.ompire/` artifacts, prompts, persisted workflow state, or judge transcripts.

A single fixed file requires strict lifecycle handling. Failing to remove stale output can attribute one step's result to another; removing it during recovery can destroy a result written before interruption. Sequential step execution avoids concurrent writers. Any future parallel execution model must give outcomes collision-free identities or isolated namespaces before outcome-bearing steps can overlap.

The generic artifact map is intentionally flexible but weakly typed at the engine boundary. Workflow-specific consumers bear responsibility for names and value validation. A future contract may introduce typed, content-addressed artifacts or richer provenance; it may supersede the envelope version without abandoning the requirements for explicit validation, attribution, Git exclusion, and visible uncertainty.

The current judge mechanism blocks acceptance because it is not represented as a declared workflow step and does not record its own step result. Reconciliation may change how judging is declared and persisted. It must not make semantic inference the normal router, erase the original absence or parse error, or turn an uncertain classification into success.

This decision should be revisited if task clones cease to be the shared agent/daemon filesystem boundary, if workflows need concurrent outcome-bearing steps, if outcome payloads require large or binary artifacts, or if a brokered artifact store can preserve the same offline recovery, attribution, and Git-isolation properties more safely.

## Alternatives considered

### Infer completion and handoffs from the agent transcript

Transcript inference requires no agent-authored side file and can sometimes recover intent from existing events. It was rejected as the normal result interface because wording and event shape are not a stable schema, the producing agent's final prose may be incomplete or contradictory, and routing would become probabilistic even when the agent can emit a deterministic document. Transcript inspection remains evidence for operators and, subject to the unresolved provenance policy, a possible input to conservative fallback judgment.

### Add a manager-only RPC tool or protocol extension

A dedicated completion tool could validate arguments at call time and deliver results directly to the daemon. It was rejected for this boundary because it would add an Ompire-specific capability to the native agent protocol, couple completion to one integration mode, and behave differently when the same session is resumed or inspected through a terminal. A file convention uses the existing shared workspace and requires no provider or protocol extension.

### Store outcome files in tracked source or the repository ignore file

Tracking outcomes would preserve them in Git but would mix control-plane metadata with the product change and expose it to status, review, commits, and pull requests. Adding `.ompire/` to the repository's tracked ignore file would avoid status noise but mutate every participating project for an Ompire-local concern. Clone-local Git exclusion keeps the boundary private to the task workspace; durable data is persisted separately in workflow history.

### Escalate every missing or unresolvable outcome directly to a human

Immediate gates maximize explicit human control and avoid model-synthesized evidence. They also interrupt recoverable runs when the working tree and transcript contain enough evidence for a narrow classification. A conservative semantic fallback can reduce those interruptions only if its provenance is explicit, its output is validated, and uncertainty still gates. Whether that fallback must always be a declared workflow step remains unresolved.
