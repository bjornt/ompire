# ADR 0018: Keep built-in workflows in Python until portable versioning is required

- Status: Accepted
- Date: 2026-08-23

## Context

Ompire needs workflow definitions that describe named sessions, ordered steps,
prompt construction, deterministic routing, and human gates. The current step
and outcome contracts are still being learned from a small set of built-in
workflows. Prompt and gate text depend on task and template state, while routes
inspect persisted outcomes and repeated step records. Expressing those
operations directly in the trusted control-plane language keeps the definitions
close to the executor and allows ordinary code review, type checking, and tests
to cover both the engine and its workflows.

The implemented registry therefore contains frozen Python workflow and step
objects keyed by a stable name. Definitions are imported with the daemon,
registered before templates are validated, and structurally checked at startup.
Only daemon-shipped code can define prompts and routes. Templates select a
registered workflow by name, and a task copies that name when it is created.
The task persists run position and step history, but it does not persist a
workflow version or an immutable copy of the definition. Restart recovery uses
the definition currently registered under that name.

That representation is appropriate only inside its present trust and lifecycle
boundary. A Python definition is executable control-plane code, not portable
configuration. A stable name does not prove which definition produced an old
run, allow two versions to coexist, or prevent a daemon upgrade from changing
the meaning of an in-flight run. Startup validation can reject malformed
structure, but it cannot make an arbitrary callable safe or reconstruct code
that is no longer deployed.

The durable product direction calls for versioned declarative workflows whose
exact version is recorded by each task, with custom behavior behind explicit
trusted-plugin or external-tool boundaries. That direction and the current
implementation differ in maturity rather than in the intended trust model. The
small built-in vocabulary and its persistence semantics need operational use
before Ompire freezes them into a portable schema. Introducing a public format
now would either expose Python-equivalent authority through configuration or
prematurely standardize contracts that remain subject to change.

This ADR backfills the implemented boundary. No reliable original acceptance
date was recorded, so it uses the backfill date. It accepts Python definitions
for the current built-in-only phase and states the conditions that end that
phase; it does not reject the versioned declarative direction.

## Decision

While workflows are trusted, built in, and released with the daemon, Ompire
defines them as frozen Python objects in the daemon process and registers each
under a stable name. Workflow prompts, gate messages, and deterministic routes
may be Python callables over the bounded run context. Definitions load before
configuration that references them, and the daemon validates their structural
invariants at startup so an invalid built-in definition prevents service
startup rather than failing a task after execution begins.

Workflow-definition code has the same trust as the control plane. It enters the
registry only through reviewed daemon releases. Operator input, project files,
templates, and agent-produced artifacts must not provide Python source, module
paths, callables, or dynamic imports for the registry. A registered name is a
selector for trusted built-in behavior, not an extension mechanism or a
security boundary.

The current name-only identity is explicitly unversioned. Tasks persist the
selected name and execution records, while restart recovery resolves that name
to the currently deployed definition. Ompire therefore does not claim exact
historical replay or semantic pinning across a definition change. Until the
representation is superseded, a daemon release that changes an existing
workflow is responsible for compatibility with persisted active runs; an
incompatible change requires those runs to be completed, failed explicitly, or
migrated as part of that release. Registering materially different behavior
under the same name and silently re-driving an active run is not an acceptable
migration strategy.

This decision must be superseded before any of the following becomes a
supported requirement:

- operators, projects, or third parties can author or install workflow
  definitions independently of a daemon release;
- more than one revision of a workflow must execute concurrently;
- a task must retain exact workflow semantics across daemon upgrades or support
  reproducible historical replay;
- workflow definitions must be exchanged between installations; or
- custom step behavior must be loaded without becoming trusted daemon code.

The superseding design must give each definition an immutable version identity
and record that identity, or an equivalent immutable snapshot, on every run. It
must use a constrained declarative format for the workflow document and keep
arbitrary behavior behind an explicit trusted-plugin or external-tool boundary.
It must also define migration for templates, active runs, and persisted history
rather than resolving an old name to whichever code happens to be deployed.
The exact schema and plugin mechanism are intentionally not fixed by this ADR.

The invariant during the current phase is that every executable workflow
definition is reviewed control-plane code shipped with the daemon. The
unversioned registry remains a bounded implementation stage, not a public
workflow format; expansion beyond built-in definitions requires immutable
versioning and a non-executable declarative boundary.

## Consequences

Built-in workflows remain compact and auditable alongside the engine that
executes them. They can reuse typed step objects and ordinary functions for
context-sensitive prompts and deterministic routes without adding a parser,
expression language, schema evolution policy, canonical serializer, or plugin
protocol before those contracts are stable. Invalid session and step structure
is detected at startup, and workflow changes receive the same review and test
coverage as other trusted control-plane changes.

The registry is deliberately not an operator customization surface. Adding or
changing a workflow requires a daemon release and restart. Definitions cannot
be shared as data, edited through the UI, or safely loaded from a project.
Python's expressiveness also means structural validation cannot describe all
possible effects of a callable; safety comes from the trusted-code boundary and
review, not from sandboxing the definition.

Name-only persistence limits recovery and audit claims. Step records preserve
what the daemon observed, but they do not identify the exact source definition
that interpreted those observations. A changed definition may remove the
persisted current step, alter fall-through order, build a different prompt, or
route the same outcome differently. Maintainers must treat changes to an
existing workflow as compatibility-sensitive whenever active runs can survive
an upgrade. This operational constraint is acceptable while workflows are few,
built in, and deployed together with the engine; it becomes unacceptable when
independent authoring, concurrent revisions, or exact replay is required.

The future migration carries real work rather than being a file-format swap. It
must assign immutable identities, retain or reconstruct executable semantics,
validate documents without executing them, constrain expressions and
extensions, preserve active-run recovery, migrate template references, and
explain how old history is interpreted. Existing name-only runs may need to be
completed under retained code, mapped through an explicit migration, or marked
as lacking an exact historical definition. A source hash alone cannot supply
missing code or dependencies.

The future declarative format may still compile into the current Python engine
and retain the task, step, named-session, persistence, and daemon-owned routing
model. Changing representation does not by itself supersede the separate
decision that tasks execute workflows over named sessions.

This decision should be revisited before the first externally supplied
workflow, not after such input has become executable. It should also be
revisited earlier if routine daemon upgrades cannot preserve active runs, if
audit requirements demand exact definition provenance, or if maintaining
compatibility under stable names becomes more costly than versioning.

## Alternatives considered

### Introduce a versioned declarative format immediately

A declarative document with an immutable version would improve portability,
validation, auditability, coexistence, and recovery. It matches the durable
product direction and is the required destination once the stated triggers
occur. It was deferred for the built-in-only phase because the step vocabulary,
outcome contracts, routing needs, retries, authority declarations, and plugin
boundary are still evolving. Freezing them now would either create a narrow
format that immediately needs escape hatches or embed a general expression
language whose authority is harder to reason about than reviewed Python.

### Allow operator-authored Python workflows or unrestricted plugins

Loading Python modules would provide immediate customization and reuse the
current definition API without designing a declarative schema. It was rejected
because workflow code runs in the trusted control plane, constructs agent
instructions, chooses routes, and can influence commands and gates. Treating a
configuration directory or project checkout as trusted executable daemon code
would let workflow customization bypass the agent sandbox and the reviewed
release boundary. Future customization must separate declarative data from
explicitly installed and governed trusted extensions.

### Version the current Python definitions by name or source hash

Adding a version field or source hash would make drift detectable and could let
templates request a particular revision. It was not chosen as the current
architecture because identity alone does not preserve the referenced Python
code, its imported dependencies, or its execution environment, and retaining
multiple historical modules would create an implicit plugin and migration
system. This can be a migration aid, but it does not satisfy portable,
non-executable workflow authoring or exact recovery by itself. The superseding
design may use hashes as immutable identifiers if it also defines how the
identified semantics remain available and safe to execute.
