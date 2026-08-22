# ADR 0007: Integrate agents through supervised native Omp RPC processes

- Status: Accepted
- Date: 2026-07-19

## Context

Ompire must do more than display an agent terminal. It needs machine-readable lifecycle state, streamed events, request acknowledgement, steering and follow-up controls, questions and approvals, subagent activity, session statistics, and a path to resume a recorded session. The integration must also let the daemon supervise each agent independently and distinguish protocol responses from unsolicited events without coupling the control plane to terminal rendering.

Omp exposes these orchestration capabilities through its native newline-delimited JSON RPC protocol. Approval requests require the UI-capable RPC mode: plain RPC does not advertise an interactive UI and therefore cannot deliver all approval prompts for an external operator to answer. The protocol multiplexes ID-correlated responses and asynchronous push events on one output stream, so a strict request-then-read-response exchange would lose or misclassify events.

The native protocol is specific to Omp and includes event payloads that can evolve with Omp. Fully modelling every frame in the daemon would duplicate an upstream protocol surface and make unrelated additions or payload changes into control-plane compatibility failures. Ompire needs typed validation only for fields that affect orchestration, request correlation, lifecycle state, questions, and approvals; the remaining event content is presentation data and can pass through without interpretation.

The integration was implemented and validated against live Omp on 2026-07-19. The current implementation, durable product direction, and recorded design agree that Omp is the first-class agent and that its structured RPC capabilities should remain available behind a small internal process/session boundary. This ADR backfills that accepted decision using its recorded acceptance date.

## Decision

Ompire integrates each named agent session through one daemon-supervised `omp --mode rpc-ui` child process running inside that task's execution environment. The daemon communicates with the child over stdio using newline-delimited JSON and owns the process lifecycle, readiness handshake, input serialization, output reading, exit observation, and termination.

The protocol boundary has these rules:

- Every request that expects acknowledgement carries a daemon-generated ID. A single output reader correlates response frames by ID while allowing push events to interleave in any order.
- The daemon validates only the frame types and fields it must act on for readiness, response correlation, lifecycle tracking, operator questions, and approvals. Unknown frame types and uninterpreted fields remain opaque and pass through unchanged to event consumers.
- Malformed or oversized input is contained at the connection boundary. It must not corrupt request correlation or terminate supervision of unrelated agent processes.
- Each named session has an independent child process. A crash or blocked stream in one session must not take down another session or the daemon.
- Omp session recording remains enabled so the native session can support inspection and recovery without requiring the managed process to stay alive.
- Omp-specific framing and process details remain behind a small internal agent/session interface. Supporting another agent protocol requires a separate adapter or a superseding decision; it must not reduce the Omp integration to terminal emulation or a lowest-common-denominator protocol.

The invariant is that orchestration uses structured Omp RPC messages across a supervised process boundary. Ompire does not infer authoritative agent state by scraping terminal output, and unrecognized protocol content does not become invalid merely because the daemon does not interpret it.

## Consequences

Ompire receives explicit state and event signals for workflow execution, attention, questions, approvals, statistics, steering, subagents, and recovery. The browser-facing presentation can evolve independently because the daemon forwards event content it does not need to understand. New upstream event types can reach consumers without a daemon release when they do not alter an interpreted contract.

One process per named session provides crash isolation, independent working directories and environments, and direct lifecycle control. It also costs more memory and process-management work than multiplexing sessions in one server. The daemon must drain stdout and stderr continuously, enforce bounded startup, handle EOF and partial failure, fail pending requests when a child disappears, and provision stream limits and buffering for large tool-output frames. Backpressure and buffer limits can discard presentation history; durable transcripts and restart recovery remain separate concerns from the live RPC transport.

The opaque-by-default policy narrows compatibility risk but does not eliminate it. Changes to frame delimiters, request fields, readiness, response correlation, or any interpreted lifecycle and operator-interaction fields require coordinated updates and live compatibility verification. Conversely, treating a frame as opaque means it cannot drive authority-bearing behavior until Ompire adds narrow validation and explicit handling for the required fields.

Native RPC preserves Omp-specific capabilities instead of constraining the product to a portable subset. The accepted cost is dependence on Omp's bespoke protocol and command-line behavior. The internal boundary should make a future adapter possible, but it is not a promise that all agent implementations expose identical semantics or can participate in every workflow.

`rpc-ui` exposes approval requests to the daemon, but some approval payloads are less structured than protocol alternatives and may not contain raw tool arguments or file locations. Rich policy decisions may therefore require upstream protocol improvements rather than parsing display text. The control plane must never infer authority from presentation-only strings.

Process isolation is not a complete security boundary. Credential delivery, container isolation, network access, tool authorization, and host-side review and publishing authority are separate decisions. The native RPC child receives only the capabilities assigned to its task environment; selecting this protocol does not justify exposing host credentials or privileged operations to the agent.

This decision should be revisited through a superseding ADR if Omp no longer provides the orchestration features Ompire requires, protocol churn makes the interpreted boundary operationally unsafe, process-per-session cost becomes prohibitive, or supporting non-Omp agents becomes a product requirement that cannot be met with independent adapters.

## Alternatives considered

### Agent Client Protocol

ACP provides a standardized, typed ecosystem, compatibility with multiple coding agents, multi-session servers, and richer structured permission requests. It was rejected because the available Omp ACP surface omits orchestration-specific capabilities Ompire relies on, including authoritative state snapshots, subagent progress, steering and follow-up queue control, and several automation hooks. Implementing those features as unstable extensions would retain Omp-specific coupling while losing native capability, and multiplexing sessions through one process would expand the blast radius of a crash.

### Terminal or PTY emulation

Driving the interactive TUI would preserve visual feature fidelity and avoid depending on an RPC schema. It was rejected because ANSI output and terminal titles are not an authoritative state protocol. Status, cost, tool activity, questions, and completion would require fragile screen scraping, while commands would require synthesized keystrokes. Terminal access remains useful for hands-on recovery of a recorded session, but not as the orchestration boundary.

### In-process Omp SDK

Embedding the Omp SDK would provide the richest access and avoid serialization overhead. It was rejected because it would couple the trusted daemon to Omp's JavaScript runtime and exact package version, expose process-global SDK state, and remove crash isolation between the agent and control plane. A wedged or crashing agent could then affect every managed session. The stdio process boundary costs supervision and serialization work but contains those failures and permits independent session lifecycles.
