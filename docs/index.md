# Ompire documentation

Ompire is a personal AI engineering workbench built from four parts: agent
isolation, oversight of parallel tasks across projects, a deterministic
workflow engine, and — as direction — a refinement loop. Documentation is
split by audience, because operating Ompire and building Ompire have almost
nothing in common.

## [Use Ompire](use/index.md)

For operators. Installing the daemon, registering a project, spawning tasks,
answering gates, reviewing results, and shipping pull requests.

Start with [Install Ompire and run your first
task](use/tutorials/install-and-first-task.md).

## [Build Ompire](develop/index.md)

For contributors. Repository layout, development environment, the test
suites, the architecture, and the change workflow used to deliver changes.

Start with [Set up a development
environment](develop/tutorials/development-environment.md).

## Durable knowledge

Two collections sit outside both sets and are referenced by each:

- [Architecture decision records](adr/README.md) — why durable architectural choices
  exist, their consequences, and the alternatives rejected.
- [`VISION.md`](VISION.md) — long-term product direction. It describes
  where Ompire is going, not what it currently does.

## How this documentation is organized

Each set uses the four [Diátaxis](https://diataxis.fr) categories:

| Category | Answers | Read it when |
|---|---|---|
| Tutorials | "Take me through this once" | You are new and want a guaranteed path |
| How-to | "How do I do X?" | You have a specific goal |
| Reference | "What exactly is X?" | You need precise detail |
| Explanation | "Why does it work this way?" | You want the mental model |

Current product behavior lives in the reference section of whichever audience
it serves — [operator](use/index.md#reference) or
[contributor](develop/index.md#reference) — and is never restated in the other
set. A page that both audiences need is written once for its primary audience
and linked from the other.

The conventions behind this structure are recorded in
[ADR-0019](adr/0019-split-documentation-by-audience-using-diataxis.md) and
[ADR-0020](adr/0020-author-documentation-as-portable-markdown.md).
