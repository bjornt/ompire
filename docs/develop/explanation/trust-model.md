# Why the control plane is trusted and the agent is not

This page is the contributor-facing version of [The trust
boundary](../../use/explanation/trust-boundary.md). That page tells an
operator what Ompire protects. This one tells you what you must not break.

## The invariant

**Nothing the agent produces may become an irreversible external effect
without passing a check the agent could not influence.**

Every rule below is a consequence of that sentence.

## Why the agent is untrusted

Not because it is malicious. Because it is a probabilistic process consuming
untrusted input — issue text, repository contents, tool output, its own prior
reasoning — any of which can steer it. Prompt injection is the sharp case, but
ordinary confusion produces the same failure with no adversary at all.

So the design question is never "will the agent behave" but "what can it reach
when it does not".

## What this forbids

**Do not give the agent a credential.** Not the signing key, not `gh`
credentials, not the daemon's bearer token. If an agent step needs a
privileged operation, the daemon performs it on the agent's behalf and the
agent receives the result.

**Do not let the agent influence its own verdict.** Review runs host-side,
driven by the daemon. An agent that could run its own reviewer, edit the
review input, or write the verdict makes review ceremonial.

**Do not let agent output route control flow unchecked.** A decision step
routes on validated evidence with explicit rules. An LLM judge is legitimate
only as a *declared* step whose inputs, output, and routing effect are
recorded — never as a hidden fallback when parsing fails. Missing, malformed,
or contradictory output is data: it becomes a gate, not a success.

**Do not widen the sandbox for convenience.** Each task gets its own clone and
container. Sharing a working tree, an index, refs, or a container between
tasks removes the property that makes parallel tasks safe.

**Do not let a temporary rewrite be unrecoverable.** Review and ship both
rewrite Git state temporarily. Both record the original `HEAD` under a durable
ref *before* touching anything, and restore from it at startup if interrupted.
Any new operation that rewrites history needs the same protection.

**Fail closed.** An unknown GPG state blocks shipping. An unclassified session
status maps to `silent`. An unresolved outcome escalates to a human. When the
system does not know, it must not proceed.

## What this permits

The agent is not crippled. It writes code, runs commands, investigates, and
drafts the commit message and pull-request text — real work, and the part
where judgment helps.

The boundary is about authority, not capability.

## Reviewing a change against this

When you touch spawn, agent supervision, review, or ship, ask:

1. Does this give the agent access to a credential, directly or through an
   environment it can read?
2. Does this let agent-produced content reach an external system without a
   check it could not influence?
3. Does this create a path where a crash leaves rewritten Git state
   unrecoverable?
4. Does this add a fallback that silently accepts unparseable agent output?
5. Does this share mutable state between two tasks?

A yes to any of these is a design problem, not a detail.

## The known gaps

One is real and currently unresolved:

**Ambient network access.** Per-workflow network policy is direction, not
behavior. The container has whatever access its environment provides.

It is tracked as a decision requiring reconciliation in `ADR.PLAN.md`. Do not
resolve it incidentally.
