# Ompire Vision

## Purpose

Ompire is a local-first control plane for coding-agent work. It lets one
operator run more work, with more rigor, by placing probabilistic coding agents
inside secure, observable, and mostly deterministic workflows.

Ompire is not merely a way to put an agent terminal in a browser. It owns the
lifecycle around an agent session: preparing an isolated workspace, executing a
workflow, carrying evidence and artifacts between steps, requesting human input
when needed, reviewing the result, and performing privileged Git and forge
operations through trusted integrations.

The intended result is trustworthy leverage: an operator can delegate more work
without losing control of credentials, source repositories, engineering
standards, or authorship.

## The problem

Running coding agents directly works well for one small task, but the manual
coordination cost grows quickly:

- Every task repeats setup, prompting, repository, branch, and environment work.
- Multiple sessions are hard to monitor. It is easy to miss an agent that is
  blocked, waiting for input, stalled, or wasting tokens.
- Agents are good at open-ended work but should not be trusted to perform every
  control-plane action or decide whether their own work is acceptable.
- Rigorous workflows such as reproduce → diagnose → fix → validate → review are
  easy to abbreviate under time pressure.
- Review, commit, push, pull-request creation, and cleanup require repetitive
  human glue work and access to sensitive credentials.
- Large roadmaps need dependency, branch, review, and rebase coordination that a
  collection of independent terminals does not provide.

Ompire should make the rigorous path the fast path.

## Product thesis

The control plane should be deterministic; agents should be workers inside it.

Ompire decides what step runs, what evidence is required, which authority the
step has, what may happen next, and when a human must intervene. Agents perform
the work that benefits from judgment: investigation, design, implementation,
review, and synthesis. Agent output is treated as untrusted input until a
declared check, policy, or human gate accepts it.

This separation makes agent work reproducible and auditable without pretending
that model output itself is deterministic.

## Product principles

### 1. Secure by construction

Agents run in disposable, task-specific isolation. They must not receive raw
secrets or have access to the operator's main checkout, GitHub credentials, SSH
keys, signing keys, daemon token, or LLM-provider token.

An agent may request a capability without receiving the credential behind it.
For example, model access, forge operations, and other privileged services are
provided by narrow, audited brokers. Network access is an explicit workflow
policy rather than an accidental property of the host environment.

### 2. Deterministic orchestration, explicit uncertainty

Anything that can be implemented as validated state transitions, commands,
policies, or checks should not be delegated to an agent. Creating a pull
request, choosing the next declared step from an exit code, enforcing an
iteration limit, rebasing dependent branches, and deciding whether a gate is
satisfied are control-plane responsibilities.

An agent or LLM judge may be a declared workflow step where semantic judgment
is genuinely useful. It must never be a hidden fallback. Its inputs, output,
confidence, and effect on routing are recorded. Invalid or uncertain output
stops at a human gate.

### 3. Human attention is the scarce resource

Ompire should remain quiet while work is progressing safely and become
unmissable when the operator is required. The UI must answer:

- What is running?
- What needs me now, and why?
- What evidence has been produced?
- What will happen next?
- What authority will the next action use?
- How much time, context, and money has been spent?

### 4. Authority and attribution are explicit

Supervision level and execution identity are separate policies. A workflow may
run unattended while using only a bot identity, or pause frequently while still
using bot-authored publishing actions.

Every privileged action records who or what authorized it, which identity was
used, and which task and workflow step caused it. Agent-produced changes must
remain identifiable as such in Ompire's audit history and in the pull-request
conversation.

Traceability must survive Git rewriting. Ompire records the lineage from agent
checkpoint commits through trusted publishing, rebases or force-pushes, and the
commit or commits that finally land on the mainline branch. Starting from any
known workspace, published, or landed commit, the operator must be able to find
the task, workflow version and run, producing steps, session logs, artifacts,
validation and review evidence, human decisions, and identities involved.
This history remains available after the task workspace is cleaned up.

### 5. Work is durable and resumable

Closing the browser or restarting the daemon must not erase the meaning of a
run. Workflow definitions, inputs, state transitions, outcomes, artifacts,
human decisions, external side effects, and agent-session identities are
durable enough to resume safely and explain what happened later.

Recovery must not duplicate non-idempotent actions such as commits, pushes,
comments, or pull-request creation.

### 6. Isolation enables parallelism

Each task works in its own clone and sandbox. Independent tasks and roadmaps may
run against the same project without sharing a working tree, index, refs, or
container. Coordination happens through explicit dependencies and trusted Git
operations, not shared mutable directories.

### 7. Prefer existing tools and open boundaries

Ompire should orchestrate proven tools rather than reimplement them: Git and the
forge CLI for source-control operations, llmvet or equivalent tools for review,
workshop for isolation, and design-handoff tools for exploratory work. Plan
changes with repository-local Markdown artifacts and skills instead of depending
on a specification CLI.

Integrations should sit behind small, versioned contracts. Capabilities that
become useful outside Ompire should be able to move into standalone tools
without changing the product model.

## Core model

### Project

A registered source repository and its trusted integration policy: source
location, upstream and optional fork, default branch, sandbox configuration,
allowed workflows, identity policy, network/capability policy, work-item
providers, saved queries, and workflow-suggestion rules.

### Work item

A provider-neutral reference to upstream intent, such as a GitHub issue or Jira
ticket. Its stable identity includes the provider instance and ticket key, not
only a mutable URL. Ompire can discover work items through project-configured
saved queries and normalize the fields workflows need while preserving a link to
the provider's full representation.

The ticketing system remains the source of truth for the current ticket. Ompire
stores immutable, revisioned intake snapshots so a run can always show exactly
which title, description, metadata, and comments it received. Later ticket
changes are recorded separately and never rewrite the historical input.

A work item may start either one task or one linear roadmap. Ompire prevents
accidental duplicate active work for the same item while allowing an explicit,
audited override.

### Task

One deliverable against a project, such as fixing a bug, proposing a change,
performing an investigation, or implementing one roadmap item. A task owns an
isolated workspace, a branch when needed, one or more durable workflow runs,
its linked work items and intake snapshots, sessions, artifacts, review history,
and publishing state.

Tasks need not produce code or a commit.

### Roadmap

An ordered train of dependent tasks against one project. Each task is an
independently reviewable change, but later tasks may begin before earlier pull
requests are reviewed or merged.

The first task is published first. Downstream tasks continue in isolated
workspaces based on their predecessor. When an earlier task changes, all
affected downstream branches are rebased or rebuilt in order and their relevant
validation is rerun. After a pull request lands, Ompire publishes the next one.
Conflicts or invalidated evidence stop at a human gate rather than being hidden.

The initial roadmap model is linear. Separate roadmaps and standalone tasks may
run in parallel against the same project.

### Workflow

A versioned, declarative definition of sessions, steps, transitions, inputs,
outputs, policies, budgets, retry rules, and gates. A task records the exact
workflow version it instantiated so later edits cannot change a run in place.

The built-in step vocabulary should remain small and composable. It includes at
least:

- **agent** — perform a bounded turn in a named coding-agent session;
- **command** — run a deterministic command in a declared environment;
- **decision** — route from validated evidence using explicit rules;
- **gate** — wait for a human decision or acknowledgement;
- **review** — collect a structured verdict and comments;
- **publish** — perform trusted commit, push, pull-request, or comment actions;
- **integration** — read or update an external system through a trusted,
  provider-aware broker;
- **export** — return approved artifacts from isolation.

Custom behavior belongs behind an explicit trusted-plugin or external-tool
boundary, not as arbitrary code embedded in a workflow document.

### Run and step

A run is one durable execution of a workflow for a task. Every step has a clear
state, declared authority, timeout, inputs, outputs, and terminal result. Steps
may reuse a named session when conversational context is valuable or use a new
session when independence is more important.

Agent steps exchange structured outcomes and artifacts rather than relying on a
transcript heuristic. Missing, malformed, or contradictory outcomes are data;
they do not silently become success.

Retries and loops are explicit and bounded. An LLM judge may decide whether an
automated review loop should stop early or continue within its declared bound.
The hard bound remains deterministic, and exhaustion or uncertainty leads to a
human gate.

### Session

A named interaction with a coding agent. Sessions expose streaming text,
thinking when available, tool calls and results, questions, approvals,
subagents, todos, model and context state, token usage, and cost. They are
resources used by a workflow, not the top-level unit of work.

### Artifact

A versioned output with provenance: producing run and step, content or path,
media type, checksum, and intended consumers. Artifacts include reproduction
scripts, root-cause analyses, plans, OpenSpec changes, design handoffs, review
reports, test evidence, patches, and release or pull-request text.

Artifacts may pass between steps without entering Git. Export to the operator's
checkout is a trusted host operation: only allowlisted paths are eligible, the
diff and destination are previewed, path traversal and symlink escapes are
rejected, and conflicts require a human decision. Exploratory workflows may
export artifacts without creating a commit or branch.

### Provenance and commit lineage

Every code-producing run creates a durable provenance record. Commit lineage is
many-to-many: several agent checkpoints may become one published squash commit,
a published commit may be rewritten during rebase, and a pull request may land
as a merge commit, a rebased range, or a new squash commit.

The provenance record links at least:

- the project, task, roadmap item, workflow definition and version, run, and
  producing steps;
- originating work-item identities, exact intake revisions, and any later ticket
  updates explicitly incorporated into the run;
- the input request, structured outcomes, artifacts, validation results, review
  iterations, gates, and human decisions;
- every contributing agent session and its archived transcript and tool-event
  log;
- workspace checkpoint commits, branch bases, published commits and head
  revisions, pull-request identity, merge strategy, and final mainline commit or
  commit range;
- author, committer, signer, pusher, bot or human identity delegation, and the
  authority that permitted each privileged transition;
- content fingerprints such as tree and patch identities needed to correlate
  commits whose SHA changed.

After a pull request lands, Ompire reconciles it with the repository and records
the actual mainline identity; it must not assume that the last PR head SHA is the
landed SHA. Squash, rebase, merge, amended commits, and downstream-roadmap
rebases preserve explicit lineage rather than overwriting the earlier identity.

The provenance index supports lookup by project and commit SHA, pull request,
task, or run. The primary experience is commit → lineage → workflow execution →
step evidence → session and tool logs. Missing or ambiguous correlation is
reported explicitly and never filled in by a guess.

Provenance metadata and the logs needed to explain it outlive sandbox cleanup.
They are local sensitive data, protected accordingly, and removed only by an
explicit retention or purge policy.

## Workflow behavior

### A rigorous bug-fix workflow

The standard bug-fix workflow should make the following stages first-class:

1. Capture the reported behavior and acceptance criteria.
2. Reproduce the bug, preferably with an executable failing check. If that is
   impossible, record repeatable evidence and why automation is not suitable.
3. Produce a root-cause analysis that distinguishes evidence from inference.
4. Plan the smallest source-level fix.
5. Implement the fix in an isolated branch.
6. Validate the original reproduction and relevant regression checks.
7. Run an independent agent-review loop, with a fixed maximum and an optional
   declared judge to stop early or request another pass.
8. Run a human review loop until accepted or explicitly abandoned.
9. Use the trusted publisher to commit, push, and create the pull request.
10. Monitor the pull request and handle review comments through a new,
    traceable remediation cycle.

No stage may claim evidence it did not produce. A failed reproduction, missing
prerequisite, exhausted review loop, or inconclusive judge becomes an explicit
gate with the available evidence attached.

### A planned-change workflow

A roadmap item or substantial feature should use the lightweight skills-based
change workflow:

1. Create or refine `changes/<name>/SPEC.md` and `PLAN.md`.
2. Have an independent agent review those artifacts against the task brief,
   this vision, applicable ADRs, and current feature documentation. Address
   findings in a bounded loop.
3. Implement the accepted plan.
4. Run the declared validation and an independent agent-review loop.
5. Stop for human review and address findings until accepted or abandoned.
6. Use the trusted publisher to create the commit and pull request.

After completion, reconcile current behavior into feature documentation and
durable rationale into ADRs, then delete the temporary change directory. Git
and the associated commit or pull request retain the delivery history.

Later roadmap items may start before this human review or pull-request merge.
Ompire records the exact predecessor revision and assumptions they started from
so an upstream change can deterministically invalidate, rebase, and revalidate
the affected downstream work.

### Ticket-driven work

A project may configure one or more work-item providers and named saved queries,
such as GitHub issue searches or Jira JQL. These views form an inbox of available
work inside Ompire without trying to replace the provider's full interface.

Selecting a work item shows its normalized metadata and provider link. Project
rules use provider, item type, labels, component, or other metadata to suggest a
workflow and whether to create a task or roadmap. The operator confirms or
changes that suggestion before work starts. Existing active work for the item is
shown and blocks an accidental duplicate unless the operator explicitly
overrides it.

Starting work records an immutable snapshot of the exact ticket revision and
comments supplied to the workflow. Ompire watches the linked item for later
edits, comments, assignment, and status changes. New information raises an
attention event with the change visible; it does not silently alter an in-flight
agent's instructions. A human or declared workflow step may incorporate the
update, producing a new revisioned snapshot and an auditable decision.

Declared integration steps may assign or claim work, transition provider status,
link the pull request, and post progress or completion comments using the
configured bot or service identity. Project configuration maps workflow states
to each provider's lifecycle. Every external write is scoped, idempotent,
audited, and visible as a workflow side effect; a failed synchronization is
surfaced rather than treated as workflow success.

Traceability is bidirectional: a work item links to all tasks, roadmaps, runs,
pull requests, and landed commits that addressed it, while commit provenance
links back to the exact ticket revision that initiated or later changed the
work.

### Exploratory work

Exploration is a valid terminal outcome. A workflow may investigate, compare
approaches, or create an OpenSpec/design handoff and then finish with no source
commit. The useful result is the artifact and its evidence, not a fabricated
code change.

### Supervised and unattended execution

Supervision is a per-run policy, and may be tightened for an individual stage:

- **Supervised:** declared gates pause for the operator; privileged transitions
  normally require an explicit action.
- **Unattended:** the run proceeds within pre-authorized capabilities, budgets,
  identities, and loop bounds. It still stops for undeclared authority,
  uncertainty, policy violations, or exhausted bounds.

Unattended does not mean unlimited autonomy. It means that all permitted
autonomy was declared before the run.

## Identity and forge policy

The default automation identity is a dedicated bot account. Automated commits,
pushes, pull requests, and GitHub comments should use that identity unless a
narrow project policy says otherwise.

The same rule applies to ticketing systems. GitHub issue activity uses the bot
account, and Jira activity uses a dedicated automation/service identity. Status
transitions and comments are privileged integration actions, not operations the
agent performs directly.

One deliberate exception is remediation of comments on an existing pull
request. When a trusted human reviewer has requested changes and project policy
permits it, Ompire may use the human operator's identity to commit and push the
fix to that existing branch. The trusted control plane performs those actions;
the agent never receives the human credentials. Ompire's bot account posts the
GitHub reply describing the automated fix and linking it to the resulting
commits, so the reviewer can see that an agent performed the work.

This delegation is scoped to the existing project, pull request, and branch; it
does not grant the agent general human GitHub authority. Ticket descriptions,
issue comments, reviewer identities, and review comments are untrusted external
input unless allowed by project policy. They can request work but cannot expand
the workflow's permissions or identity.

Signing and credential use should rely on host-side credential agents or narrow
brokers. Secrets are never copied into a task workspace, environment, prompt,
transcript, artifact, or workflow state.

## Security model

The coding agent, its tools, dependency scripts, repository contents, generated
files, ticket descriptions and comments, and review comments are all untrusted.

The sandbox boundary must provide:

- a disposable clone with no writable or reachable path to the main checkout;
- no mounted host credentials, credential sockets, daemon control socket, or
  unrelated task data;
- task-scoped filesystem, process, network, and resource boundaries;
- an explicit egress policy and scoped proxies for privileged services;
- no raw LLM-provider credentials, even though model calls are available;
- no direct push, signing, pull-request, or comment authority;
- safe teardown that cannot delete outside Ompire-owned task roots.

The trusted control plane must:

- validate paths, refs, URLs, workflow definitions, and all broker requests;
- enforce least privilege, budgets, timeouts, loop bounds, and identity policy;
- keep privileged Git and forge actions deterministic and idempotent;
- redact secrets from logs and reject attempts to persist them as artifacts;
- audit human decisions and every privileged external side effect;
- render untrusted agent output without treating it as executable UI content;
- fail closed when authority or outcome is ambiguous.

The operator's main checkout remains untouched during ordinary task execution.
Branch synchronization or artifact export back to it is an explicit trusted
operation with a preview and conflict handling; it is never performed by the
agent.

## Interaction and user experience

The web UI is a cockpit for the agent system, not a terminal emulator.

At the fleet level it shows tasks and roadmaps grouped by whether they need
attention, are running, are waiting, have failed, or have shipped. At task level
it shows the workflow graph or train, current step, evidence, artifacts, and one
rich transcript per named session.

The operator can:

- browse project-configured issue queues, inspect tickets, see existing linked
  work, and start a suggested task or roadmap after confirming its workflow;
- answer structured questions and approval requests;
- steer a running turn, queue a follow-up, interrupt, or stop a session;
- inspect tool inputs and outputs, subagent activity, todos, context use,
  tokens, cost, elapsed time, retries, and budgets;
- see the exact reason for every state and routing decision;
- compare produced evidence with the task's acceptance criteria;
- open review tools and respond to workflow gates;
- preview privileged Git, forge, identity, and artifact-export actions;
- take over through the coding agent's native terminal/session escape hatch;
- look up a source, published, or landed commit and follow its lineage back to
  the workflow steps, evidence, and session/tool logs that produced it.

The UI should preserve the agent's native semantics instead of scraping a TTY,
while keeping the underlying session available for direct expert use.

## Reliability and operations

Ompire is a long-lived local service and the source of truth for orchestration
state. The browser is a reconnectable presentation layer.

The system should:

- persist enough state to resume sessions and workflow runs after a restart;
- reconcile its records with workspaces, processes, branches, pull requests,
  and linked work items;
- distinguish retryable infrastructure failures, work failures, cancellations,
  policy blocks, and human gates;
- use idempotency keys or observed external state for non-repeatable actions;
- support cancellation, manual intervention, and safe continuation from a
  selected step without falsifying prior history;
- retain provenance, commit lineage, session logs, and an inspectable run
  history after sandbox cleanup, subject only to explicit retention or purge;
- make time, token, cost, network, and iteration budgets enforceable;
- surface degraded integrations rather than silently skipping them.

## Extensibility

Oh My Pi is the first-class coding agent. Ompire should use its structured RPC
surface deeply, including state, streaming events, questions, steering,
subagents, session recovery, and statistics. The internal session boundary
should nevertheless be small enough to admit another agent protocol later
without reducing Ompire to a lowest-common-denominator terminal wrapper.

Workflows should compose external tools through explicit inputs, outputs, exit
semantics, and capability declarations. OpenSpec operations, automated review,
design handoffs, test runners, and forge actions should be replaceable
integrations rather than special behavior hidden inside agents.

Work-item providers share a small capability-aware interface for saved-query
execution, revisioned reads, change observation, comments, links, assignment,
and status transitions. GitHub Issues and Jira are initial adapters, not separate
product models; provider-specific fields and lifecycle constraints remain
available when a workflow needs them.

## Scope and non-goals

The initial product is for one operator on one local machine. It is not a
multi-user hosted service, a general CI/CD platform, a full ticketing client, or
a replacement for the coding agent, Git, GitHub, Jira, OpenSpec, or review tools.

It does not attempt to make model output deterministic. It makes the surrounding
process explicit, bounded, inspectable, and recoverable.

It does not require every task to end in a commit or pull request, and it does
not merge code autonomously merely because an agent says it is ready.

## Measures of success

Ompire is succeeding when:

- an operator can run many independent tasks and linear roadmaps without
  workspace collisions or losing track of required attention;
- configured GitHub and Jira queues can start a linked task or roadmap with an
  exact intake snapshot, lifecycle updates remain synchronized through audited
  bot actions, and traceability connects the ticket to its landed commits;
- a ten-change roadmap can continue producing downstream work while earlier
  changes are reviewed, then rebase and publish each change in order;
- the same bug-fix workflow reliably produces reproduction, diagnosis,
  implementation, validation, agent review, human review, and publishing
  evidence;
- exploratory work can return useful, safely exported artifacts without a fake
  code deliverable;
- daemon, browser, or agent restarts do not lose the run or duplicate privileged
  actions;
- every privileged action and routing decision can be explained after the fact;
- any Ompire-produced commit found on the mainline branch can be traced through
  rewrites to its task, exact workflow run, evidence, identities, and producing
  agent-session logs;
- no coding agent receives a raw secret or gains write access to the operator's
  main repository;
- human time is spent on judgment and exceptions rather than setup, polling,
  rebasing, or mechanical GitHub operations.

## Relationship to project documentation

This document is the product north star. Current feature documentation
describes the executable contract, and architecture decision records preserve
the rationale for durable choices. Temporary `changes/<name>/SPEC.md` and
`PLAN.md` artifacts state an intended delta and its implementation work; each
change must explain how it advances this vision and how the system remains
usable and secure during the transition. On completion, its durable knowledge
is reconciled into current documentation and ADRs before the temporary
artifacts are deleted. Legacy OpenSpec specifications and archived changes are
migration evidence, not the destination for new work.
