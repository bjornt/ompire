# ADR 0012: Derive attention centrally from session state

- Status: Accepted
- Date: 2026-07-21

## Context

Ompire supervises work that can continue without an operator for long periods, but some conditions require a prompt human response. A session may be starting, actively working, between turns, retrying, waiting for input or approval, stalled, under review, or failed. Workflows can also wait at operator gates independently of any one session. With multiple tasks and named sessions active at once, the system needs one reliable answer to “what needs me now, and why?” without making every client understand agent protocol details.

Raw agent events are not a suitable attention contract. Most frames are intentionally opaque to the control plane, event ordering can race with process exits and queued turns, and absence of frames can itself become meaningful. Browser tabs also disconnect and reconnect. If each client inferred urgency from raw frames or maintained its own set of important statuses, desktop notifications, badges, task grouping, and future clients could disagree or silently miss new states.

Attention is policy layered over authoritative operational state. Session state must first be classified from the small set of lifecycle events and agent frames that the control plane needs for orchestration. Urgency can then be derived independently, allowing the state machine to describe what is happening while a separate policy describes how strongly it should attract the operator. Task-level attention must additionally combine all named sessions and workflow gates so one task does not produce competing answers or duplicate notifications.

Not every useful signal is authoritative enough to affect urgency. In particular, free-form assistant text that resembles a question can suggest that an idle session may be waiting, but it cannot prove that the operator is blocking progress. Context use, token use, and cost are similarly useful presentation signals rather than lifecycle states.

The centralized model was implemented and dogfooded on 2026-07-21. Later support for named sessions and workflow gates extended its aggregation sources without changing the decision. The implementation, current requirements, and the product principle that human attention is scarce agree on the boundary. This ADR backfills that accepted decision using the recorded implementation date.

## Decision

Ompire derives operator attention in the trusted daemon from daemon-owned session and workflow state. Clients consume the derived result; they do not independently translate raw agent events, session statuses, or workflow statuses into attention severity.

The daemon owns one guarded state machine per named session. It derives state only from supervised process lifecycle, control-plane operations, and the minimal interpreted subset of agent frames needed for orchestration, questions, approvals, retries, and liveness. Every transition records a reason naming its evidence. Unknown agent frames remain opaque and do not create states. Transition guards resolve competing evidence deterministically, including process exit racing with delayed frames or timers.

One pure control-plane mapping assigns every session status to exactly one ordered attention tier:

- `starting` and `working` are `silent`;
- `idle` and `retrying` are `badge`;
- `waiting-input`, `stalled`, and `reviewing` are `notify`;
- `waiting-approval` and `failed` are `interrupt`.

A workflow waiting at an operator gate contributes `notify` attention without inventing a session. Any new authoritative status must receive an explicit tier before it is used. Channel preferences may control whether a tier produces a badge, desktop notification, or sound, but they do not change the authoritative classification.

The daemon aggregates attention once per task across all of its named sessions and any waiting workflow gate. The most urgent active source wins according to `silent < badge < notify < interrupt`; when it clears, the next most urgent source becomes effective. The published entry identifies the source and reason. The daemon exposes the effective entries in the authoritative connection snapshot and publishes deltas as they change, so reconnecting and live clients use the same state.

Heuristics and resource signals remain advisories or decorations. They may explain or decorate an authoritative state, but they do not create session states, raise attention tiers, or trigger operator-blocking behavior. In particular, question-like free-form text must not turn `idle` into a waiting state.

The invariant is that a given set of daemon-owned session states and workflow gates produces one deterministic task-level attention tier regardless of which clients are connected. Agent claims, client heuristics, and presentation preferences cannot redefine that tier.

## Consequences

Desktop notifications, badges, task grouping, and future clients share one severity policy. A client can reconnect from a snapshot without replaying raw events, and multiple clients cannot disagree merely because they implement different status lists. One task-level entry also prevents concurrent sessions from stacking duplicate notifications; a more urgent source supersedes a less urgent one while the lower-tier source remains available to become effective later.

The agent protocol boundary stays narrow. Most frames pass through without validation, while the state machine interprets only evidence required for orchestration and attention. This reduces coupling to protocol changes and prevents an agent from declaring its own urgency or completion. The control plane, however, becomes responsible for keeping its interpreted subset, status set, and attention mapping exhaustive and synchronized. A new status omitted from the mapping could be under-signalled, so status additions require attention-policy review and changed-contract tests.

Separating state from tier policy allows urgency and notification-channel preferences to evolve without rewriting transition mechanics. It also means maintainers must preserve two explicit layers and must not add ad hoc urgency checks in clients. Workflow-level sources require the aggregator because they cannot be represented honestly as session state.

The ordered tiers deliberately compress several causes into one severity scale. This makes fleet sorting and notification behavior predictable, but it cannot express incomparable priorities or show every simultaneous source in the single effective entry. Detailed session and workflow state remains available for explanation. If future scheduling needs multiple independent urgency dimensions, the aggregation model will need a new decision rather than client-side exceptions.

False-positive heuristics remain quiet decorations, avoiding notification fatigue and accidental claims that an operator is blocking work. The accepted cost is that an agent which asks only in unstructured prose may remain `idle` with badge-level attention. Agents must use the structured question path when a response is required.

Attention delivery can degrade independently of classification. If the host notification service is unavailable or a channel is disabled, daemon attention entries and client badges still work. Active attention is operational derived state, not by itself a durable audit log: reconnecting clients receive a snapshot, while daemon restart behavior follows session and workflow recovery. The durable history and provenance boundary is a separate decision.

This decision should be revisited if Ompire becomes a multi-operator service requiring per-operator attention policy, if authoritative workflow sources cannot be represented by one ordered severity scale, or if the agent protocol provides a trusted semantic lifecycle that can replace local interpretation. Any replacement must still produce a single control-plane-owned result for all clients and keep heuristics distinct from authoritative blocking state.

## Alternatives considered

### Let each client derive attention from session and workflow status

Client-side derivation would keep the daemon simpler and let each interface choose its own notion of urgency. It was rejected because the browser, desktop notifier, and future clients would duplicate policy, drift when statuses change, and reconstruct different results after reconnect. Presentation may vary by client; authoritative severity may not.

### Store the attention tier inside each session state

Adding a tier field to every session record would make individual session snapshots self-contained. It was rejected because it couples lifecycle classification to notification policy, duplicates information that is a pure function of status, and cannot naturally represent workflow gates that have no session. A separate mapping and task aggregator keep those responsibilities explicit.

### Let agents explicitly report that they need attention or are done

A manager-specific tool or agent-reported completion state could carry semantic intent directly and avoid some frame interpretation. It was rejected because agent compliance is probabilistic and agent output is not authoritative control-plane evidence. Structured questions already have a supervised protocol path, while a completed turn is `idle`; workflow outcomes, not a self-declared `done` attention state, determine whether orchestration advances.

### Promote question-like prose and other heuristics to session states

Heuristics could catch agents that ask for help without using the structured question path. They were rejected as authoritative state because false positives would notify operators and could incorrectly imply that progress is blocked. Keeping them as advisories preserves the useful hint without allowing uncertain text classification to alter control flow or urgency.
