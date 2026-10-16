# ADR 0042: Pin environment grants at task acceptance

- Status: Accepted
- Date: 2026-10-16

## Context

Brokered QA capabilities need two independent decisions: a workflow declares
what infrastructure it requires, and the operator decides whether this task
may use it. An environment registry alone can report availability but cannot
answer who authorized sandbox access. Ambient repository tunnel declarations
provide access without a task-specific decision; copying broker credentials
into a task would violate [ADR-0015](0015-keep-agent-credentials-behind-narrow-brokers.md).

A task must retain the access it accepted across restart and configuration
edits, just as it retains its workflow and model policy under
[ADR-0026](0026-resolve-launch-inputs-once-and-pin-them-to-the-task.md).
Availability, however, is an observation: a ready preview cannot prove that
infrastructure is ready when a person submits the launch. The grant must
be durable before any workspace or agent can use it, following
[ADR-0030](0030-commit-human-decisions-before-advancing.md).

## Decision

Environment access is an explicit whole-environment operator grant at task
acceptance. A workflow's optional environment requirement belongs to its
retained content identity. The reviewed launch binds the grant selection to
the environment's exact capability map and wiring fragment; readiness and
probe timestamps are not part of that identity.

Acceptance requires a grant exactly when the workflow declares an environment,
freshly probes readiness outside the write reservation, and refuses unready
infrastructure with named blockers. The final transactional resolution stores
the task and its immutable grant together: environment name, capability and
wiring snapshots, grant time, and actor `operator`. Neither restart nor later
configuration edits rederive access for accepted work. An environment without
wiring remains observation-only and adds no sandbox route.

Granted wiring is an opaque, non-secret Workshop additions fragment applied
only around that task's container launch. Workshop owns tunnel semantics; the
daemon does not acquire a second Workshop schema or a new tunnel lifecycle.
Existing additions restoration and interrupted-staging recovery restore the
clone, while container cleanup revokes its tunnels. A surviving container
keeps its wiring across daemon restart. A legacy continuation cannot create
wiring and must refuse a requiring definition in favor of a newly granted task.

A non-empty selected additions source and environment wiring are refused,
not merged. The project-source guard inspects the accepted Git tree and pins
that source commit so later branch movement cannot substitute uninspected
content. Global-source inspection reads the operator's selected file.

Grant-gating covers **daemon-applied wiring only**. A repository can statically
declare the same plug in its own Workshop document and give every task the
route. Environment-backed broker endpoints must therefore stay out of static
repository declarations. Repository schema policing is not a daemon concern;
the operator controls the QA sandbox repository and the deployment boundary.

Brokered grants never deliver reusable credentials to a task. Ending a task
revokes its channel, not the broker's credential. There is no credential
dirty-marking or automatic rotation merely because a task ran; actual
trusted-side credential exposure still requires management-plane rotation.
This generalizes ADR-0015 alongside
[ADR-0041](0041-broker-forge-access-behind-read-only-proxy.md)'s read-only forge
policy without changing trusted signing, push, or publication authority.

## Consequences

Access is attributable to one recorded human act and survives restart without
silently expanding when configuration changes. Unrelated workflows retain
their existing launch path and identity. The cost is intentional snapshot
duplication, a bounded readiness subprocess on requiring previews and accepts,
and an additional grant decision for the operator.

The daemon validates wiring as a non-empty YAML mapping, not as a promise that
Workshop will connect it correctly. Operators must supply trusted fragments
and working brokers. Availability at acceptance is not a lease guaranteeing
availability throughout execution. A tunnel limits credential disclosure,
not misuse of the granted capability while it exists.

Refusing additions composition excludes legitimate combined overlays until
reviewed merge semantics exist. Revisit this decision if narrower grants,
multiple operators, remote execution hosts, or a Workshop-native immutable
capability interface require a different authority boundary. Any replacement
must preserve explicit task-scoped authorization, durable attribution,
credential non-disclosure, and container-scoped revocation.

## Alternatives considered

### Apply environment wiring whenever a workflow names it

This removes a form control but turns a repository-authored requirement into
authority. A workflow edit could silently expand sandbox access without a
person granting the newly requested capability.

### Re-read environment configuration during spawn and recovery

This avoids snapshots but makes an accepted task's authority change after a
configuration edit or restart. The operator would have reviewed different
wiring from the wiring the task receives.

### Merge selected additions and wiring automatically

This preserves more launch combinations but invents overlay precedence that
neither the operator nor the daemon has reviewed. Refusal is a smaller and
safer contract than a second Workshop merge implementation.

### Inject credentials and rotate them after each task

This can support unbrokered providers, but gives the untrusted task an
exportable credential throughout its lifetime. Rotation cannot undo disclosure
or past use. Narrow brokers and task-scoped channels avoid giving it that
material in the first place.
