# Build Ompire

This documentation is for contributors changing Ompire itself. If you want to
run Ompire rather than modify it, read [Use Ompire](../use/index.md).

Ompire is a Python control plane with a React presentation layer. The daemon
owns everything that matters — process supervision, state, credentials,
review, and publishing — and the frontend renders what the daemon reports. The
split is deliberate and load-bearing; most of the architecture follows from it.

## Start here

[Set up a development environment](tutorials/development-environment.md) gets
both halves building and both test suites passing.

Then read [Architecture overview](explanation/architecture.md) before changing
anything structural.

## Tutorials

- [Set up a development environment](tutorials/development-environment.md)
- [Deliver a change end to end](tutorials/deliver-a-change.md)

## How-to guides

- [Build, test, and run](how-to/build-test-run.md)
- [Run the local end-to-end harness](how-to/run-local-e2e.md)
- [Run the dogfooding QA loop](how-to/run-the-qa-loop.md)
- [Write documentation](how-to/write-documentation.md)
- [Write an architecture decision record](how-to/write-an-adr.md)

## Reference

Repository and schema:

- [Repository layout](reference/repository-layout.md)
- [Daemon module map](reference/daemon-modules.md)
- [Database schema](reference/database-schema.md)

Daemon internals:

- [Daemon core](reference/daemon-core.md) ·
  [Daemon API](reference/daemon-api.md) ·
  [WebSocket protocol](reference/websocket-protocol.md)
- [Agent integration](reference/agent-rpc.md) ·
  [Workshop lifecycle](reference/workshop-lifecycle.md)
- [Crash recovery](reference/crash-recovery.md) ·
  [Local testing harness](reference/local-testing.md)

Behavior an operator sees is documented in the [operator
reference](../use/index.md#reference), not here.

## Explanation

- [Architecture overview](explanation/architecture.md)
- [Why the control plane is trusted and the agent is not](explanation/trust-model.md)
- [The change workflow](explanation/change-workflow.md)
- [Decision log](explanation/decisions.md)

## Ground rules

- Durable rationale goes in an [ADR](../adr/README.md). Current behavior goes in
  the reference section of the audience it serves —
  [operator](../use/index.md#reference) or [contributor](#reference). Neither
  goes in code comments beyond a short `ADR-NNNN` backlink at the boundary that
  enforces a decision.
- Both test suites and the linter must pass before committing. See
  [Build, test, and run](how-to/build-test-run.md).
- Changes are delivered through the workflow described in
  [The change workflow](explanation/change-workflow.md).
