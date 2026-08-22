# ADR 0002: Run Ompire as a local daemon with a stateless web UI

- Status: Accepted
- Date: 2026-08-22

## Context

Ompire supervises agents through child-process stdio. Those pipes cannot be reattached after their owning process exits, so the process that starts an agent must outlive casually closed presentation surfaces. Agent execution, workflow progress, credentials, notifications, and recovery therefore need an owner whose lifetime is independent of any browser window. This constraint favors a long-running user service over a desktop application whose process lifetime is coupled to its window.

The implementation follows that boundary. The daemon owns agent supervision, workflow execution, review and publishing operations, notifications, and startup recovery. A connecting browser receives an authoritative state snapshot before live events, and the React client reconnects and rebuilds its in-memory view from daemon messages. Closing or reloading the UI does not own or terminate running work.

This is a local, single-user control plane. The daemon binds to loopback by default and protects REST and WebSocket access with a generated bearer token. The architecture does not yet provide a multi-user authorization model or a hardened remote-access boundary.

This ADR is a backfill. No reliable original acceptance date was recorded, so its creation date is used.

## Decision

Ompire runs as a long-lived per-user daemon and exposes a stateless web UI:

- The daemon is the authoritative owner of agent processes, control-plane state, credentials, notifications, external side effects, and recovery. Its lifecycle must not depend on a browser window.
- The React and TypeScript frontend is a presentation and command surface only. It may hold a transient projection of daemon state, but it must be able to discard that projection and reconstruct it from an authoritative daemon snapshot after reload or reconnection.
- The daemon serves the frontend and its APIs. It binds to loopback by default, and every REST command and WebSocket observation channel requires the daemon's bearer token.
- Native operating-system integration that must work without an open browser, including desktop notifications and process supervision, remains daemon-owned.
- A future desktop shell, tray helper, or alternate client may wrap or consume the same daemon APIs, but it must not become a second owner of execution state or supervised processes.

The invariant is that presentation clients are replaceable and disposable: disconnecting, reloading, or closing every client must not stop active work or remove the daemon's authoritative state, and a newly connected client must recover its view from the daemon.

## Consequences

Agent and workflow lifetimes are decoupled from browser lifetimes. The operator may close or reload the UI without terminating work, and multiple presentation clients can observe the same authority without coordinating ownership. Native notifications can continue when no browser tab is open. Recovery and external side effects remain centralized in one auditable process.

The daemon becomes operational infrastructure rather than an incidental web server. Installation must arrange a long-running user service, upgrades must preserve recovery behavior, and daemon failure can interrupt in-flight turns even when durable task or agent session data permits later resumption. The UI cannot safely invent state or continue control operations while disconnected; it must show the loss of connection and wait for a fresh authoritative snapshot.

The loopback-plus-bearer-token boundary is appropriate for a local single-user service, but it is not a complete remote or multi-user security design. Binding beyond loopback, adding LAN access, or introducing users with distinct authority requires a new decision covering transport security, token exposure, origin policy, authorization, and audit identity. A native wrapper does not by itself require revisiting this ADR if it remains a disposable daemon client.

## Alternatives considered

### Desktop application monolith

A desktop application could combine process supervision and presentation in one package and provide native window and tray integration. It was rejected because closing or crashing the application window would also destroy the parent process that owns agent stdio, terminating active work. Separating a background component from that application would recreate the daemon boundary with additional packaging complexity.

### Native desktop or tray client as the primary UI

A thin native client could improve tray and notification integration. It was not selected because the browser UI avoids desktop packaging and the daemon can provide notifications independently of an open tab. A native shell remains compatible as an optional client, but it must use the daemon as the sole control-plane authority rather than owning agents or durable state.
