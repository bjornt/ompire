# Plan

## Approach

Extend the existing declarative interpreter and task-owned attempt registry. Do not
introduce another runner, workflow-specific Python routes, a generic schema engine,
or an authoring surface. New bugfix launches use format 2; retained format 1 remains
an executable compatibility contract, not a migrated alias for the new behavior.
Keep `single-step` on its present revision.

### Repository facts and compatibility

- `workflow_definitions.py` owns parsing, frozen definitions, canonicalization,
  three-valued evaluation and graph validation. `SUPPORTED_FORMATS` currently
  contains only 1. `_successors` currently knows decision edges and fall-through,
  not gate choices; `_validate_graph` rejects cycles without bounded steps and
  exhaustion targets that return to their own bound.
- `workflows.py` reads only a generic version-1 success/failed envelope. Its
  evaluation context excludes the current attempt and `latest` supports `after`
  and `with_outcome`, but attempts have no explicit frozen input bindings.
- `WorkflowRunner.resume_gate` checks `expected_seq` then resolves an in-memory
  future. It does not commit the answer before returning. Uncertainty retry is
  already transactional through `registry/workflows.py:retry_paused_step`; reuse
  that write-before-schedule pattern for gate answers.
- `builtin_workflows/bugfix.yaml` currently routes failed reproduction directly to
  escalation, contains no separate diagnosis, and skips the QA turn when a script
  already answered. Its only declared work bound is three visits to `fix`.
- ADR-0028 already removed the implicit judge and accepted ADR-0009. The epic's
  judge-reconciliation seed is satisfied in current facts; this change preserves
  it rather than removing a mechanism that no longer exists.

### Format-2 contract

Use format-aware parsing, validation, canonicalization and protocol rendering at
existing boundaries. Share unchanged value/text/command semantics; never add
format-2 defaults into format-1 canonical output. Reject fields belonging to the
other format. Keep format-1 parsing, hashes, instructions, outcomes and gate
fall-through unchanged, including old retained bugfix definitions. Unknown formats
remain classified unavailable. Do not revise accepted task bindings or infer
historical results. The existing legacy continuation compatibility checker must
refuse a new bugfix candidate whose steps or outcome interpretation cannot account
for the old history; no automatic format upgrade is offered.

The new data contract is deliberately bounded:

- An agent declares `outcome: null` (no result requested) or an `outcome` document
  with `results`, a nonempty mapping from slug result names to contracts. Each
  result contract has `required`, a mapping from artifact field names to the
  evaluator's non-null JSON type names. Required strings must contain
  non-whitespace text; null is not a substitute for a required value. Extra artifact
  fields may remain bounded JSON data, but cannot satisfy undeclared requirements.
  No recursive schema, regex validator, expression code, or inferred result names.
  Format 2 uses this instead of `expects_outcome`.
- The outcome-file envelope is `{version: 2, result, summary, artifacts}`. The
  result must be declared by this step, summary must be nonblank, and artifacts
  must satisfy that result's required fields. Reject unknown envelope keys,
  duplicate keys, invalid UTF-8, non-finite numbers, nesting deeper than 32, or content
  over 1 MiB. Preserve the existing fixed path and fresh-file lifecycle; recovery
  of an interrupted prompt never removes its potentially fresh outcome. Emit the
  step-specific contract in the initial instruction and versioned recovery nudge.
  A valid negative domain result finishes the attempt `ok`; missing/invalid output
  uses the existing uncertainty mechanism with a field-specific reason. A required
  format-2 outcome with an empty rendered prompt pauses rather than silently
  producing a usable result; explicit `when: false` remains an intentional skip.
- Add a common `evidence` map from alias to a record selector with `steps`, optional
  `after`, `with_outcome` (default true), and `required` (default true). Selectors use
  existing latest/after rules against prior task-local records. Add the closed
  value operation `{op: evidence, name: <alias>}` for the selected record view.
  Aliases cannot reference each other. Resolve each selection once at attempt
  entry, record its step/sequence identity (or explicit absence for an optional
  selector), and reuse it for prompt, conditions, gate presentation and recovery.
  Missing required evidence pauses before prompting or routing. Record views
  expose source evidence identities as well as outcome, step and sequence, allowing
  a decision to compare a verifier's bound fix with the actual latest fix. Do not
  repeatedly copy whole prior outcomes: the immutable earlier rows own their bytes.
- A format-2 gate has a message, evidence bindings, and a nonempty ordered
  `choices` list. Each choice has a unique slug `id`, nonblank `label`,
  `feedback_required` boolean, and a static `next` naming a step or a named
  completion (not a pause). Choices do not contain executable predicates or dynamic destinations in this child. Retry can
  target a bounded step; if it is spent the engine opens its exhaustion gate.
  Gate choices are the successors; there is no additional gate fall-through.
- Format-2 completion destinations are `{complete: true, result: <slug>}`.
  Preserve run statuses (`running`, `waiting`, `complete`, `failed`) and persist
  the terminal work result separately. Reject unnamed completions and implicit
  fall-off at the last step in format 2. Intermediate agent/command fall-through
  and explicit decisions remain the normal sequencing model. Stop gates use
  named completion destinations, never an `ok` outcome masquerading as a fix.
- Include gate edges in graph/cycle/exhaustion validation and descriptor
  conditionality. Existing parser size/depth/node/step bounds also cover the new
  maps and choices. Static references must resolve inside the definition; runtime
  absence never selects an older convenient result or a guessed fallback.

An explicitly authored semantic assessor is an ordinary outcome-bearing agent step
with declared evidence and results, for example `accepted`, `rejected`, `uncertain`
and required numeric confidence. It consumes a normal launch binding and routes
uncertainty explicitly to a gate. Do not add a fifth step kind, reserved consumer,
implicit dispatch, or extra assessor to the packaged bugfix.

### Durable gate transition and projections

Add nullable `workflow_steps.evidence_json` and `tasks.workflow_result` through the
next Alembic migration and typed registry decoding. Existing rows remain null,
meaning not recorded, not an invented empty binding or terminal verdict. The
workflow step's existing outcome JSON holds a versioned gate snapshot: rendered
message, choice definitions, bound evidence identities and, after acceptance, its
resolution. Preserve the question snapshot when adding the resolution. Record the
choice id, exact feedback, resolved destination, server timestamp, and actor
`operator` at the single-user authenticated boundary; do not infer a person or
store the bearer token. Structured history remains after ordinary cleanup.

Extend the existing resume request with optional `choice_id`. For a format-2 gate
it is required; `note` remains the feedback field, limited to 16 KiB UTF-8 and
nonblank when required. Missing/unknown choices and invalid feedback are `422` with
field-level errors. An unknown task is `404`; a stale `expected_seq`, no longer
waiting task, duplicate answer, or conflicting waiting state is `409`. Reject
`choice_id` on format-1 gates and uncertainty retries; retain their current
`expected_seq`/`note` semantics and apply the new feedback bound only to format-2
gate answers. Refuse unknown request fields. An outcome pause
never becomes a gate just because a caller supplied a choice.

The registry gate-resolution operation obtains the existing write reservation,
checks task/attempt/revision/kind/waiting state and the pinned choice, and commits
all of these together: the answer on the waiting attempt, its completion, and
either a newly opened successor attempt with its evidence bindings/current run
position or a terminal status/result. Apply visit limits before opening a bounded
successor. The run machinery schedules or wakes only after commit; its future is
notification, not authority. A crash before commit leaves the original unanswered
gate; after commit recovery finds the already selected successor or completion,
never re-arms the answered gate. Do not let `_park_at_gate` independently finish
or open the same attempt again. Use this durability mechanism for old gate answers
too without changing their note/fall-through meaning.

Expose the same typed gate/evidence/result data in task REST payloads, snapshot,
`task_updated`, and `workflow_step`; identify step events by sequence so same-name
loop iterations cannot overwrite one another. Reconnect reconstructs the same
history without replay or catalog substitution. Show attempt disclosures and
source/session links in task detail, not a new graph UI. Render labels, feedback,
and evidence as text/escaped content. Display nonblank errors and unavailable
history links honestly. Offer no preselected choice and require an explicit
selection before submitting. Keep unsent feedback local to its task/attempt identity,
reset it when that identity changes, disable repeat submission, and reconcile a
409 against daemon state without reapplying the old answer.

### Packaged bugfix policy

Use two sessions, `reproducer` and primary `coder`, with `default` roles unless the
operator overrides a consumer at launch. All outcomes carry a nonblank summary.
Required result-specific artifact fields are small strings except the boolean
`script_available`:

| Producer | Results | Required evidence |
|---|---|---|
| `reproduce`, `reproduce-informed` | `reproduced` | `attempts`, `expected_behavior`, `observed_behavior`, `reproduction_evidence`, `script_available` |
| same | `not-reproduced` | `attempts`, `observed_behavior`, `missing_prerequisites` (explicitly say none when none are known) |
| `diagnose` | `candidate-found` | `findings`, `suspected_trigger`, `suggested_reproduction` |
| same | `no-root-cause` | `findings`, `missing_information` |
| `fix` | `implemented`, `unable-to-fix` | `changes`, `validation_notes` |
| `verify` | `validated`, `rejected`, `inconclusive` | `checks`, `observations`, `limitations` |

Use readable decision steps in YAML for these routes, not Python hooks:

- `reproduce` → `diagnose` for either declared reproduction result. Diagnosis
  freezes the latest QA evidence and any new retry feedback. It must not change
  the code under investigation. A new diagnosis based on failed reproduction
  cannot inherit an older candidate's successful informed reproduction.
- `candidate-found` with a bound positive reproduction → `fix`; with negative
  reproduction → `reproduce-informed` in the same QA session. Informed QA freezes
  the candidate diagnosis and attempts its suggested reproduction before fixing.
- `no-root-cause` → `diagnosis-gate`: `retry-diagnosis` (feedback required) or
  `stop` → `stopped-without-fix`.
- Informed `reproduced` → `fix`; informed `not-reproduced` → `reproduction-gate`:
  `retry-diagnosis` (feedback required), `proceed-without-reproduction` (rationale
  required) → `fix`, or `stop` → `stopped-without-fix`. The exception evidence must
  reference the same diagnosis as the selected informed QA. A later diagnosis
  invalidates use of the earlier exception, without rewriting its record.
- `fix` freezes diagnosis, the matching reproduction or exception, and on revisits
  the latest rejection for the previous fix. It plans and implements without
  changing reproduction evidence. `unable-to-fix` goes to the correction stop
  gate with its explanation, never into successful validation.
- After `implemented`, if the selected positive QA evidence says a script exists,
  a declared command runs literal `bash .ompire/repro.sh` through Workshop; do not
  execute an agent-supplied argv. Both script and non-script paths reach `verify`
  in the original QA session. Verification freezes the current fix, its evidence
  chain/exception, and the script result when applicable. A nonzero script exit
  cannot be overridden by a positive QA verdict; it routes to correction.
- `validated` with acceptable current script evidence (when required) completes
  as `validated`, or `validated-without-reproduction` when that fix has an explicit
  exception. `rejected` or nonzero script evidence returns to `fix` with the report.
  `inconclusive` opens a validation gate offering `retry-verification` with required
  feedback or `stop` → `stopped-unvalidated`; it never silently becomes rejection
  or success. The retry keeps the same fix binding and carries the new feedback.
- `reproduce`, `diagnose`, and `reproduce-informed` each declare three visits and
  an `investigation-exhausted` gate that offers only `stop` → `stopped-without-fix`.
  `fix` and `verify` each declare three visits and a `correction-exhausted` gate
  offering only `stop` → `stopped-unvalidated`. Thus a malformed verification
  retry also cannot spin without limit. Script attempts are bounded by the fix
  path; command infrastructure failures retain the existing failed-run semantics.
  No gate resets these lifetime run budgets; a restart resumes the open attempt.

A newer fix invalidates earlier validation by exact evidence binding as well as
sequence ordering. Optional selectors are guarded explicitly; do not coalesce a
required negative report into empty text. Preserve supported QA native session
identity across all three roles and model handoffs. Diagnose inability to resume
that identity instead of silently prompting a new conversation.

## Affected areas

- Definition/interpreter: `daemon/src/ompire_daemon/workflow_definitions.py`,
  `workflows.py`, `builtin_workflows/bugfix.yaml`.
- Persistence/recovery: `daemon/src/ompire_daemon/db.py`, `registry/tasks.py`,
  `registry/workflows.py`, `registry/workflow_definitions.py`, the next migration
  under `daemon/alembic/versions/`, `taskdefinition.py`, and existing recovery and
  continuation validation callers. Retained rows and accepted inputs stay intact.
- API/state/launch: `daemon/src/ompire_daemon/api/rest.py:WorkflowResumeBody` and
  `resume_workflow_route`, `registry/tasks.py:task_payload`, `api/ws.py` snapshot,
  and `workflows.py` event publication. `launch.py` and definition descriptors
  enumerate every new agent consumer and preserve revision fingerprint coverage,
  without adding a second resolver.
- Frontend: `frontend/src/routes/TaskDetailView.tsx:GateCard` and `WorkflowStrip`,
  `frontend/src/types.ts`, `frontend/src/lib/api.ts:resumeWorkflow`,
  `frontend/src/lib/daemonReducer.ts`, and existing `SpawnView.tsx`,
  `TaskConfigurationPanel.tsx` and `components/WorkflowRevision.tsx` presentation
  where richer task/descriptor types require it.
- Verification: `daemon/tests/test_workflow_definitions.py`, `test_workflows.py`,
  `test_launch_reconciliation.py`, `test_agent_api.py`, `test_recovery.py`, existing
  migration coverage, `frontend/src/lib/daemonReducer.test.ts`,
  `frontend/src/routes/taskDetailCockpit.test.tsx` and `views.test.tsx`;
  `local-test/fakes/omp`, `local-test/ompctl`, and a new focused
  `local-test/scenarios/workflow-decisions` registered in `local-test/scenarios/run`.
  Reuse `lib.sh` and `crash-recovery`; never write fake state directly.
- Documentation: the exact existing paths and audiences listed in SPEC.md.

## Architecture decisions

1. Add a focused ADR for **attempt-bound human decisions committed before
   advancement**, covering immutable question/evidence identity, write-before-ack,
   replay refusal, and bounded retries. Extend ADR-0008's human transition model
   without replacing task/session ownership. Extend only this decision-history
   slice of ADR-0016, leaving it Proposed for its outstanding publishing,
   transcript, retention and lineage gaps.
2. Add a successor ADR to ADR-0009 for **declared domain outcomes and attributable
   evidence handoffs**. It carries forward fresh files, Git exclusion, untrusted
   data, and no hidden judge, while versioning the success/failed envelope and
   recording frozen consumer references. Mark ADR-0009 superseded with a forward
   link, preserving its historical substance and format-1 applicability. This
   does not supersede ADR-0028: using format 2 is the extension boundary it requires.
3. Allocate the next available ADR numbers during implementation; link both from
   `docs/adr/README.md` and stable enforcing code boundaries when accepted. Do not
   mark proposed rationale accepted until implementation and documentation agree.
   No new ADR is needed for card layout, an API field, or built-in retry counts.
   ADR-0018 stays superseded; ADR-0026's launch boundary and ADR-0027's policy handoff
   remain intact and are linked rather than rewritten.

## Risks

- **Semantic drift in old runs:** adding defaults or changing outcome instructions
  can invalidate retained hashes or reinterpret evidence. Verify frozen format-1
  documents and active runs alongside format-2 runs, including legacy continuation
  refusal, single-step behavior, and unsupported formats.
- **Lost/duplicated answers:** the present in-memory gate future acknowledges too
  early. Exercise before/after-commit interruption and two competing submissions;
  assert exactly one recorded choice and one successor attempt.
- **Stale evidence accepted as current:** compare exact producer bindings and
  causal chains, not just result text or the latest globally successful record.
  Exercise a newer fix, newer diagnosis, optional absence, and resumed attempts.
- **Human edges defeating bounds:** include all choice and exhaustion edges in
  graph validation; verify gate retries and uncertainty retries cannot reset counts.
- **Session replacement losing QA context:** test native identity after informed
  reproduction, policy handoff, and restart; fail visibly on missing/unresumable
  history. No promise of immutable workspace contents or unlimited model context.
- **Growing DSL/UI beyond this child:** only bounded field contracts and record
  selectors; no schema runtime, graph editor, delivery permission or artifact store.
  QA verification adds a deliberate turn even on the script path to satisfy the
  epic's original-session verification contract.

## Tasks

- [x] Implement format-2 definition parsing, canonicalization, result contracts,
  evidence selectors/value references, named completion results, and structured
  gate choices. Preserve exact format-1 serialization/protocol semantics and
  include gate edges in cycle/exhaustion checks. Before changing exported symbols,
  use available LSP references to cover all consumers. Verify loader rejection,
  format coexistence and result/route edits changing revision identity. (R1, R4, R7)
- [x] Add the additive evidence/result migration and typed registry support. Freeze
  source-attempt bindings at entry and persist question snapshots. Implement an
  atomic gate-resolution operation that validates the exact wait, records the
  decision, and opens the bounded successor or completes with a named result.
  Keep old rows honestly unrecorded; verify rollback, replay/concurrency refusal,
  and preservation through ordinary cleanup. (R2, R3, R4, R6, R7)
- [x] Integrate versioned outcome instructions/validation, frozen evidence lookup,
  gates, completion and crash recovery into the existing runner. Replace
  authoritative future-only gate resolution with post-commit notification;
  preserve uncertainty retry and old-format behavior. Verify missing/invalid
  results pause, explicit negative results route, old evidence cannot satisfy new
  work, and interruption after accepted choice does not lose/replay it. (R1–R4, R7)
- [x] Replace the new-launch bugfix YAML with the complete policy above, including
  diagnosis, informed QA, structured gates, evidence chains, explicit exception,
  script plus QA verification, inconclusive/rejected results, and all bounds.
  Extend existing executable workflow tests for the reachable branches, current
  fix/candidate association and QA native-session continuity. Keep accepted old
  bugfix revisions executable and single-step unchanged. (R1, R2, R4, R5, R7)
- [x] Extend the existing resume request/route, authoritative task/snapshot/event
  projections and exact-sequence history delivery; update frontend types/client/
  reducer and task-detail history/gate controls. Preserve legacy Resume/Retry,
  display named terminal results, handle 422/409/network errors without replay,
  and show new launch consumers through the existing preview. Verify the actual
  controls and evidence/session links, including keyboard and narrow layout.
  (R2, R3, R5, R6, R7)
- [x] Extend the executable fake and add a focused local workflow scenario through
  the published harness controls, then run isolated browser journeys: initial
  non-reproduction → diagnosis → informed QA → fix → script/QA validation;
  no-root-cause feedback/retry and stop; continued non-reproduction exception and
  stop; inconclusive verification with feedback/retry; rejected-fix and exhausted
  budgets; malformed result and retry. Restart during informed QA and while gated.
  Cover the exact post-commit/pre-schedule interruption with a deterministic runner
  regression, without a production test hook. Observe preserved native QA identity,
  evidence and choices after reconnect/restart, no duplicated attempt, and no
  automatic publication. Read `skill://local-e2e` at execution time, use a unique
  state root/free port and the published controls, and tear down that environment.
  Run focused workflow/API/recovery regressions plus existing single-step
  happy-path and crash-recovery scenarios; use ws-watch for changed event
  behavior. Run project lint/typecheck and both test suites once after integration.
  Keep permanent regressions only for behavioral boundaries, not wording, field
  forwarding or incidental defaults. (R1–R7)
- [ ] Update every documentation destination in SPEC.md to delivered behavior,
  preserving format-1 distinctions. Write/reconcile the two focused ADRs and
  ADR-0016 progress, index and code backlinks; make no claim to deliver later
  library, publisher or durable-file slices. (R1–R7)
- [ ] Re-check completed behavior and evidence against `docs/VISION.md` and this
  spec: deterministic routing, visible uncertainty, bounded work, durable decisions,
  QA continuity, no fabricated reproduction or authority, and no in-flight revision
  mutation. Keep the vision unchanged. (R1–R7)
