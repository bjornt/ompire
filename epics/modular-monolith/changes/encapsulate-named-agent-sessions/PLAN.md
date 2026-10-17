# Plan

## Approach

Complete one owner extraction, not the rest of the modular-monolith map. Keep
one process, one SQLite schema and migration chain, the existing native omp
protocol, and the isolation package's container transports and shared guard.
Application code resolves task/workflow admission; sessions execute resolved
requests and own all session state. The workflow interpreter retains accepted
consumer selection, prompt construction, attempt receipts, outcomes and routing.

### Canonical owner and values

Replace the flat `sessions.py` with a `sessions/` package. Move `agent.py` to
`sessions/supervisor.py`, `rpc.py` to `sessions/rpc.py`, the status machine to
`sessions/tracker.py`, and `registry/sessions.py` to `sessions/store.py`. Add
`sessions/service.py` for the cohesive operations assembled from these existing
pieces. Declare the public surface in `sessions/__init__.py`, following the
existing isolation-package convention. Internal modules may cooperate directly;
other owners must not import them or receive raw mutable handles/trackers.

Move `ModelPolicy` and `split_model_identifier` from `work/inputs.py` into the
existing dependency-free `model_config.py`, beside `RoleBinding`. Keep the
four-pair representation, equality, `from_roles`, auxiliary comparison, and
payload codec unchanged. Remove the work-specific `ModelPolicy.from_binding`
and `for_step` conveniences and migrate every caller to explicit accepted-binding
selection followed by `from_roles(binding.roles, active_role=binding.role)`.
This avoids a sessions-to-work dependency and avoids inventing a duplicate
policy type or broadening the shared vocabulary into a service layer.

Session-owned values carry the logical key, resolved workspace, exact policy,
and attribution needed by the existing applied-policy document. Profile name,
role and consumer identity are provenance supplied by application/workflow
selection, not instructions for sessions to query those owners. Keep physical
table declarations in `db.py`; only the session store writes session identities
and applied policy. Work's legacy reconciliation uses a named operation that can
seed only a migrated, unverified continuation; verified-policy writes and identity
capture remain private to session execution. Task purge uses a session-owned
connection-scoped deletion operation inside its existing transaction, not session
SQL or private imports. No nested commit or cascade/schema change is introduced.

### Public execution and admission

Construct one fully wired session service with the supervisor, tracker,
persistence access, EventHub and shared WorkspaceGuard. Keep concrete supervisor
and tracker construction private to this owner. Supply only the process settings
it needs rather than teaching it to resolve task intent. Session code may use
isolation's public operations, shared model values, technical configuration/event
infrastructure and its own store; it must not import work/workflow persistence,
application/HTTP code, delivery or artifacts. Oversight remains a consumer of
session observations, not a dependency for deciding session state.

The service's declared operations cover:

- `ensure`: ensure the session row, apply the supplied policy, record verified
  applied state before success, capture identity using the existing best-effort
  rule, and classify spawn versus handoff failure. The durable commit callback
  becomes session-internal; callers cannot skip it or supply a no-op.
- `resume`: apply a supplied eligible native identity/policy and re-commit its
  verification, establish recovery tracking, and return an observation/refusal.
  It never reads a workflow record or emits a recovery prompt.
- `prompt`, `steer`, `follow_up`, `interrupt`, `answer`, and `stop`: use one
  owner-controlled live-process admission path; keep request acknowledgement
  separate from turn completion. Re-read process state inside the existing
  per-session boundary, never publish an unverified candidate, and do not hold
  that lock while waiting for a whole agent turn or a human answer. Preserve
  the existing operation-specific use of the shared writer guard.
- State/stats and pending-question reads, immutable status/model observations,
  bounded stream subscription/replay and exit/replacement observation, plus
  named lifecycle operations currently needed by review, cleanup and shutdown.
  Do not expose a generic mutable tracker or arbitrary process-write escape hatch
  to production callers; explicit native read operations can retain opaque data.

Expose the existing turn-end/idle-entry hooks through the owner with a restricted
read view tied to the originating child, not an `AgentHandle`. Its native reads
are state, stats and last-assistant-text; advisory throttling and heuristics stay
in `advisories.py`, failed samples still cannot disrupt idle, and replacement
must not silently sample a different child's turn. Ship drafting uses the same
last-assistant-text read and public idle waiter; review uses explicit
review-opened/closed notifications. Threshold updates and cleanup/discard remain
named operations rather than exported tracker access.

Add `application/sessions.py` for session commands and addressability checks.
Move the checks from `_require_declared_session`, `_require_live_agent`, and
`_agent_request`, together with agent control handlers, into typed operations:
resolve the pinned session declaration and accepted intent, preserve error precedence,
apply the existing workflow-position rules for operator controls, and call the
session owner. The service must not accept an `allowed=True` flag in place of
those checks. Workflow execution is admitted as a declared run operation, not as
an operator follow-up; those operation-specific rules must not be conflated.
Stop, answer, state/stats and raw-stream reads retain their own current admission
rules; do not apply composer-only writer checks indiscriminately to them.

Extract thin session HTTP handlers to `api/sessions.py`, retaining Pydantic model
names, paths, response bodies and error mapping. Wire through `api/deps.py` and
the common authenticated router, following the already-extracted work routers.
Keep start/prompt unavailable over REST. `api/ws.py` obtains declared-session
admission and session observations through public application/query contracts,
not supervisor maps, handle queues or tracker internals. The WebSocket adapter
still owns authentication, envelopes and close-code encoding; sessions report
bounded replay/live events and logical exit versus replacement. Main snapshot
composition stays where it is until the oversight child, using public reads.

### Workflow and recovery adapters

Add a small consumer-owned `SessionExecutor` contract to the current
`workflows.py`, beside its existing `CommandExecutor` seam. Its operations are
ensure-on-supplied-policy, prompt receipt, and prompt-skipped notification; no
concrete supervisor, RPC connection or persistence callback crosses it. Implement
it in `application/execution.py` over the same admitted session commands. Require
it at runner construction rather than setter-injecting or providing a legacy
fallback.

Remove `_ensure_session` mechanics and all session-row writes from the engine.
`_run_agent_step` still selects the exact accepted binding, ensures the session
even for a false condition/promptless step, renders and validates prompt mentions,
handles outcome-file rules, records `mark_prompt_sent` only after acknowledgement,
and waits for the session's existing debounced idle or exit observations.
`_await_step_idle` may continue consuming the public EventHub lifecycle events;
it is an execution waiter, not ownership of native RPC. Preserve the separate
infrastructure-failure path and every retained workflow format's behavior.

In `recovery.py`, keep task eligibility, pinned-definition checks, declared-session
filtering and the exact no-policy/pre-prompt exception outside sessions. Those
rules read workflow history; moving them into the session owner would violate
its boundary. Delegate resume, applied-state persistence and status restoration
to the session service with resolved values. Do not move all recovery/startup
composition as a side effect of this child. `app.py` supplies the same service
to the runner adapter, routes, recovery, review/delivery consumers, cleanup and
observation, preserving the current startup and shutdown barriers.

### Persistence and enforcement

Session persistence exposes named read/update operations and the minimal
connection-scoped purge participation that current cross-owner atomicity needs.
No session service writes task/run/delivery state. Keep all existing document
versions and migration bytes; migrate live reconciliation callers, not historical
migration logic into a new policy interpretation.

Extend `daemon/tests/test_architecture.py` using its existing AST import collector
and negative-fixture style. Protect sessions' allowed dependencies and declared
surface, ban cross-owner private imports and session-route access to raw schema
or managers, and ban the runner's concrete session/RPC/persistence imports.
Retire `agent.py`, `rpc.py`, `registry/sessions.py`, the old `sessions.py` file,
and the moved work-policy symbol locations. The `ompire_daemon.sessions` import
name itself remains valid as the new public package, not a legacy forwarding
module. Scan the existing source/test/script/harness roots for retired callsites.

The two current exception entries are `registry.workflows` imports of private
work-task mutators, both assigned to the next workflow extraction. Preserve those
unrelated entries; do not claim this child removes them or empties the ratchet.
No new blanket exemption is permitted for the extracted session boundary.

## Affected areas

| Area | Existing paths and expected touch |
|---|---|
| Session execution | `agent.py`, `rpc.py`, `sessions.py`, `registry/sessions.py` → private session package pieces and public service |
| Model values | `work/inputs.py`, `model_config.py`, all model-policy callers and import locations |
| Workflow adapter | `workflows.py`, `application/execution.py`; no definition/hash/routing rewrite |
| Commands/transport | `api/rest.py`, `api/deps.py`, new `application/sessions.py` and `api/sessions.py`, session-facing portions of `api/ws.py` |
| Lifecycle/integration | `app.py`, `recovery.py`, `application/tasks.py`, `application/cleanup.py`, `review.py`, `ship.py`, `results.py`, `work/tasks.py`, `work/reconciliation.py`; migrate session access without extracting those owners |
| Observation | Session reads in `advisories.py`, notifications/snapshot consumers and affected test fixtures; preserve attention policy and wire shapes |
| Persistence | Session store operations over existing tables; preserve purge atomicity and accepted/applied document encodings |
| Verification | `test_agent.py`, `test_rpc.py`, `test_sessions.py`, `test_agent_api.py`, `test_omp_model_policy.py`, `test_recovery.py`, `test_workflows.py`, `test_ws.py`, `test_architecture.py`, affected migration/reconciliation/cleanup/review/ship fixtures and native fake imports |
| Harness | Existing `local-test/scenarios` and `local-test/ompctl` controls; only necessary canonical import updates, no production test mode |
| Documentation | Every path listed in SPEC's Documentation impact, plus the next available session-boundary ADR and `docs/adr/README.md` |

Use language-server references before exported-symbol changes and symbol-aware
moves/renames where supported. Migrate all production, test, harness and script
callers in the same cutover; keep no aliases to the old paths.

## Architecture decisions

Write one new ADR during implementation for session-owned execution and applied
state with application-owned task/workflow admission. It extends ADR-0038's
modular-monolith ownership, uses ADR-0039's isolation resource surface, and keeps
ADRs 0007, 0008 and 0027's native protocol, named-conversation and handoff rules.

The durable choice is to hide native process and persistence mechanics behind
one admitted owner without hiding workflow policy inside it. Alternatives are
a folder-only move retaining raw handles and caller-controlled persistence, or
moving workflow eligibility into sessions; both leave the boundary misleading.
A generic chat API would discard native capabilities and is also rejected.
Individual file moves and ordinary dataclasses need no separate ADR. Mark the
new record Accepted only when code and current documentation agree.

## Risks

- **Ownership-only move leaves a bypass.** REST, engine, recovery, ship drafting,
  cleanup and observation currently reach different pieces. Migrate the full
  caller inventory and enforce public imports; verify equivalent refusals from
  direct application calls and HTTP, not just forwarding through a mock.
- **Policy move changes stored meaning.** Keep codecs, source attribution and
  document versions intact; run existing accepted-input/applied-policy migration
  and recovery regressions. The shared value must not import work back again.
- **Handoff is mistaken for a crash or fresh conversation.** Preserve replacement
  identity verification, retirement before successor publication and replay
  transfer; verify native identity plus rendered reconnect behavior.
- **Lock/guard lifetime deadlocks a human answer or admits a writer.** Keep
  per-session serialization distinct from workspace holds; test busy refusal,
  controls while waiting for input, replacement/control contention and cleanup
  refusal. Never hold a database reservation over native I/O.
- **Cancellation exposes a half-configured child.** Ensure every resource acquired
  before readiness/verification/recording is retired on cancellation or failure;
  test the externally observable inability to send the dependent prompt, without
  killing an untouched busy conversation as a supposed cleanup improvement.
- **Recovery invents policy or repeats workflow work.** Keep eligibility and
  pre-prompt fallback in composition, resume on recorded policy, preserve prompt
  receipts and attempt identities, and exercise restart with declared/undeclared,
  missing-policy and no-session cases. Do not add a healthy-daemon respawn loop.

## Tasks

- [ ] **T1 — Establish canonical values and owned persistence (R1, R2, R4).**
  Map symbol references and session-table readers/writers; move pure policy values
  into `model_config.py` and session persistence into `sessions/store.py`, preserving
  codecs and existing schema. Add named public record operations for existing
  reconciliation and transaction-scoped purge participants. Migrate callers with
  no old-path re-exports; establish the session package's declared public surface.
- [ ] **T2 — Consolidate native execution and lifecycle admission (R1–R4).**
  Move supervisor/RPC/tracker behind the owner and implement the session service
  operations above, including ensure/resume with mandatory verified-state writes,
  classified failures, controls, stream/read contracts, lifecycle marking and
  shutdown. Keep one guard and per-session mutation boundary; exercise failures
  and cancellation without allowing an unverified process to receive a prompt.
- [ ] **T3 — Wire workflow session execution through its port (R1, R2, R4).**
  Add the required consumer-owned `SessionExecutor` and its application adapter;
  remove `_ensure_session`, raw handles and session persistence from the runner.
  Preserve ensure-before-skip, exact consumer selection, prompt receipts,
  debounced idle completion, evidence/outcome handling and infrastructure failures.
- [ ] **T4 — Extract admitted commands and session transport adapters (R1, R3).**
  Centralize current addressability, readiness, workflow-position and workspace
  admission in application commands; extract thin HTTP handlers with unchanged
  schemas/errors. Convert raw-stream admission and snapshot reads to public
  contracts. Check stale answers, undeclared names and unavailable/no-live sessions
  through direct commands and real HTTP; retain replacement and terminal close codes.
- [ ] **T5 — Migrate recovery and remaining production consumers (R1–R4).**
  Delegate resolved resumes and identity/applied-state writes to sessions while
  retaining eligibility and the pre-prompt exception in recovery composition.
  Wire a single service at construction and migrate review/ship/capture, cleanup,
  explicit Continue, observation and shutdown. Preserve startup writer blocks,
  graceful exits, purge transactions and last-applied-policy continuation.
- [ ] **T6 — Enforce the completed session boundary (R1).**
  Extend owner/public-surface/retired-import checks and negative fixtures for local,
  module-level and type-only violations. Delete obsolete modules and symbols,
  update all checked source/test/harness/script import paths, and retain only the
  two unrelated existing workflow exceptions. No cross-owner session internals or
  runner session-store/native-protocol imports remain.
- [ ] **T7 — Verify existing behavior and uncertain boundary edges (R1–R4).**
  Migrate existing session/RPC/agent/API/model-policy/workflow/recovery/WS tests and
  affected persistence, reconciliation, cleanup and delivery consumers. Keep
  regression coverage for exact active/auxiliary policy changes, failed applied
  writes, replacement identity mismatch, busy refusals and rich frame isolation.
  Add only missing behavioral regressions for uncertain edges introduced by the
  ownership move: cancellation leaving a child usable, direct-command admission
  bypass, or replacement/control contention. Prove the owner can ensure/resume a
  supplied session and policy without workflow persistence through a throwaway
  in-process smoke using the executable fake and an isolated database; do not use mock echo
  tests as proof of ownership. Verify receipt-before-idle and promptless-step parity.
  Retain advisory hook/threshold tests and idle-versus-exit/debounce races; retain
  the native ask-timeout preflight, oversized-frame and malformed-frame coverage.
- [ ] **T8 — Exercise real local session journeys (R2–R4).**
  Use the `local-e2e` skill with a unique state root/free port and the real frontend.
  Run `ask-approval`, `crash-recovery` and `cleanup`; use existing scenario controls
  for a working/waiting session. In the browser answer asks/approvals, steer, queue
  a follow-up, interrupt, and observe another session remaining independent.
  Author a temporary two-step same-session workflow with differing accepted
  auxiliary policies; pause between steps with a workflow gate so the transcript
  can be attached before replacement. Continue in the browser and verify unchanged
  native identity via `ompctl sessions`/`transcript`, visible policy change, bounded
  replay and reconnect without a terminal crash. Repeat with a startup failure
  armed through `ompctl scenario get-state-fails --task <id> --startup` for the replacement;
  observe the failure and absence of a dependent prompt. Restart and confirm
  restored recorded policy and workflow position without a new decision. Record
  dashboard/raw-stream separation; `ws-watch` is a recorder, not a runnable
  scenario. Shut down and wipe only the isolated harness environment afterward.
- [ ] **T9 — Reconcile durable documentation and rationale (R1–R4).**
  Update all nine contributor pages in SPEC's Documentation impact; document exact
  state ownership, public execution/read contracts, failures and remaining legacy
  owners. Add the session-boundary ADR and index link, with no product-contract
  reversal. Check that operator interaction/workflow references remain accurate
  and intentionally unchanged. No package README or parallel documentation tree.
- [ ] **T10 — Run final integration verification (R1–R4).**
  Run `make build`, `make test`, `make lint`, and `make typecheck` after integration;
  resolve failures rather than weakening behavioral coverage or boundary rules.
  Record the precise focused tests, browser journeys and external fake conditions
  exercised, distinguishing local harness proof from real-provider/native QA.
- [ ] **T11 — Re-check the delivered change against the vision (R1–R4).**
  Confirm native richness, deterministic workflow authority, accepted-policy
  fidelity, honest resumability, isolation and the reconnectable browser remain
  intact. Confirm no sibling extraction or excluded security/archival capability
  is claimed delivered, and leave `docs/VISION.md` unchanged.
