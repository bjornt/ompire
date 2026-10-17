# Encapsulate named agent sessions

## Outcome

A maintainer can change native agent supervision, conversation recovery, or model
policy handoff without changing the workflow interpreter. One session owner
controls named conversations and their verified applied policy; workflows decide
which conversation to use and what work it may perform. HTTP is an adapter to
admitted commands, not a second implementation of session control.

For the operator, existing named conversations, controls, transcripts, questions,
and recovery keep working through this structural change. This is the third
child of the [modular-monolith epic](../../EPIC.md), following the finished
`isolate-workspace-lifecycle-and-execution` change.

## Vision alignment

This enables the [vision](../../../../docs/VISION.md)'s deterministic workflow
engine and deep native agent integration without turning sessions into workflow
authority. It preserves the isolation resource boundary, accepted model intent,
and honest resumability. The browser remains a reconnectable presentation layer.

The boundary is inside the trusted daemon, not a new security sandbox. No claim
is made about stronger isolation, network policy, durable transcript archival,
full provenance, or a second agent backend. No vision change is required.

## User experience

A task starts only the named sessions its pinned workflow needs. A workflow can
reuse a conversation across steps, including steps with different accepted model
policies. Changing only the active model/thinking pair keeps the process; changing
an auxiliary role replaces the process while resuming the same conversation.
The transcript reconnects instead of appearing finished or starting over.

While work runs, the operator can inspect state and cost, steer, queue a follow-up,
interrupt with new text, answer an ask or approval, and stop the addressed agent.
One session's pending question, crash, or replacement does not become another
session's state. Existing busy/workspace refusals remain visible and do not gain
an HTTP-only bypass or an in-process bypass.

An unstarted session has no live stream; a command-only task need not start any
agent. Missing or undeclared sessions, stale answers, unavailable pinned inputs,
and absent live processes retain their existing explicit refusals. Failed model
configuration never sends the dependent prompt under substituted or unverified
settings. A busy refusal does not interrupt the turn already in progress.

After a daemon restart, recoverable conversations resume on their own recorded
policy, not today's profiles. Missing policy is reported rather than guessed,
with only the existing pre-prompt interrupted-step exception. Recovery starts no
new workflow decision and invents no prompt; the workflow retains the decision
about whether its existing attempt needs to be re-driven.

## Requirements

### R1. One owner and one admitted command path

- The session owner controls conversation identity, verified applied-policy
  records, native child supervision, status transitions, questions/approvals,
  native controls, and live session observations.
- Workflow callers and operator commands reach the same session admission and
  execution mechanisms. A direct application caller receives the same readiness,
  question, workspace, and process-state refusals as the corresponding HTTP call.
- Session execution accepts a named session, resolved workspace, and exact policy;
  it does not select a workflow, read workflow persistence, resolve mutable model
  profiles, choose a successor step, authorize publishing, or infer success from
  narrative output. Workflow-owned admission and routing remain outside it.
- Cross-owner callers cannot bypass these operations through mutable supervisor,
  process, tracker, or persistence internals. The import boundary is mechanically
  checked, including local and type-only imports, with no forwarding paths left
  behind. The maintained module map identifies owned state and public contracts.

### R2. Preserve policy and conversation continuity

- Each consumer uses exactly its accepted four-role binding. A missing binding
  fails closed. Every policy application reasserts and verifies the active pair,
  even when a cached process reports the same policy.
- Policy changes occur only at a turn boundary. Streaming, compaction, queued
  messages, and an unanswered question retain their current busy refusals.
- An auxiliary-role change gracefully stops the old child, resumes the captured
  native identity with the new policy, and verifies that identity. A new
  conversation cannot masquerade as a successful replacement.
- A newly started or reconfigured child is not available for the dependent prompt
  until its policy has been verified and durably recorded. Failure, timeout, or
  cancellation after configuration begins cannot leave an unverified candidate
  available to send prompts. A refusal before changing the child may leave the
  previously verified conversation intact; it never admits the refused turn.
- Model observations publish only after successful verification and recording.
  Replacement preserves the logical session, status tracking and bounded replay
  history; the retired child's exit cannot fail or deregister its successor.
- Fresh-start identity capture remains best-effort. This is distinct from the
  mandatory identity check during a policy-driven replacement.

### R3. Preserve native controls and observation

- Keep the current session-addressed HTTP paths, request/response shapes,
  authentication, error mapping, and controls. No start/prompt endpoint is added.
- Follow-ups and interruption continue the last applied session policy rather
  than reselecting a task default. Stale question IDs send no answer; resolving a
  question affects only that session. Controls remain usable while a workflow
  waits for an ask or approval; no session lock is held across the whole turn.
- Keep native request correlation and opaque rich frames, including stderr,
  tool activity, subagents, todos, model/context state and cost. No generic chat
  abstraction replaces these capabilities.
- Main snapshots/deltas and per-session raw streams remain separate. Replay
  stays bounded and ephemeral. Existing `4404` missing-agent and `4409`
  replacement/reconnect behavior, stream envelopes, and terminal exit behavior
  are unchanged; no durable ordering or archival guarantee is added.

### R4. Preserve recovery, cancellation and local consistency

- Resume only sessions eligible under the task's pinned definition and accepted
  inputs, after existing startup reconciliation and writer-block restoration.
  Restore each session's recorded applied policy before re-driving its run.
- A missing policy leaves that session unresumed with its reason and evidence
  intact, except when the existing interrupted attempt had not sent its prompt
  and supplies its exact accepted binding. Retired undeclared sessions remain
  retained but are not resumed or exposed as live sessions.
- No recorded sessions is valid for lazy or command-only work. A failed resume
  is session-scoped; unreadable task intent is task-scoped, not a daemon failure.
- Healthy-daemon child exits are not automatically restarted. Graceful shutdown
  still flushes native session files with bounded forced termination as fallback,
  and shutdown/intentional-replacement exits are not reported as agent crashes.
- Existing workspace-writer exclusion and cleanup holds remain shared with
  review, delivery, capture, and teardown. Session admission does not weaken them
  or introduce a second guard.
- Keep one database and migration history, unchanged retained input/applied-policy
  encodings, and unchanged workflow attempts and prompt-receipt semantics.
  Applied policy commits before dependent prompting; the prompt acknowledgement
  is not turn completion. No database transaction spans a native process wait.
  Delivery/result journals and cross-owner atomic groups are not redesigned.

## Scope

The named-session boundary, application admission and workflow execution adapter,
session-related HTTP and WebSocket integration, session recovery delegation,
canonical caller migration, import enforcement, and the documentation/verification
needed to prove unchanged behavior. Consumer policy selection and run transitions
stay workflow-owned; resource preparation and container transport stay isolation-owned.

## Non-goals

- Extracting the workflow, delivery, artifacts, or oversight owners in full, or
  completing the entire startup composition refactor in this child.
- Changing workflow formats, hashes, routing, visit limits, declared authority,
  prompt/outcome meaning, attention tiers, or publishing identity.
- Schema migrations, historical evidence rewrites, new retry/recovery policy,
  new session endpoints, transcript storage, or new model configuration features.
- Reorganizing the frontend, adding another agent protocol, a plugin framework,
  a service locator, distributed services, or a generic command bus.

## Documentation impact

Update existing contributor pages; do not introduce a parallel package handbook:

| Path | Category | Change |
|---|---|---|
| `docs/develop/reference/daemon-modules.md` | Reference | Session owner, public operations/reads, persistence ownership, dependencies, failures and remaining legacy boundaries |
| `docs/develop/reference/repository-layout.md` | Reference | Canonical session package and application/transport integration paths |
| `docs/develop/reference/agent-rpc.md` | Reference | Ownership of preparation, policy recording, admitted controls and native stream lifecycle; preserve native semantics |
| `docs/develop/reference/crash-recovery.md` | Reference | Separate application eligibility from session-owned resume and persistence; preserve startup ordering and missing-policy exception |
| `docs/develop/reference/database-schema.md` | Reference | Explicit session-row writer ownership, with no schema or encoding change |
| `docs/develop/reference/daemon-api.md` | Reference | Session routes become thin adapters to application commands |
| `docs/develop/reference/websocket-protocol.md` | Reference | Public session observation source; unchanged envelopes, replay and close codes |
| `docs/develop/explanation/architecture.md` | Explanation | Session execution versus workflow policy and isolation resources; distinguish delivered boundaries from later epic work |
| `docs/develop/how-to/build-test-run.md` | How-to | Added session dependency-check rules and their negative fixtures |

Record the durable session ownership/admission decision in the next available ADR
under `docs/adr/`, linked from `docs/adr/README.md`. It extends ADRs 0007, 0008,
0027, 0038 and 0039 rather than superseding their product contracts.

The operator contracts in
[Agent interaction](../../../../docs/use/reference/agent-interaction.md) and
[Workflow engine](../../../../docs/use/reference/workflow-engine.md) intentionally
remain unchanged: no operator action, authority, or recovery policy changes here.
Verify their descriptions against the completed behavior rather than adding a
refactor announcement. `docs/VISION.md` is also intentionally unchanged.
