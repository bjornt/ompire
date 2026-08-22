# ADR 0004: Use REST for commands and WebSocket snapshot-then-deltas for observation

- Status: Accepted
- Date: 2026-08-22

## Context

Ompire's browser must both issue bounded commands and observe work that continues independently of any request or browser connection. Command submission needs explicit validation, authorization, success or failure semantics, and a finite response. Observation must carry asynchronous registry changes, workflow progress, attention changes, and agent output with low latency. Treating those unlike interactions as one protocol would couple command correctness to a long-lived connection and make reconnect behavior part of every mutation's semantics.

The frontend is stateless and replaceable, while the daemon is the source of truth. A browser can disconnect, miss events, or be opened after work has started. Replaying an unbounded event history into each client would turn the observation channel into a durable log and duplicate control-plane persistence concerns. A fresh state snapshot followed by incremental events lets a reconnecting client recover current state without requiring the browser to preserve prior state or the daemon to retain every dashboard event.

Raw agent events have a different traffic shape and audience from aggregate control-plane state. They can be high-volume, are useful only while viewing a particular named session, and need a short replay window to bridge UI reconnects. Sending every agent frame through the aggregate dashboard stream would impose transcript traffic and buffering costs on clients that do not consume it.

The current daemon API, frontend state synchronization, and per-session event channels implement this separation. This ADR is a backfill. No reliable original acceptance date was recorded, so its creation date is used.

## Decision

Ompire uses separate transports for control-plane commands and observation:

- Every state-changing client operation is an authenticated REST request with a validated JSON body where input is required. WebSocket channels do not accept commands.
- The main authenticated WebSocket sends a full current control-plane snapshot as its first message, followed by typed incremental events in a common `{seq, ts, type, payload}` envelope.
- A reconnect is a resynchronization boundary. The client treats the next snapshot as authoritative replacement state, then applies later deltas; the main event stream is not a durable audit log.
- Raw agent events use a separate authenticated WebSocket for each `(task, session)` pair. A new connection receives that session's bounded buffered events and then its live events. Aggregate dashboard clients do not receive those frames unless they explicitly open the session channel.
- The daemon remains authoritative for command outcomes and observed state. Client state derived from deltas must always be replaceable by a fresh snapshot.

The invariant is that mutations cross a finite, validated REST boundary, while WebSockets are read-only projections of daemon-owned state and event streams. A client must be able to recover current control-plane state by reconnecting without replaying commands or relying on its pre-disconnection state.

## Consequences

Command authorization, validation, and errors use ordinary HTTP semantics and remain independent of WebSocket connection lifetime. Observation stays push-based and low-latency without making each browser poll every resource. Snapshot replacement gives reconnecting or newly opened clients a simple recovery path and prevents stale client state from becoming authoritative.

The transport split creates two client paths and two authentication handshakes. A command response and its corresponding observation event can arrive in either order, so presentation code must render daemon state rather than assume event ordering relative to the REST response. If a connection fails after a command reaches the daemon but before its response reaches the client, the client must reconcile from observed state rather than blindly replay a potentially non-idempotent command.

The main snapshot grows as new durable or currently authoritative control-plane state is added. Every such addition must be represented coherently in snapshot production and client replacement logic, with deltas for changes that connected clients must see. Ephemeral progress not included in the snapshot can be lost across reconnects by design; it must not be the sole record of an authority-bearing outcome.

Separate agent channels prevent transcript traffic from flooding dashboard clients, but they require one connection per viewed session and only provide bounded replay. Events older than the buffer are recovered from the agent's durable session material or other authoritative state, not from the WebSocket. Both main and per-session channels expose sensitive local state and therefore require the same authorization boundary as REST even though they are read-only.

This decision should be revisited through a superseding ADR if Ompire requires durable offline event delivery, multi-user fan-out at a scale where full snapshots or per-session sockets are impractical, or a non-browser client environment in which a different streaming transport materially simplifies the system without weakening command semantics or recovery.

## Alternatives considered

### Bidirectional WebSocket for commands and events

A single bidirectional protocol could reduce connection and routing ceremony and allow commands and events to share correlation machinery. It was rejected because it would combine finite, authority-bearing mutations with an interruption-prone observation stream. The daemon would need to recreate request validation, error, retry, and reconnect semantics inside the socket protocol, and clients could not use standard HTTP behavior for commands.

### REST polling for observation

Polling would avoid a persistent observation connection and use one protocol for the entire API. It was rejected because workflow progress, attention changes, and agent lifecycle events are asynchronous and latency-sensitive. Frequent polling would create repeated full reads while still risking delayed or missed transient progress; infrequent polling would make the operator interface stale.

### One WebSocket carrying aggregate and raw agent events

Multiplexing all events onto the main socket would simplify connection management and provide one sequence of frames. It was rejected because raw agent traffic is substantially higher-volume and narrower-interest than aggregate task state. Separate per-session channels keep dashboard cost proportional to aggregate state and let focused clients opt into only the transcripts they display.
