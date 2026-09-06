# Explicit outcomes, evidence handoffs, and human decisions

## Outcome

An operator can follow a bugfix from reproduction through diagnosis and validation,
understand which evidence caused each route, and answer a blocked workflow with a
specific, durable decision. Failure to reproduce does not discard the issue or
silently permit a fix: diagnosis investigates it, and new findings return to QA
before implementation. Stopping, validating a fix, and proceeding with an explicit
reproduction limitation remain distinguishable after reconnect and restart.

This is the second child of [Editable, durable workflows](../../EPIC.md). It builds
on finished `pin-declarative-workflow-revisions`; it does not deliver the library,
visual authoring, or trusted delivery composition owned by later children.

## Vision alignment

Aligned with [VISION.md](../../../../docs/VISION.md): deterministic orchestration,
explicit uncertainty, scarce human attention, durable decisions, and named-session
continuity. A domain result is evidence, not an infrastructure status or authority.
An agent's hypothesis never becomes a reproduction merely because it is plausible.
Human permission to proceed without reproduction preserves that limitation and
authorizes only the declared work route, not signing, pushing, or opening a PR.

The implicit judge was already removed by the first child and ADR-0028 reconciled
ADR-0009. Preserve that resolution; do not reintroduce a fallback classifier. No
vision change is needed.

## User experience

### Bugfix flow

1. QA records whether it reproduced the issue, what it tried, expected and observed
   behavior where known, and any missing prerequisites. Scriptable reproductions
   retain an executable check; non-scriptable evidence is described honestly.
2. Both `reproduced` and `not-reproduced` continue to a separate diagnosis turn in
   the coder session. That turn sees the negative evidence as well as any positive
   evidence. It investigates without implementing and reports either a candidate
   root cause with findings and a proposed trigger, or `no-root-cause`.
3. A candidate root cause with reproduction evidence can proceed to planning and
   fixing. If QA initially could not reproduce, the candidate findings and suggested
   reproduction return to QA in its original session, against the still-unfixed
   code. Only QA's new evidence can establish reproduction.
4. `no-root-cause` opens a gate offering **Supply information and retry diagnosis**
   or **Stop without a fix**. Continued non-reproduction after informed QA offers
   **Supply information and retry diagnosis**, **Proceed without reproduction**,
   or **Stop without a fix**. Retry requires useful feedback; proceeding without
   reproduction requires an explicit rationale and an existing candidate cause.
5. Planning and implementation share the coder's bounded fix turn. QA verifies
   every fix in its original session, receives the exact fix attempt and relevant
   reproduction or exception decision, and sees any deterministic script result.
   A rejected fix sends its actionable report back to the coder; a failing script
   cannot be overridden by a positive QA verdict. An inconclusive QA result asks
   the operator to supply information and retry verification or stop without a
   validated fix. A coder unable to implement stops at a human gate rather than
   entering successful validation.
6. Investigation, fixing, and verification each have finite budgets. Exhaustion reaches a human
   gate outside the loop; its only resolution stops this run. Gates never replenish
   a budget. Completion says whether validation passed with reproduction, passed
   under an explicit no-reproduction limitation, or the operator stopped without
   a fix or without a validated fix. The workspace remains available under the
   existing task lifecycle.

### Inspecting and answering

Task detail shows ordered attempts, domain results, input evidence links, decisions,
and session links. A gate names the question, the evidence it is asking about, each
choice and destination, and any required feedback. Nothing is pre-authorized by
opening the card. Waiting remains a notify-tier attention state.

Submitting locks the controls while the command is pending. A rejection remains
inline. If another tab already answered, the current daemon state replaces the old
card; the old choice and feedback are never submitted automatically against the
new question. After reconnect or restart, an unanswered question is the same
question, and an accepted answer remains in history with its destination.

Before any attempts exist, task detail says the workflow has not started. Missing
outcomes show a retryable uncertainty pause and its validation error, not an empty
gate or a negative domain result. Unavailable pinned definitions leave history
readable and execution blocked, with no guess at choices from today's catalog.
Older format-1 tasks retain their existing Resume/Retry behavior and do not acquire
invented domain results or gate approvals.

## Requirements

### R1 — Declared results and fail-closed evidence

Definitions can declare named results and the evidence fields required for each
result. A valid negative result completes the producing attempt and follows an
explicit route. Missing, malformed, unsupported, unknown-result, or incomplete
required evidence pauses at that attempt with a useful error. It never falls
through, becomes success, or invokes an undeclared model. Validation establishes
structure and attribution, not factual truth or permission to execute content.
Any semantic assessment uses an ordinary declared agent step with visible inputs,
model policy, outcome, and an explicit uncertainty route; no special judge engine
or built-in judge turn is required by this change.

### R2 — Attributable, attempt-specific handoffs

Every new-format consuming attempt identifies the prior attempts supplying its
required evidence. Those selections remain fixed through restart. Diagnosis sees
failed reproduction evidence; informed QA sees the diagnosis that requested it;
validation identifies the fix it checked. A successful old validation cannot answer
for a newer fix, and a reproduction or human exception from an older diagnosis
cannot authorize a newer candidate silently. Missing required handoffs pause.
Persisted records are not rewritten by retry or later output-file changes.

### R3 — Structured and durable gate decisions

A new-format gate declares unique named choices, static destinations, and whether
feedback is required. No generic Resume action may bypass those choices. The daemon
accepts only a choice for the exact waiting attempt and pinned definition, recording
the displayed question/evidence, choice, feedback, operator attribution, time, and
selected destination durably before acknowledging acceptance or starting its
successor. Duplicate, stale, unknown, or malformed submissions advance nothing.
An unanswered gate and an accepted decision survive restart, including interruption
between acceptance and the next step. Feedback is untrusted data, never a route or
command. Gate choices cannot grant publishing authority.

### R4 — Bounded investigation and correction

All cycles, including human retry routes, pass through declared visit bounds.
Budgets count newly opened work attempts, including uncertainty retries; recovery
of an already open attempt costs no visit. The built-in allows at most three
initial-reproduction attempts, three diagnosis attempts, three informed-reproduction
attempts, three fix attempts, and three QA-verification attempts per run. Verification
retries consume that same three-attempt budget, so they can reduce the number of
fix iterations the run can validate. Exhausting any investigation budget opens an
investigation-exhausted stop gate; exhausting fixes or verification opens a
correction-exhausted stop gate. Neither can return to an exhausted loop. A retry requested after the
budget is spent reaches that gate rather than launching another turn.

### R5 — The complete bugfix route

Implement the flow above as the new packaged bugfix revision: distinct diagnosis,
findings-driven return to QA before fixing, no-root-cause and continued
non-reproduction gates, explicit exception rationale, and bounded rejected-fix
correction. The two logical sessions remain `reproducer` and `coder`, with coder
primary. Reproduction, informed reproduction, and QA verification reuse the same
native QA conversation, including after restart and supported model-policy handoff.
A session that cannot be resumed fails visibly rather than quietly becoming a fresh
conversation. Passing script evidence does not skip QA's declared verification
turn in the new bugfix revision. No human-authorized no-reproduction path claims
before/after proof it did not establish.

### R6 — Honest, inspectable run state

Task detail distinguishes domain results, infrastructure failure, uncertainty,
unanswered gates, chosen routes, loop exhaustion, and terminal work results.
Attempt evidence and decisions survive reconnect, daemon restart, and ordinary
workspace cleanup as structured history; missing session transcripts or files are
identified as unavailable, not fabricated. The UI uses daemon-owned projections
and renders untrusted strings safely. Controls remain keyboard accessible and
usable at narrow widths. Existing launch preview discloses every additional agent
consumer and its accepted policy; this child does not build a flow diagram/editor.

### R7 — Revision compatibility and unchanged authority

New semantics use workflow format 2. Retained format-1 definitions keep their
canonical identity, outcome protocol, gate semantics, routes, and recovery behavior.
Existing accepted tasks are not rebound; new bugfix launches pin the new revision.
A prompt, result contract, gate choice, or route change invalidates the affected
launch preview. Unknown versions fail visibly. Legacy tasks without retained
semantics still require checked explicit continuation; incompatibility stays a
visible block, never an automatic history rewrite. Keep single-step behavior and
existing review/ship services intact. Neither workflow completion nor a gate answer
starts review, signs, pushes, or creates a PR.

## Scope

Versioned definition/interpreter changes, bounded result validation and handoffs,
transactional gate decisions and recovery, the new packaged bugfix flow, task-detail
inspection and gate controls, existing launch/state projections, focused behavioral
regressions, and isolated local browser verification.

## Non-goals

- Workflow library CRUD, YAML import/export UI, visual editor, or overview diagram.
- Review/publish workflow steps, selectable delivery endings, publishing identity,
  or new authorization checks for manual review/ship entry points; later children
  own that composition. This proposal grants no additional delivery permission.
- Durable file bundles, cross-task handoff/export, transcript archival, full commit
  lineage, purge policy, or artifact storage from the task-artifacts epic.
- Concurrent steps, nested workflows, plugins, arbitrary executable expressions,
  general JSON Schema, automatic judge fallback, or a separate model policy system.
- Sandbox-enforced read-only diagnosis/QA or an assurance that prompt instructions
  can prohibit every agent mutation. The flow sequences investigation before fix;
  stronger sandbox capabilities are a separate boundary.
- Reopening a stopped run, resetting exhausted budgets, or migrating accepted
  format-1 tasks onto new procedures.

## Documentation impact

Update existing pages on implementation, not during this proposal:

- Operator reference: [workflow-engine.md](../../../../docs/use/reference/workflow-engine.md)
  (results, evidence, gates, bounds, compatibility),
  [bugfix-workflow.md](../../../../docs/use/reference/bugfix-workflow.md) (replace
  the old triage/fix route and diagram),
  [task-detail.md](../../../../docs/use/reference/task-detail.md) (history and choices),
  [api.md](../../../../docs/use/reference/api.md) (gate request/refusal contract), and
  [task-spawn.md](../../../../docs/use/reference/task-spawn.md) (new bugfix consumers
  and coexistence of pinned formats).
- Contributor reference: [workflow-definitions.md](../../../../docs/develop/reference/workflow-definitions.md),
  [database-schema.md](../../../../docs/develop/reference/database-schema.md),
  [websocket-protocol.md](../../../../docs/develop/reference/websocket-protocol.md),
  [daemon-api.md](../../../../docs/develop/reference/daemon-api.md), and
  [crash-recovery.md](../../../../docs/develop/reference/crash-recovery.md) for the
  versioned grammar, persistence/transaction boundary, and projections/recovery.
- Contributor explanation: [architecture.md](../../../../docs/develop/explanation/architecture.md)
  for evidence-bound human transitions, linking to the primary references.
- Contributor local-verification reference/how-to:
  [local-testing.md](../../../../docs/develop/reference/local-testing.md) and
  [run-local-e2e.md](../../../../docs/develop/how-to/run-local-e2e.md) for the added
  fake-agent behaviors and workflow scenario.
- ADRs: extend the durable rationale with a focused gate-decision ADR and a
  version-2 outcome-contract successor to ADR-0009; preserve ADR-0028's version
  boundary and reconcile only the delivered decision/evidence slice of ADR-0016.
  Update [the ADR index](../../../../docs/adr/README.md). Exact ADR work is in PLAN.md.

No new product-documentation page is needed: both audience sets already have homes
for this behavior. `docs/VISION.md` remains unchanged.
