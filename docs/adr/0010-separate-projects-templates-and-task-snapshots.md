# ADR 0010: Separate projects, templates, and task snapshots

- Status: Proposed
- Date: 2026-08-22

## Context

Ompire needs three kinds of configuration with different identities and lifetimes. Repository identity and publishing topology are durable facts about a project. Reusable spawn policy changes as an operator adjusts how new work should run. A task, once accepted, needs stable execution and historical facts even if the reusable policy is later edited or removed. Combining these concerns would either duplicate repository routing across spawn configurations or let mutable configuration silently change work already in progress.

A project is the authority for the checkout and for upstream-versus-fork routing. Several ways of working against that project may share those facts while differing in base branch, branch naming, workflow, Workshop additions, model and thinking defaults, or prompt preamble. Those reusable choices form a template. Keeping them outside the project avoids treating one project's current defaults as its only supported workflow and avoids copying authority-bearing remote information into every template.

Templates need operator-facing create, update, and delete operations. They also participate in the daemon's authoritative state snapshots, transactional reference guards, and schema migrations. A daemon-rewritten configuration file would introduce a second mutation and synchronization mechanism, risk losing operator comments or producing invalid startup configuration, and provide weaker concurrency and migration behavior than the existing registry.

Tasks create a different consistency problem. A template is intentionally mutable, but a task must remain attributable, recoverable, reviewable, and publishable according to the choices under which it was accepted. Retaining only a live template reference makes later edits retroactive. Retaining no source identity makes history impossible to explain. The task therefore needs both source attribution and an execution snapshot.

The entity split and registry-backed templates are implemented. Task records currently retain the project, template, workflow, and rendered branch identities, and the initial spawn pipeline resolves a template into memory. However, not every effective template or task-override value needed after that point is durable. Recovery and later review or publishing operations can consult the current template again. This conflicts with the documented rule that template edits affect only future spawns and can make an existing task observe a different base branch or agent configuration after an edit or restart.

This ADR is therefore a proposed backfill. Its date is the record's creation date, not an invented historical acceptance date. It can become Accepted when task execution, recovery, review, and publishing consume one durable task snapshot rather than mutable template state.

## Decision

Ompire separates project identity, reusable spawn policy, and task execution facts into three registry concepts with distinct ownership:

- A **project** owns repository identity and authority-bearing routing: its operator-facing identity, main checkout, upstream repository, and optional fork. Push and pull-request destinations derive from the project, never from a template or agent-supplied task data.
- A **template** owns reusable policy for creating tasks against one referenced project. This includes the base branch, branch naming rule, workflow selection, Workshop additions source, agent model and thinking defaults, and prompt preamble. Templates are mutable registry entities because operators manage them through the daemon and connected clients observe them through the authoritative state stream.
- A **task snapshot** owns the effective, immutable facts for one accepted task. Task creation resolves the selected template and project as one consistency boundary, applies any allowed task-level overrides, derives values such as the branch name, and durably records every value that later workspace construction, workflow execution, recovery, review, publishing, or historical explanation can require.

The task also records the source project, template, and workflow names for attribution. Those names do not authorize later code to reinterpret the task through the current contents of a mutable template. Template updates affect only tasks accepted after the update. A daemon restart, template edit, or workflow continuation must not change an existing task's effective project routing, base branch, branch name, workflow identity, Workshop selection, prompt preamble, model, or thinking level.

Project and template mutations must preserve their references deliberately rather than cascade silently. A project cannot be removed or renamed while templates or task history depend on its identity. A live task protects its source template from removal; after archival, the template name may remain as historical annotation without retaining a live template row. Updating a template requires no task migration because existing task snapshots are immutable.

Templates live in the transactional daemon registry, not in the operator's static configuration file. Registry schema constraints and daemon validation enforce project references and the allowed structure of spawn policy. Template changes use the same command, event, and authoritative-snapshot boundary as other mutable control-plane entities.

The invariant is that projects answer **where trusted repository operations act**, templates answer **how future tasks should start**, and task snapshots answer **what this task was created to do**. No later stage may recover authority-bearing or execution-significant facts for an existing task by re-reading mutable template configuration.

## Consequences

Repository and publishing facts have one authority. Multiple templates can safely reuse a project without duplicating checkout paths or upstream and fork URLs, while one project can support different branches, workflows, sandbox additions, agent defaults, and preambles. Moving a project or changing its publishing topology is a project operation rather than a coordinated edit across templates.

Templates become ordinary mutable control-plane data. Transactional writes, schema migrations, reference checks, and snapshot-driven clients provide one synchronization model. The cost is that template schema changes require reviewed database migrations and compatibility handling instead of a simple text edit. Infrastructure settings that are not operator-managed spawn policy remain outside this registry boundary.

A durable task snapshot makes execution and history reproducible across daemon restarts and template edits. Recovery does not depend on the continued existence or current contents of a template, and review or publishing cannot silently switch base branches or repository routing midway through a task. Spawn-time overrides become explainable task facts rather than transient request data. This also makes template update semantics simple: an edit changes future tasks only.

The accepted cost is deliberate denormalization. Project, template, workflow, and effective execution values appear on the task even though some originated elsewhere. Task creation must resolve and validate them atomically enough that the snapshot cannot combine incompatible versions. Every newly introduced template field that affects later task behavior must be classified explicitly: snapshot it on the task, or prove that it is presentation-only. Reading through a template reference later because it is convenient violates the boundary.

Reference guards favor safety over cascading convenience. Operators must repoint or remove templates before renaming or deleting a project, and must archive live tasks before deleting their source template. Historical task annotations can outlive templates, but authority-bearing project identity remains protected while retained task history refers to it.

Template and project input remains trusted control-plane configuration, not agent authority. Repository URLs, branch rules, workflow names, Workshop sources, model settings, and preambles must be validated before entering a task snapshot. A prompt preamble may influence an untrusted agent but cannot grant host credentials or publishing authority. Secrets must not be stored in templates or copied into task snapshots merely to make execution self-contained.

The current partial implementation requires migration before acceptance. Effective values that are currently held only in memory or re-read from templates must become durable task facts, and all recovery, review, and publishing paths must consume those facts. Existing tasks that lack a complete snapshot need an explicit compatibility policy that does not pretend current template contents are historical truth; safe choices include preserving a clearly marked legacy fallback or refusing operations whose original authority-bearing inputs cannot be established.

This decision should be revisited if templates become immutable and versioned artifacts whose exact version is permanently retained, if projects no longer own checkout and publishing topology, or if an external configuration service can provide transactional, immutable task-resolution records. Any replacement must preserve stable task behavior, historical attribution, and a single trusted source for repository routing.

## Alternatives considered

### Keep spawn defaults on each project

Per-project defaults require fewer entities and matched the original single-template-per-project behavior. They were rejected because a project is repository identity, not one way of working. A project may need several workflows, branch policies, agent defaults, or preambles, and folding those into the project either prevents that reuse or grows an unstructured collection of optional defaults around authority-bearing repository data.

### Embed checkout and remote details in every template

Self-contained templates make task resolution direct and allow a template to point anywhere without another lookup. They were rejected because checkout paths and upstream-versus-fork routing are trusted project facts. Duplicating them across templates creates drift, makes repository moves and credential review harder, and permits two templates claiming the same project identity to publish to different destinations.

### Store templates in the operator configuration file

Text configuration is portable, familiar, and easy to inspect manually. It was rejected for mutable template data because UI-driven edits would require the daemon to parse and rewrite an operator-owned file while preserving comments and formatting, coordinate file and in-memory state, implement its own concurrency guards, and migrate evolving records. The registry already provides transactions, constraints, migrations, and authoritative snapshot synchronization. Static infrastructure settings remain appropriate for operator configuration.

### Resolve the current template whenever a task needs a value

A live reference minimizes denormalized columns and makes template corrections immediately visible to existing tasks. It was rejected because the same mutability also changes work after acceptance. A restart, delayed review, or publishing action could use a different base branch, workflow setting, model, preamble, or repository association than the original spawn. This weakens recovery and audit history and makes safe template deletion difficult. Source names remain useful for attribution, but execution consumes the task snapshot.
