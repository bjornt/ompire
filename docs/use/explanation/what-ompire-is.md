# What Ompire is

Running a coding agent directly works well for one small task. Ompire exists
because of everything after that. The founding problem was running several
unrelated tasks at once, often across different projects: in a terminal
multiplexer that is a wall of panes with no context — hard to tell which
agent is blocked, waiting, or wasting tokens, or which project a piece of
work even belongs to. Session trackers such as herdr improve the listing,
but they are session-first: a flat list of terminals, not work organized by
project and state.

The second problem is rigor. When time pressure arrives, the careful path —
reproduce, diagnose, fix, validate, review — is the first thing abbreviated.

Ompire makes the rigorous path the fast path, and keeps many tasks across
many projects visible and under control.

## The four parts

Ompire is one product built from four parts. Each earns its place alone;
together they are the workbench.

**Agent isolation.** The agent works in a disposable container on a full
clone of your project — never your main checkout or its `.git` directory.
The container holds no secrets: signing keys, forge credentials, and raw
model-provider credentials all stay on the host, and the agent reaches the
model through a local authentication gateway with a scoped token instead of
the credential behind it. An agent that leaks everything it can see leaks
nothing that matters. See [The trust boundary](trust-boundary.md).

**Parallel task oversight.** The founding motivation. Tasks are grouped by
project, each with its state, its workflow position, and its evidence — not
a flat list of sessions. Attention is derived centrally so the one task that
needs you among the nine that do not can actually reach you. See
[The attention model](attention.md).

**The workflow engine.** Instead of telling one agent "do this workflow" in
markdown, the workflow is declared deterministically: reproduce, diagnose,
fix, validate, review, gate, publish. Mechanical and privileged steps —
signing, pushing, creating pull requests — are performed by the daemon, not
the agent. Humans are asked for structured feedback at declared gates. This
part is expected to change the most as it is used for real work.

**The refinement loop.** After work finishes, a retrospective over the
run's sessions looks at where agents struggled and proposes durable
improvements — new or amended skills, AGENTS.md rules, documentation. This
is direction, not built behavior; it is named because it is what compounds
over time. See [Where it is going](#where-it-is-going).

## The thesis

**The control plane should be deterministic; agents should be workers inside
it.**

Ompire decides what step runs, what evidence is required, what may happen
next, and when you must intervene. The agent does the work that benefits
from judgment: investigation, implementation, review, synthesis. Its output
is treated as untrusted input until a declared check, policy, or human gate
accepts it.

This does not make agent output deterministic. It makes the process around
it reproducible and auditable, which is the part that was missing.

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

It is not an agent terminal in a browser, and it is not a session tracker.
A terminal multiplexer gives you many panes; a session tracker gives you a
list of them. Neither gives you isolation, project-organized work, workflow
state, evidence handoff between steps, a review you can trust, or a
publishing path that keeps your signing key away from the agent.

It is also not a hosted service. Ompire runs on your machine, binds to
localhost, and holds your credentials on the host side of a boundary the
agent never crosses.

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

## Where it is going

[`VISION.md`](../../VISION.md) describes the intended destination — the four
parts in full: versioned declarative workflows, roadmaps of dependent tasks,
credential brokers, provenance from commit back to the reasoning that
produced it, and a refinement loop that turns each run's struggles into
skills, rules, and documentation. Much of it is not built. Read it as
direction, and this documentation as what exists.
