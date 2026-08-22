# ADR 0003: Implement the trusted control plane in Python

- Status: Accepted
- Date: 2026-08-22

## Context

Ompire's daemon is a security and authority boundary. It chooses process working directories and environments, supervises coding agents, receives untrusted protocol frames and client commands, holds local credentials, relays operator decisions, and performs repository review and publishing operations. A defect in that control plane can expose credentials, execute commands with unintended authority, or misrepresent an external side effect. The operator's ability to understand and audit the implementation is therefore more important than sharing a language with the browser or optimizing compute throughput.

The workload is predominantly asynchronous I/O at single-operator scale: a small number of child processes, line-oriented RPC streams, HTTP and WebSocket traffic, local database operations, and occasional external commands. It does not require a highly parallel compute runtime. Python's asynchronous ecosystem covers these boundaries while remaining the language in which the operator can most confidently review security-sensitive behavior.

The implementation uses Python 3.12 or newer, `asyncio`, FastAPI, Pydantic, SQLAlchemy Core, and Alembic. Its production dependency set is intentionally small. The React and TypeScript frontend is a separate presentation surface; sharing its language or protocol types is less valuable than keeping privileged behavior in the more readily audited daemon.

This ADR is a backfill. No reliable original acceptance date was recorded, so its creation date is used.

## Decision

Ompire implements its trusted control plane in Python 3.12 or newer:

- Asynchronous control-plane orchestration uses `asyncio`.
- FastAPI provides the HTTP and WebSocket application boundary, and Pydantic validates data the control plane must interpret.
- Production dependencies remain deliberately few and individually reviewable. Adding a framework or library to privileged paths requires a concrete benefit that outweighs the additional audit and supply-chain surface.
- Process supervision, authorization enforcement, path and command validation, approval handling, workflow policy, credential access, review, publishing, and recording of external side effects remain on the Python side of the boundary.
- React and TypeScript remain on the untrusted presentation side. The frontend may collect commands and project daemon state, but correctness or security must not depend on client-side policy enforcement.

The invariant is that every authority-bearing decision and privileged side effect is validated and executed by the Python control plane. A frontend, protocol client, or other replaceable presentation component must not become a second trusted implementation of control-plane policy.

## Consequences

The most security-sensitive code uses the operator's strongest review language, and one runtime owns the daemon's authorization and orchestration rules. Python's I/O libraries fit the workload without introducing cross-thread state coordination, while FastAPI and Pydantic provide explicit network validation boundaries. A small dependency set keeps upgrades, vulnerability review, and behavioral auditing tractable.

The language choice does not itself provide memory safety, static exhaustiveness, or secure behavior. The daemon must compensate with explicit validation at trust boundaries, narrow types, tests of authority-bearing behavior, careful subprocess construction, and dependency review. Dynamic failures in privileged paths remain possible and must fail closed where authority is involved.

The frontend cannot directly reuse the daemon implementation or make shared TypeScript types authoritative. Some request, response, and state shapes are represented in both languages, creating synchronization cost. That cost is accepted; the daemon's validation and policy remain authoritative even when the client has corresponding compile-time types.

Operators and contributors need a Python 3.12-or-newer runtime and the daemon's managed environment. Python is not optimized for CPU-heavy parallel work, so expensive computation should not be added to the event loop. This decision should be revisited through a superseding ADR if Ompire must be distributed as a self-contained cross-platform binary, if measured control-plane load no longer fits the asynchronous model, or if the operator's security-review constraints materially change.

## Alternatives considered

### Bun and TypeScript

Using TypeScript throughout could share protocol types and selected client code with the frontend, use the native agent RPC client directly, and reduce the number of languages in the repository. It was rejected because those reuse benefits do not outweigh placing the trusted control plane in a language the operator is less confident auditing. Shared types would also not remove the need to validate untrusted runtime data.

### Go

Go could provide a static binary, a smaller runtime deployment surface, stronger compile-time typing, and straightforward concurrency. Those are material advantages for widely distributed infrastructure. They do not provide a decisive benefit for the current local, single-user, I/O-bound daemon, and they would reduce operator auditability relative to Python. A future distribution or load requirement may justify reconsidering Go, but it does not justify the present review cost.
