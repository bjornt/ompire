# ADR 0038: Own modules and compose local transactions

- Status: Accepted
- Date: 2026-09-10

## Context

The daemon grew as flat modules with one very large REST file. Every
state-changing operation was reachable only through an HTTP route, so the
launch acceptance transaction, the filesystem observation it reviews against,
and the supervision of preparation jobs all lived inside transport code. A
maintainer had to read a route to understand or exercise task launch, and
nothing prevented a new dependency from re-coupling storage, presentation,
and transport as the codebase grew.

At the same time, the daemon deliberately runs as one process against one
local SQLite database, and many correctness properties — profile deletion
versus project assignment, launch acceptance versus a concurrent edit,
result retention versus purge — depend on every owner admitting through the
same `BEGIN IMMEDIATE` write reservation. A naive "extract everything into
services with their own stores" response would fragment exactly that local
consistency boundary.

This decision records the first delivered slice of the modular-monolith
direction (see `epics/modular-monolith/`): the work and launch boundary.

## Decision

Modules are owned, and ownership is expressed through enforced import
boundaries rather than separate processes or databases:

1. **Platform values are shared, not owned.** The SQLite write reservation
   lives in `platform/transactions.py` and imports the standard library and
   SQLAlchemy only. The model-value vocabulary — thinking levels, the four
   abstract roles, and the pure `RoleBinding` value with its validation —
   lives in `model_config.py` with no product imports. No domain owns these;
   every domain uses them.

2. **Accepted work has one owner.** Projects, model profiles, task records,
   launch resolution, accepted execution inputs, launch reconciliation, base
   checkout inspection/setup, and project file rules live in the `work/`
   package. Work modules import no transport, no application commands, and
   no projections.

3. **Operations are commands, not routes.** `application/launch.py` exposes
   `LaunchService.preview/accept` as typed in-process interfaces that own the
   acceptance algorithm end to end — observation outside every lock, the
   reserved re-resolution and token comparison, the task-plus-references
   transaction, and only then the post-commit event and the scheduled
   preparation job through the injected `SpawnScheduler`. `application/work.py`
   and `application/tasks.py` do the same for configuration commands and task
   reads. HTTP routes convert wire bodies and map domain errors; they hold no
   business rules. The same admission applies to a direct caller as to an
   HTTP request.

4. **One wire shape per concept.** The task projection (`task_payload`, and
   the launch resolution payload) lives in `oversight/tasks.py`, used by REST
   responses, events, and the WebSocket snapshot alike. Storage does not
   depend on presentation.

5. **The local consistency boundary stays whole.** Named module ownership
   does not give each owner its own database, queue, or retry machinery.
   Cross-owner atomicity is still one shared SQLite reservation composed
   inside a command, and direct in-process calls are the composition
   mechanism. There is no internal HTTP, message bus, or outbox.

The boundary is enforced by `daemon/tests/test_architecture.py`, an
`ast`-based import check in the ordinary pytest run, with a checked-in
exception table: each entry names the importing module, the imported module
and symbol, and the later epic change that owns removing it, and an
exception whose import disappears fails as stale.

## Consequences

- A maintainer can understand and exercise task launch without reading HTTP
  handlers, and tests can drive acceptance directly with typed inputs.
- New dependencies that would re-couple work storage to transport, commands,
  or projections fail the test suite, as does importing any retired module
  path.
- Migration debt is explicit and bounded: the workflow engine's private task
  mutators, delivery's and review's private spawn helpers, and similar
  pre-existing edges remain as named exceptions with owners, never wildcard
  permissions.
- The reservation primitive being platform code means every future extracted
  domain (isolation, sessions, workflows, delivery, artifacts) can share the
  same local transaction semantics without importing a product module.
- This ADR covers only the delivered work/launch slice. It does not declare
  the full target module map implemented, and it does not reverse ADRs
  0002–0005 (daemon/stateless UI/Python/SQLite), 0026–0028 (launch pinning),
  0031 (library), 0032–0033 (delivery authority), or 0034–0037
  (results/handoff).

## Alternatives considered

- **Extract each domain into a deployable service with its own store.**
  Rejected for now: the product is one operator on one machine; the
  consistency boundary is local SQLite; distribution would multiply failure
  modes the vision does not ask for.
- **Keep everything flat and enforce conventions by review alone.** Rejected:
  the boundary erodes silently under growth; the checker makes it a test.
- **Generic repositories and a command dispatcher.** Rejected: ownership is
  about who may depend on whom, not about uniform interfaces; a dispatcher
  would obscure the calls the boundary is meant to make visible.
- **Per-owner transaction managers.** Rejected: one reservation primitive
  with identical semantics is the point; two subtly different reservations
  is precisely the bug class the shared primitive exists to prevent.
