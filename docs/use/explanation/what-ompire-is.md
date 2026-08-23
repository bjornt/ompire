# What Ompire is

Running a coding agent directly works well for one small task. The cost shows
up when there are several: every task repeats the same setup, sessions are
hard to monitor, it is easy to miss an agent that is blocked or wasting
tokens, and the rigorous path — reproduce, diagnose, fix, validate, review —
is the first thing abbreviated under time pressure.

Ompire exists to make the rigorous path the fast path.

## The thesis

**The control plane should be deterministic; agents should be workers inside
it.**

Ompire decides what step runs, what evidence is required, what may happen
next, and when you must intervene. The agent does the work that benefits from
judgment: investigation, implementation, review, synthesis. Its output is
treated as untrusted input until a declared check, policy, or human gate
accepts it.

This does not make agent output deterministic. It makes the process around it
reproducible and auditable, which is the part that was missing.

## What that means concretely

Anything expressible as a validated state transition is not delegated to an
agent. Creating a pull request, choosing the next step from an exit code,
enforcing an iteration limit, deciding whether a gate is satisfied — these are
control-plane responsibilities, and they behave the same way every time.

An agent or an LLM judge may be a *declared* step where semantic judgment is
genuinely useful. It is never a hidden fallback. Its inputs, output, and
effect on routing are recorded, and uncertain output stops at a human gate
rather than being resolved by a guess.

## What Ompire is not

It is not an agent terminal in a browser. A terminal multiplexer gives you
many sessions; it does not give you isolation, workflow state, evidence
handoff between steps, a review you can trust, or a publishing path that keeps
your signing key away from the agent.

It is also not a hosted service. Ompire runs on your machine, binds to
localhost, and holds your credentials on the host side of a boundary the agent
never crosses.

## The pieces

**Project** — a registered repository and its publishing routing.

**Task** — one deliverable. It owns an isolated clone, a branch, a container,
one or more workflow runs, its sessions, and its publishing state. A task need
not produce code.

**Workflow** — the declared sequence of steps a task executes. Steps are
agent turns, deterministic commands, routing decisions, and human gates.

**Session** — a named interaction with a coding agent. Sessions are resources
a workflow uses, not the top-level unit of work. This is the inversion that
distinguishes Ompire from a session manager: you manage tasks, and sessions
are an implementation detail of how a task gets done.

**Run and step** — one durable execution. Every step has a state, declared
authority, timeout, inputs, outputs, and a terminal result.

## Three properties worth understanding

**Isolation enables parallelism.** Every task gets its own clone and its own
container. Independent tasks against the same project share no working tree,
index, refs, or container, so they cannot interfere with each other or with
your checkout.

**Human attention is the scarce resource.** Ompire is designed to stay quiet
while work is progressing safely and become unmissable when it needs you. See
[The attention model](attention.md).

**Authority is explicit.** Every privileged action records what authorized it
and which identity performed it. See [The trust boundary](trust-boundary.md).

## Where it is going

[`VISION.md`](../../../VISION.md) describes the intended destination —
versioned declarative workflows, roadmaps of dependent tasks, credential
brokers, provenance from commit back to the reasoning that produced it. Much
of it is not built. Read it as direction, and this documentation as what
exists.
