# ADR 0015: Keep agent credentials behind narrow brokers

- Status: Proposed
- Date: 2026-08-22

## Context

Ompire runs coding agents, repository code, dependency scripts, and tools inside a task sandbox. All of them are untrusted and share the authority available inside that sandbox. A provider credential placed in the task workspace or process environment is therefore a credential given to the agent: the agent can read it, print it, persist it in a transcript or artifact, commit it, or send it over the network. Restricting the credential to one provider or repository reduces its blast radius but does not prevent disclosure or unauthorized use within that scope.

The current implementation does not satisfy that boundary. It accepts an arbitrary operator-configured map of agent environment values and constructs the supervised agent command with those values in an `env` prefix. Values intended as secrets are consequently available to the agent and also appear in the host-visible process argument vector. The historical design accepted configuration-file and logging exposure as manageable on a single-user machine, but it did not examine process-list exposure or reconcile direct credential delivery with an untrusted sandbox.

The supported deployment already has a different mechanism: the Workshop exposes a tunnel to an authentication gateway. The gateway retains provider credentials outside the task and gives the sandbox access to a service operation rather than to the reusable secret behind it. This matches the broader security model, which keeps signing and forge credentials on the trusted side and requires host-side credential agents or narrow brokers for authenticated operations.

The implementation and durable security direction therefore conflict. This ADR records the proposed reconciliation and remains proposed until arbitrary agent environment injection is removed and supported deployments use a brokered path without exposing raw credentials to task processes.

## Decision

Ompire keeps every raw credential required by an agent-accessible service outside the task sandbox. A process inside the sandbox may access an authenticated service only through a trusted, narrow broker reached over a task-scoped channel such as a Workshop tunnel.

The broker retains and applies the credential. It exposes only the service capability the task needs and provides no operation for retrieving the underlying secret. The channel must be scoped no more broadly than the task and service, must be unavailable to unrelated tasks, and must be revoked when the task environment is removed. Non-secret routing data may enter the sandbox; provider keys, bearer tokens, refresh tokens, credential files, credential-agent sockets with broader authority, and equivalent reusable secrets may not enter its workspace, environment, argument vector, prompt, transcript, artifact, or workflow state.

The daemon does not support a generic credential-bearing environment fallback. A deployment without the required broker fails closed instead of injecting direct provider credentials or silently widening the task's authority. This decision covers credentials needed by code running inside the agent sandbox. Host-side review, signing, publishing, and forge authority remain outside the sandbox under their own decision.

The invariant is that a task can exercise only an explicitly brokered service capability; it cannot read, recover, or reuse the credential that authorizes that capability outside the brokered channel.

## Consequences

A compromised or confused agent can use an allowed service while the task is active, but it cannot extract the provider credential from its environment or discover it in the host process list. Repository code and dependency scripts gain no more credential access than the agent itself. Removing reusable secrets from task state also prevents them from leaking through commits, prompts, transcripts, outcome files, crash diagnostics, backups, or retained task directories.

The broker becomes required infrastructure for real agent execution. Deployments that currently rely on direct provider environment variables must install and configure a compatible broker before upgrading; there is no direct-key compatibility fallback. Obsolete credential-bearing configuration must be rejected explicitly rather than ignored, so an operator does not mistake an unauthenticated or differently authenticated agent for the intended setup.

Broker availability now affects agent startup and continued provider access. Startup must fail clearly when the channel is absent, and recovery must recreate only the channel assigned to the recovering task. Broker and tunnel failures must not cause the daemon to fall back to a broader credential path. Credential rotation can occur outside the task without rewriting its state, but broker operation, channel creation, revocation, and diagnostics become part of the trusted operational boundary.

A broker limits credential disclosure, not misuse of the capability while it is available. It must constrain the reachable service and operations, isolate tasks, avoid returning upstream authorization material in responses or errors, and treat request and response logs as potentially sensitive. Rate, cost, model, and network policy remain separate controls; the existence of a tunnel does not by itself make an unrestricted upstream proxy narrow.

This decision should be revisited if an agent-accessible service can authenticate tasks without reusable secret material, or if the sandbox can receive a cryptographically task-bound, non-exportable capability whose authority and lifetime are no broader than the brokered channel. A replacement must preserve the invariant that neither the agent nor other task processes can recover a credential usable outside their assigned capability.

## Alternatives considered

### Pass credentials through the child environment without exposing them in argv

Using the subprocess environment directly, a protected environment file, or another launcher mechanism would remove host process-list exposure and would support providers without broker infrastructure. It was rejected because it fixes only the secondary transport flaw. The untrusted agent, repository code, dependency scripts, and tools would still be able to read and exfiltrate a reusable credential.

### Retain an operator-configured direct-credential fallback

An explicit fallback would make Ompire usable where no authentication gateway is available and would leave the risk decision with a single-machine operator. It was rejected because the trust boundary does not depend on whether the agent runs for one user or many. Arbitrary configuration cannot make an untrusted task a safe secret holder, and a fallback would become a permanent alternate architecture that security-sensitive code and documentation must continue to accommodate.

### Proxy authenticated agent traffic through the daemon

The daemon could retain provider credentials and implement the provider-facing proxy itself. This would keep secrets outside the sandbox and avoid a separate gateway process. It was rejected because provider authentication, request forwarding, streaming, rate handling, and protocol compatibility would enlarge the trusted control plane and couple it to each provider. A dedicated broker keeps that concern behind a narrower interface while preserving the daemon's role as task and workflow orchestrator.
