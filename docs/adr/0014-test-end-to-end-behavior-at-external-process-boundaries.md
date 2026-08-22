# ADR 0014: Test end-to-end behavior at external process boundaries

- Status: Accepted
- Date: 2026-08-22

## Context

Ompire's core behavior crosses several process and trust boundaries. Spawning a task, supervising an agent, reviewing its work, signing rewritten commits, publishing a branch, creating a pull request, and observing its eventual state depend on command-line tools, process streams, filesystem effects, local cryptographic state, container infrastructure, an LLM-backed agent, and a remote forge. Tests that replace calls inside the daemon can verify local control flow while missing the command construction, exit statuses, stream handling, working directories, process lifecycles, and artifacts that make the integrated behavior succeed or fail.

Running every check against the production stack has the opposite problem. It requires container infrastructure, network access, credentials, a forge repository, and an LLM provider. Those dependencies make failures slower and less deterministic, impose cost, make uncommon failure states difficult to reproduce, and create a risk of tests affecting real external resources. That stack remains necessary for dogfooding and final compatibility checks, but it is not a safe or repeatable default for development.

The useful boundary is therefore the daemon's existing subprocess and published control-plane interfaces. The daemon already treats external tools as executables with observable contracts: arguments, current directory, environment, exit code, standard streams, signals, and filesystem changes. A local substitute can preserve those contracts without introducing test-only branches into production code. Keeping Git, GPG, the project launcher, and the reviewer real retains the semantics and security-sensitive behavior that would be most damaging to approximate, while local substitutes remove the network, container, and model dependencies that prevent deterministic offline runs.

Executable substitutes carry a standing drift risk. A fake may continue satisfying the daemon while no longer matching the real tool, especially for the agent RPC protocol and container command behavior. Fidelity therefore needs its own evidence: sanitized recordings of real invocations, versioned provenance, replay against the substitutes, and comparison of daemon-observable outcomes between local and real environments.

The offline harness, its scenario matrix, the current feature documentation, and the fidelity checks agree on this boundary. They were completed together on 2026-08-22. This ADR backfills that accepted decision using the recorded completion date.

## Decision

Ompire tests complete task behavior through an offline end-to-end harness that runs the production daemon and frontend together with real Git, real GPG, the real project launcher, and the real reviewer. It substitutes only dependencies whose production form requires a remote forge, container runtime, or LLM-backed agent:

- local bare Git repositories and an executable forge client replace remote forge operations;
- an executable Workshop substitute replaces container lifecycle and execution;
- an executable agent substitute replaces the LLM-backed agent while speaking the native supervised RPC protocol.

Substitutes run as child processes through the same configuration and executable lookup mechanisms used in production. They must honor every part of the contract the daemon consumes: supported argument shapes, working directory, relevant environment, exit status, standard output, standard error, signals, and selected filesystem effects. Unsupported invocations fail explicitly. A substitute must not return plausible success for behavior it does not implement.

Production code remains unaware of the harness. It must not branch on a test mode, import a fake, bypass a subprocess boundary, or use an alternate internal API for end-to-end execution. Scenarios drive the system through published REST, WebSocket, process, and tool-control surfaces and assert externally observable task, repository, review, publishing, and recovery outcomes. All mutable state, identities, repositories, configuration, credentials, and process metadata used by a local run remain under a disposable state root.

Security-sensitive local behavior stays real. Git performs actual repository operations. GPG performs actual signing with a throwaway, passphrase-protected test identity, including locked and cached agent states. The project launcher and reviewer execute their production binaries unchanged. The reviewer is not replaced because it enforces the host-side review boundary; the local agent substitute removes the model dependency from the review loop instead.

Fidelity is part of the testing boundary, not an optional maintenance task. Real-stack invocations are recorded transparently in the isolated QA environment. Recordings must be sanitized before process streams or environment data are persisted, retain tool-version provenance, and contain only the minimal contract Ompire consumes. Conformance replays those invocations against executable substitutes and compares normalized exit codes, streams, and selected filesystem effects. Normalization removes nondeterministic identities, timestamps, paths, ports, and similar incidental values; it must preserve behavioral differences.

The local harness does not replace real-stack QA. Before releases and after relevant tool upgrades, maintainers compare recorded versions, refresh recordings when needed, run conformance, and compare representative daemon-observable outcomes between the local and real stacks. A divergence is treated as either substitute drift or newly discovered production behavior, not normalized away without review.

The invariant is that offline end-to-end tests exercise the same production process boundaries and observable contracts as deployment, while nondeterministic external services are replaced by explicit, failing-closed executables whose fidelity is checked against sanitized real-stack evidence.

## Consequences

Developers can exercise spawn, agent interaction, workflow routing, review, signed publishing, pull-request polling, crash recovery, cleanup, advisories, and failure recovery without network access, container infrastructure, production credentials, or model cost. Scenarios can deliberately produce locked keys, rejected pushes, malformed or stalled agent behavior, process crashes, and other states that are expensive or unreliable to arrange against the real stack.

Because production command construction and stream handling remain in the path, the harness detects errors that internal mocks cannot: wrong arguments, incorrect working directories, missing filesystem effects, signal mishandling, invalid RPC frames, unexpected exit codes, and assumptions about subprocess output. Real Git, signing, project launching, and review preserve important semantics at the publishing and trust boundaries. A successful local run therefore provides stronger integration evidence than tests that patch daemon functions, while remaining deterministic enough for frequent use.

The cost is maintaining executable substitutes and their state-control interfaces. They implement only the contracts Ompire consumes, not complete replicas of the external tools. New production invocations must be added deliberately, recorded where practical, and made to fail loudly until supported. The process-boundary approach also starts more real processes and performs more filesystem and cryptographic work than an in-process test, so it is heavier than the unit suite.

The Workshop substitute runs commands on the host rather than reproducing a container's mounts, installed tools, isolation, and environment. The agent substitute validates Ompire's RPC orchestration but does not validate a real model's reasoning, provider behavior, Omp's complete runtime, or unrecorded protocol extensions. REST- and WebSocket-driven scenarios do not by themselves prove browser presentation behavior. Real-stack dogfooding and browser verification remain required when those properties are in scope.

Recordings create a security obligation. Capture must be opt-in and isolated; credentials, passphrases, authorization headers, private keys, operator paths, prompts, and reviewer content must not enter committed fixtures. Sanitization happens before persistence, and promotion into golden fixtures is explicit. A recording that cannot be shown safe is discarded rather than partially redacted after the fact.

Fidelity checks reduce but do not eliminate drift. They cover recorded, daemon-consumed behavior and can become stale between tool upgrades. Tool-version provenance and periodic real-stack comparisons are therefore operational requirements. Releases made after a dependency change without refreshed evidence accept an explicit compatibility risk.

This decision should be revisited if substitute drift becomes more expensive than running a deterministic real tool, if Omp or Workshop provides a stable offline test backend that preserves the same boundaries, if Ompire stops using subprocess contracts, or if the system becomes a remote multi-user service whose isolation properties cannot be represented by a disposable local state root. Any replacement must continue to keep production code test-unaware and retain real evidence for security-sensitive publishing behavior.

## Alternatives considered

### Use the real QA stack for every end-to-end test

The real forge, container runtime, Omp agent, and model provider provide the highest immediate production fidelity and avoid maintaining substitutes. This was rejected as the default because it requires network access, credentials, external resources, container support, and model spend; introduces nondeterminism; and makes destructive and recovery cases harder to arrange safely. The real stack remains a complementary dogfooding and conformance environment rather than the everyday test environment.

### Mock daemon functions or add a test execution mode

In-process mocks would be faster and simpler to steer, and production test branches could bypass unavailable infrastructure. This was rejected because both approaches skip the subprocess contracts where integration defects occur. They can pass while arguments, streams, signals, working directories, or filesystem effects are wrong, and a test mode creates behavior that production never executes. Unit tests may still isolate local logic, but end-to-end evidence must cross the production boundaries.

### Replace every external executable

Faking Git, GPG, the project launcher, and the reviewer as well as networked and nondeterministic dependencies would reduce prerequisites and make all outcomes directly scriptable. This was rejected because repository mutation, signature creation and verification, launch integration, and host-side review are central semantics and trust boundaries rather than incidental dependencies. Approximating them would make the harness easier to run by removing the behavior it most needs to prove.

### Run real Omp with a deterministic model plugin

Intercepting model calls inside real Omp would retain more RPC and runtime fidelity than an executable agent substitute. It was rejected for the current system because it still requires a deterministic prompt-driven model, adds plugin-API and real-runtime dependencies to every local run, and does not remove the need for controllable failure scenarios. It remains a plausible replacement if recorded protocol drift makes the executable agent disproportionately expensive to maintain.
