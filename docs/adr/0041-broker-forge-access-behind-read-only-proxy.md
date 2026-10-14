# ADR 0041: Broker forge access behind a read-only proxy

- Status: Accepted
- Date: 2026-10-14

## Context

The real QA loop needs an agent to observe GitHub: clone the bot's sandbox
repository, inspect pull requests, and read commit-signature and check-run
evidence. Those observations should use the real service without giving the
observing agent a credential. A repository-scoped PAT in a task is still an
exfiltratable secret shared with repository code, dependencies, and every tool
the agent runs. Reducing its scope or lifetime does not remove that problem.

[ADR-0015](0015-keep-agent-credentials-behind-narrow-brokers.md) requires
agent-accessible services to retain reusable credentials behind narrow brokers
and task-scoped channels. The model authentication gateway already demonstrates
that shape through a Workshop tunnel. Forge observation needs the same
boundary, but not publishing authority: the daemon under test signs and ships,
while the observing agent reads the result.
[ADR-0011](0011-keep-review-and-publishing-authority-outside-agent-sandbox.md)
and [ADR-0032](0032-bind-trusted-delivery-to-retained-candidates.md) continue to
own the trusted review and publication boundary.

The broker must also run on trusted machines without a daemon environment.
The required clients are ordinary `git` smart HTTP and repository REST reads
with `curl`; transparently supporting every GitHub client is not a requirement.

## Decision

Ompire exposes forge observation through a dedicated, standard-library-only
Python broker outside the task sandbox. The broker retains the bot PAT, accepts
an explicit repository allowlist, and permits only repository REST `GET`/`HEAD`
and git upload-pack clone/fetch. API writes, receive-pack push, account-level
operations, GraphQL, and other repositories are denied before an upstream
request. No sandbox PAT or direct-credential fallback is permitted.

The broker listens only on trusted-host IPv4 loopback, using plain HTTP through
a Workshop tunnel. The assigned tunnel, its reachability, and its revocation
are the task-facing trust boundary; the broker is not a public listener and
has no sandbox client-authentication secret. Upstream traffic uses HTTPS to
fixed GitHub origins. This is a narrow reverse proxy, not TLS interception: it
installs no local CA and does not MITM arbitrary client traffic.

Credential injection, request and response header allowlists, streaming
credential scrubbing, and redirect policy checks are part of that boundary.
Redirects are not followed by the broker; only same-origin, policy-allowed
destinations may be translated back into the broker namespace. Responses and
logs must not expose the PAT or its generated authorization encoding.

The broker does not change the publisher. Trusted review, signing, push, and
pull-request creation remain daemon-side under ADR-0011 and ADR-0032. The
observer's task label is spoofable audit context, never authentication or an
authority selector. All consumers of one broker share its read-only repository
allowlist; per-task repository separation requires narrower broker instances
or a separately designed authority boundary, not reliance on that label.

## Consequences

An agent can read the granted repositories while its tunnel is available, but
cannot recover a reusable GitHub credential from the broker. Ending a task
requires revoking its channel, not rotating the PAT merely because the task
ran. Trusted-side credential exposure still requires the usual rotation.
Read access itself can disclose repository content and consume upstream rate
limits; credential non-disclosure does not make an allowed read harmless.

A small standalone process avoids adding forge proxying and its dependencies
to the daemon control plane. Standard-library-only execution permits direct
invocation on a trusted host without a daemon virtual environment and keeps
the broker's dependency surface narrow. Operators must nevertheless start and
supervise the broker and protect the slot-side endpoint. Provisioning,
environment grants, and the dogfood environment's tunnel integration remain
separate responsibilities, not implicit features of the process.

The plain-HTTP listener requires a protected tunnel and a trusted slot host.
Any process that can reach that host's loopback listener can use its allowlist.
Task labels provide human correlation, not authenticated attribution. The
absence of TLS MITM avoids distributing a trusted CA into sandboxes, but means
HTTPS-only client assumptions are not supported. `git` and `curl` cover the
observation contract; general `gh` CLI compatibility does not.

Requests stream through independent `ThreadingHTTPServer` handlers. Slow
clones do not deliberately serialize all reads, but consume threads,
connections, and bandwidth; concurrent slow clones can pressure the host. This
tradeoff is acceptable for read-only QA volume, without a scheduler or a
separate concurrency framework. Revisit it if measured QA traffic requires
bounded concurrency or stronger resource isolation.

Recovery is restart. The broker has no durable request state to reconcile;
clients repeat interrupted reads or fetches. Restart also re-reads the token
file, so trusted-side token rotation is replace the file and restart, with no
sandbox state update or hot-reload protocol. Broker and tunnel availability
become dependencies of observation, and local health does not prove upstream
GitHub authentication.

Revisit the policy if a new observation cannot be expressed as repository REST
reads or upload-pack. Adding write authority is a new architectural decision,
not a broader default allowlist. Any replacement must preserve credential
non-disclosure, explicit repository confinement, and trusted publishing.

## Alternatives considered

### Give tasks GitHub App installation tokens

Installation tokens can have narrower repository scope and a shorter lifetime
than a PAT, and would support standard GitHub clients directly. They were
rejected because they remain exportable bearer credentials: a task can reuse
them outside its assigned channel until expiry. Delegation limits damage but
does not satisfy ADR-0015's non-disclosure invariant.

### Support `gh` through GitHub Enterprise host mode

Enterprise-host mode could preserve familiar `gh` workflows, but its HTTPS
expectation would require a sandbox-trusted local CA or a different TLS-serving
arrangement. A transparent HTTPS interception layer would widen both protocol
support and the trust surface. The loop's observations are covered by `git`
and `curl`, so that compatibility does not justify TLS MITM.

### Extend the daemon's publishing client into an agent proxy

Reusing the trusted publisher could avoid a separate process. It was rejected
because publisher authority and observer reads have different contracts;
combining them enlarges the control plane and couples task-facing traffic to
privileged forge operations. The narrow standalone broker applies ADR-0015
without changing ADR-0011's publisher.
