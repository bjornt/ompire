# ADR 0025: Store global model profiles separately from launch policy

- Status: Accepted
- Date: 2026-09-05

## Context

Model settings in Ompire are per-template. A template carries a `model` and a
`thinking` level alongside its branch pattern, workflow, and preamble
([ADR-0010](0010-separate-projects-templates-and-task-snapshots.md)), so an
operator who wants the same model choices in five projects re-enters them five
times, and changing a model means editing every template that used it.

Two further pressures make that shape wrong going forward. First, omp does not
run one model: it selects among named roles — an active model plus `smol`,
`slow`, and `plan` auxiliaries — each of which takes its own thinking level.
A single `model`/`thinking` pair cannot express that. Second, the same set of
role bindings is the natural unit of reuse across projects, while the branch
pattern and workflow that sit beside it in a template are not.

The obvious places to put this all fail for concrete reasons. Daemon settings
are deliberately a flat map of scalars layered over operator TOML
([ADR-0013](0013-layer-daemon-writable-settings-over-operator-configuration.md));
a set of named entities with four structured bindings each is not a scalar
setting, and putting it there would stretch that mechanism into a general
object store. Operator `config.toml` is owned by the operator and edited by
hand — profiles are UI-managed data. And omp's own user configuration is the
agent's, not the control plane's; reading or writing it would make Ompire's
state depend on a file another program owns.

Naming models also invites naming more. A "profile" could plausibly grow to
mean provider credentials, an omp auth session, or a whole launch preset with
a repository and a workflow attached. Each of those would bind the entity to
something other than model policy and make it unshareable across projects.

## Decision

Global model profiles are a first-class registry entity, stored in the
daemon's own SQLite database as a `model_profiles` table and managed through
`/api/model-profiles` like projects and templates.

A profile is a name plus exactly four role bindings — `default`, `smol`,
`slow`, `plan` — and nothing else. Each binding pairs a provider-qualified
model identifier with an explicit thinking level. Neither field may be null:
there is no "inherit from the host" value, and a thinking level encoded as a
`model:level` suffix is refused so that two sources can never disagree about
one role's reasoning mode. Profiles may mix providers freely; a profile is a
map of role to concrete pair, not a provider selection.

Validation is structural. Ompire checks that an identifier is shaped like
`provider/model-id` and that the level is one omp accepts. It does not contact
a provider, consult a catalog, or verify credentials, so saving a profile is
never a claim that the model exists or supports the level.

A project optionally references one profile by name as its default. The
reference is non-cascading in both directions: deleting a profile that any
project still names is refused with those project names, and removing a
project releases its reference without touching the profile. Because the
runtime does not enable SQLite foreign-key enforcement, that guarantee is
carried by a `BEGIN IMMEDIATE` write reservation shared by the reference check
and its write, rather than by the declared foreign key alone.

Profiles contain identifiers only — never credentials, commands, prompts, or
workflow settings.

The scope of this decision is configuration, not execution. Task spawning
remains template-driven: profile CRUD and project assignment do not alter
templates, spawn requests, agent argv, running sessions, judge configuration,
review, or shipping. The UI and the operator reference say so where profiles
are managed and assigned.

## Consequences

Model policy is now edited once and reused, and a project's model choice is a
reference to shared policy rather than a copy of it. Changing a profile
changes what every project referencing it points at.

That mutability is exactly why a future consumer must not read a profile at
the moment it needs a model. When task execution starts using profiles, it
must resolve and snapshot the concrete pairs onto the task, so that editing or
deleting a profile cannot retroactively change how an accepted task runs or
how a restarted one resumes. This ADR records that requirement rather than
satisfying it: no task consumer of profiles exists yet.

Until that consumer exists, an operator can save a profile, assign it to
projects, and see none of it affect a task. That gap is real and is stated in
the UI and the operator reference rather than left to be discovered. It closes
when workflow-first task launch replaces template-driven spawning.

The reference guard is enforced by the registry's write reservation, not by
the database. A direct SQL writer, or a future connection that bypasses the
registry, can still create an orphaned reference. Turning on
`PRAGMA foreign_keys` globally would be the durable fix, but it changes
enforcement for every existing table at once and can surface unrelated legacy
inconsistencies, so it is left as a separate decision.

Profiles are daemon-managed data and therefore live in the daemon's data
directory and its migration chain — one more table to migrate, and one more
entity in the WebSocket snapshot.

## Alternatives considered

### Extend templates with the four role bindings

The smallest change: add eight columns to `templates`. Rejected because it
keeps model policy duplicated per template and per project, which is the
problem. It also entrenches templates as the unit of launch configuration
exactly as they are about to be replaced.

### Store profiles in the daemon settings map

Settings are already daemon-writable and UI-editable, so profiles could be a
JSON blob under one key. Rejected: ADR-0013's mechanism is deliberately a flat
map of validated scalars layered over operator TOML. A named collection with
its own lifecycle, sort order, references, and deletion guard is a registry
entity, and modelling it as a setting would either break that boundary or
reimplement a registry inside a value.

### Store profiles in operator `config.toml` or omp's user configuration

Rejected on ownership. `config.toml` is the operator's file, edited by hand and
layered *under* daemon state; writing UI-managed collections into it inverts
that. omp's user configuration belongs to the agent — reading it would make
Ompire's saved state depend on another program's file, and writing it would
have Ompire reconfigure the agent behind the operator's back.

### Make a profile a full launch preset

Bundle repository, workflow, prompt preamble, and models into one "preset".
Rejected because it makes the entity unshareable: the reason a profile is
reusable across projects is precisely that it contains no project or procedure.
Workflow and project selection are separate axes and are chosen separately at
launch.

### Bind a profile to one provider

A profile could name a provider once and list bare model names under it.
Rejected: the roles exist to let cheap and expensive work go to different
models, which routinely means different providers. Requiring one provider per
profile would force operators to give up either the role split or the profile.

### Validate models against a provider catalog before saving

Rejected. It would make saving configuration depend on network reachability
and configured credentials, turn a typo and an outage into the same error, and
still not guarantee the model is available later. Structural validation
refuses what is definitely wrong; the runtime that eventually launches an
agent is where a genuinely unusable model must surface — and it must report
that failure rather than silently substituting a model.
