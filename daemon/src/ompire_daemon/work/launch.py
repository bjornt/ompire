"""Launch resolution: turning what the operator selected into the inputs one
task will run under, by exactly one set of rules.

Preview and acceptance call the same `resolve_launch`. That is the point of
this module: the operator reviews a resolution, and the thing that is stored
is that same resolution recomputed under a write reservation and compared —
never a second, subtly different reading of the same selections.

Resolution is pure with respect to the world outside the database. It touches
no filesystem, runs no git, spawns nothing, and publishes no event, so it can
be run inside `BEGIN IMMEDIATE` without holding the write lock across
anything slow. Git and mention validation happen outside the lock, before it.

What is resolved now includes the *workflow definition itself* (ADR-0028).
The preview names an exact revision, acceptance re-checks that same revision
under the write reservation, and the task stores it. Editing a prompt or a
route therefore invalidates a reviewed preview — the operator reviews a
procedure, not a workflow's name — while an unrelated workflow or profile
change leaves it valid.

ADR-0026 (docs/adr/0026-resolve-launch-inputs-once-and-pin-them-to-the-task.md)
ADR-0028 (docs/adr/0028-retain-declarative-workflow-revisions.md)
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection

from ompire_daemon.db import model_profiles as model_profiles_table
from ompire_daemon.db import projects as projects_table
from ompire_daemon.db import tasks as tasks_table
from ompire_daemon.handoff import HandoffError, plan_destinations
from ompire_daemon.model_config import MODEL_ROLES, RoleBinding, validate_model_role
from ompire_daemon.registry.results import (
    ResultNotAttachableError,
    ResultNotFoundError,
    StaleRevisionError,
    verify_attachable_on,
    verify_payload_on,
)
from ompire_daemon.registry.workflow_library import (
    UnknownWorkflowNameError,
    WorkflowNotLaunchableError,
    resolve_current,
)
from ompire_daemon.work.inputs import (
    AUXILIARY_CONSUMERS,
    HANDOFF_CLASSIFICATION,
    PROFILE_SOURCE_PROJECT,
    PROFILE_SOURCE_STEP,
    PROFILE_SOURCE_TASK,
    PROVENANCE_ACCEPTED,
    ROLE_SOURCE_STEP,
    ROLE_SOURCE_WORKFLOW,
    WORKFLOW_SOURCE_ACCEPTED,
    WORKSPACE_FIELDS,
    AttachedFile,
    BaseComparison,
    ConsumerBinding,
    ResultAttachment,
    TaskExecutionInputs,
    WorkflowBinding,
    WorkspaceInputs,
    decode_roles,
)
from ompire_daemon.work.projects import (
    validate_branch_pattern,
    validate_workshop_additions,
)
from ompire_daemon.workflow_definitions import WorkflowRevision, describe


class LaunchInputError(ValueError):
    """A submitted launch input is unusable. `field` addresses the form
    control the operator has to fix, so a refusal never reads as "something
    was wrong somewhere"."""

    def __init__(self, field: str, detail: str) -> None:
        super().__init__(detail)
        self.field = field
        self.detail = detail


class ProjectNotLaunchableError(Exception):
    """The project exists but cannot launch: its checkout is not ready, or
    its carried-over launch configuration still needs a decision. Reported
    separately from a bad input — the fix is elsewhere, and the message says
    where."""

    def __init__(self, name: str, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.name = name
        self.reason = reason
        self.detail = detail


class PreviewChangedError(Exception):
    """The reviewed resolution is no longer what the same selections resolve
    to. Creation is refused so the operator reviews the changed choices; no
    task, workspace, or background job is created."""

    def __init__(self, resolved: ResolvedLaunch | None) -> None:
        super().__init__(
            "the launch configuration changed since it was previewed; "
            "review the current resolution and submit again"
        )
        self.reason = "preview_changed"
        self.resolved = resolved


@dataclass(frozen=True)
class ConsumerOverride:
    """One model consumer's row-level selections.

    Both dimensions are independently three-valued: `None` means "inherit",
    a value means "the operator chose this". An explicit choice that happens
    to equal the inherited value is still an explicit choice — it survives a
    later task-profile change, which is the whole difference between having
    chosen and not having chosen.
    """

    model_profile: str | None = None
    role: str | None = None

    def is_empty(self) -> bool:
        return self.model_profile is None and self.role is None


@dataclass(frozen=True)
class LaunchRequest:
    """Normalized submitted selections.

    `model_profile` is three-valued through `profile_explicit`: an explicit
    task profile, or inheritance from the project. `workspace_overrides`
    carries only the fields the operator actually overrode — an absent key
    means inherit, and an empty `preamble` string is an override to "no
    preamble", not an absence.

    `step_overrides` and `auxiliary_overrides` are separate namespaces on
    purpose, and an unknown key in either is refused rather than quietly
    resolved against the other. There are no auxiliary consumers left, so the
    second namespace is now only how a request naming the *retired* judge is
    refused explicitly instead of being dropped.
    """

    project_name: str
    workflow_name: str
    slug: str
    prompt: str
    model_profile: str | None
    profile_explicit: bool
    workspace_overrides: Mapping[str, str]
    step_overrides: Mapping[str, ConsumerOverride] = field(default_factory=dict)
    auxiliary_overrides: Mapping[str, ConsumerOverride] = field(default_factory=dict)
    # Accepted result revisions to install before the first step runs
    # (ADR-0035). Empty is an ordinary launch, not an error.
    result_attachments: tuple[AttachmentSelection, ...] = ()
    # The operator's acknowledgement that a plan captured against a different
    # or unknown base has not been validated against this target. Bound to
    # *this* attachment set and target commit by the fingerprint, so it cannot
    # be carried over to a different selection.
    acknowledge_result_base_difference: bool = False


@dataclass(frozen=True)
class AttachmentSelection:
    """One selected revision, named the only way that identifies it exactly.

    `expected_manifest_id` is what makes this a *revision* rather than a
    pointer: a successor capture on the same producing task does not match it,
    so a stale selection is refused instead of silently upgraded.
    """

    producer_task_id: int
    result_id: str
    expected_manifest_id: str


@dataclass(frozen=True)
class TargetEvidence:
    """The Git reading a launch is reviewed against, made outside any lock.

    Resolution stays pure with respect to the world: this is passed *in*, so
    the same evidence is used by the preview the operator read and by the
    acceptance that recomputes it. `commit` is immutable, which is what makes
    binding it into the fingerprint meaningful — and what makes a ref that
    moved afterwards a visible refusal rather than a silent retarget.
    """

    commit: str
    # `(reason, detail, path)` per refused destination, already classified.
    conflicts: tuple[tuple[str, str, str | None], ...] = ()
    # result id → the comparison observed for that attachment.
    comparisons: Mapping[str, BaseComparison] = field(default_factory=dict)


@dataclass(frozen=True)
class PreviewRow:
    """One model-consuming (or deliberately model-free) row of the preview.

    A command, decision, or gate row carries no binding at all. Showing one
    would be a fiction: those steps never reach a provider, and the form
    renders no override controls for them.
    """

    step: str
    kind: str
    session: str | None
    conditional: bool
    # The role the *definition* declares for this step. Kept beside the
    # effective role so the form can say what resetting the role override
    # would restore.
    declared_role: str | None
    binding: ConsumerBinding | None

    @property
    def role(self) -> str | None:
        return self.binding.role if self.binding else None

    @property
    def model(self) -> str | None:
        return self.binding.binding.model if self.binding else None

    @property
    def thinking(self) -> str | None:
        """The accepted thinking *policy*, spelled as the profile spells it.
        omp may resolve `auto`/`max` to a model-specific level at run time;
        that resolved state is reported separately, from the running
        process."""
        return self.binding.binding.thinking if self.binding else None


@dataclass(frozen=True)
class ResolvedLaunch:
    inputs: TaskExecutionInputs
    # The exact definition this launch would pin. Held whole, not just named,
    # so acceptance retains the same document the preview was built from.
    revision: WorkflowRevision
    rows: tuple[PreviewRow, ...]
    fingerprint: str
    # Display facts the form shows beside the rows.
    profile_source: str
    project_default_profile: str | None
    inherited_workspace: WorkspaceInputs
    # The task-wide effective profile's own four-role map: what a row that
    # inherits both dimensions resolves against.
    task_roles: dict[str, RoleBinding]
    # Whether this launch still needs the operator to acknowledge an
    # unvalidated base. False both when nothing needs it and when it was given.
    needs_base_acknowledgement: bool = False


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _read_project(conn: Connection, name: str):
    row = conn.execute(
        projects_table.select().where(projects_table.c.name == name)
    ).first()
    if row is None:
        raise LaunchInputError("project_name", f"project {name!r} not found")
    return row


def _read_profile_roles(conn: Connection, name: str, *, field: str) -> dict[str, RoleBinding]:
    row = conn.execute(
        model_profiles_table.select().where(model_profiles_table.c.name == name)
    ).first()
    if row is None:
        raise LaunchInputError(field, f"model profile {name!r} not found")
    return decode_roles(json.loads(row.roles_json))


class _ProfileReader:
    """Reads each distinct profile once per resolution.

    Rows share role values freely: `RoleBinding` is frozen and the snapshot
    is copied into every binding's own dict, so sharing the immutable pairs
    costs nothing and keeps a twelve-row preview from issuing twelve
    identical selects inside the write reservation.
    """

    def __init__(self, conn: Connection) -> None:
        self._conn = conn
        self._cache: dict[str, dict[str, RoleBinding]] = {}

    def roles(self, name: str, *, field: str) -> dict[str, RoleBinding]:
        if name not in self._cache:
            self._cache[name] = _read_profile_roles(self._conn, name, field=field)
        return dict(self._cache[name])


def _resolve_workspace(
    project_row, overrides: Mapping[str, str]
) -> tuple[WorkspaceInputs, tuple[str, ...]]:
    unknown = sorted(set(overrides) - set(WORKSPACE_FIELDS))
    if unknown:
        raise LaunchInputError(
            "workspace_overrides", f"unknown override fields: {', '.join(unknown)}"
        )
    values: dict[str, str] = {}
    applied: list[str] = []
    for name in WORKSPACE_FIELDS:
        if name in overrides:
            values[name] = overrides[name]
            applied.append(name)
        else:
            values[name] = getattr(project_row, name)
    if not values["base_branch"].strip():
        raise LaunchInputError(
            "workspace_overrides.base_branch", "base branch must not be empty"
        )
    try:
        validate_branch_pattern(values["branch_pattern"])
    except ValueError as exc:
        raise LaunchInputError("workspace_overrides.branch_pattern", str(exc)) from exc
    try:
        validate_workshop_additions(values["workshop_additions"])
    except ValueError as exc:
        raise LaunchInputError(
            "workspace_overrides.workshop_additions", str(exc)
        ) from exc
    return (
        WorkspaceInputs(
            base_branch=values["base_branch"],
            branch_pattern=values["branch_pattern"],
            workshop_additions=values["workshop_additions"],
            preamble=values["preamble"],
        ),
        tuple(applied),
    )


def _resolve_binding(
    reader: _ProfileReader,
    override: ConsumerOverride | None,
    *,
    field: str,
    declared_role: str,
    task_profile: str,
    task_profile_source: str,
) -> ConsumerBinding:
    """One consumer's effective binding.

    Profile precedence is row override → task-wide decision (itself already
    task-explicit or project-default). Role precedence is row override →
    declared role. The two are resolved independently and then read *one*
    pair out of *one* profile: a role never survives its profile, because
    changing the role changes the concrete model and the thinking level
    together, which is what makes a role an abstraction rather than a label.
    """
    override = override or ConsumerOverride()
    if override.model_profile is not None:
        profile_name = override.model_profile
        profile_source = PROFILE_SOURCE_STEP
        roles = reader.roles(profile_name, field=f"{field}.model_profile")
    else:
        profile_name = task_profile
        profile_source = task_profile_source
        roles = reader.roles(profile_name, field="model_profile")
    if override.role is not None:
        try:
            validate_model_role(override.role)
        except ValueError as exc:
            raise LaunchInputError(f"{field}.role", str(exc)) from exc
        role = override.role
        role_source = ROLE_SOURCE_STEP
    else:
        role = declared_role
        role_source = ROLE_SOURCE_WORKFLOW
    return ConsumerBinding(
        profile_name=profile_name,
        profile_source=profile_source,
        role=role,
        role_source=role_source,
        roles=roles,
    )


def _resolve_consumers(
    reader: _ProfileReader,
    request: LaunchRequest,
    revision: WorkflowRevision,
    *,
    task_profile: str,
    task_profile_source: str,
) -> tuple[dict[str, ConsumerBinding], tuple[PreviewRow, ...]]:
    """Resolve every declared consumer and build the preview in one pass, so
    what the operator reviews and what the task stores cannot drift apart.

    Every model consumer is now a *declared step*. The engine reserves none:
    with the implicit judge gone there is no row for a model that runs outside
    the definition, and nothing in the preview stands for work the operator
    cannot see in the flow.
    """
    workflow_name = revision.name
    descriptor = describe(revision)
    agent_steps = {
        step.name: step.role for step in descriptor.steps if step.role is not None
    }

    unknown_steps = sorted(set(request.step_overrides) - set(agent_steps))
    if unknown_steps:
        # A non-agent step is named separately from a step that does not
        # exist: "there is no such step" and "that step has no model" are
        # different corrections.
        declared = {step.name for step in descriptor.steps}
        for name in unknown_steps:
            detail = (
                f"step {name!r} has no model binding; only agent steps can be "
                "overridden"
                if name in declared
                else f"workflow {workflow_name!r} declares no step {name!r}"
            )
            raise LaunchInputError(f"step_overrides.{name}", detail)
    unknown_auxiliary = sorted(set(request.auxiliary_overrides) - set(AUXILIARY_CONSUMERS))
    if unknown_auxiliary:
        # There are no engine-reserved consumers left. A request still naming
        # the retired judge is refused with the field, not silently dropped:
        # dropping it would accept a launch the operator believes configures
        # a model that no longer runs at all.
        name = unknown_auxiliary[0]
        detail = (
            "the engine-reserved judge was removed; workflows no longer run an "
            "implicit model, and unresolved evidence pauses for you instead"
            if name == "judge"
            else f"unknown auxiliary model consumer {name!r}"
        )
        raise LaunchInputError(f"auxiliary_overrides.{name}", detail)

    step_bindings = {
        name: _resolve_binding(
            reader,
            request.step_overrides.get(name),
            field=f"step_overrides.{name}",
            declared_role=declared_role,
            task_profile=task_profile,
            task_profile_source=task_profile_source,
        )
        for name, declared_role in agent_steps.items()
    }
    rows = [
        PreviewRow(
            step=step.name,
            kind=step.kind,
            session=step.session,
            conditional=step.conditional,
            declared_role=step.role,
            # A command, decision, or gate has no model. Showing one would be
            # a fiction — those steps never reach a provider — and the form
            # renders no override controls where there is no binding.
            binding=step_bindings[step.name] if step.role is not None else None,
        )
        for step in descriptor.steps
    ]
    return step_bindings, tuple(rows)


def resolve_target_context(conn: Connection, request: LaunchRequest) -> tuple[str, str]:
    """The `(checkout_path, base_branch)` an attachment observation is made
    against, by exactly the rules resolution will apply.

    Read separately because the Git work has to happen *outside* the write
    reservation, and it needs to know which checkout and which branch before
    the full resolution runs. It reuses the same project read and the same
    workspace override resolution, so the observation can never be made against
    a different base than the one the launch is then pinned to.
    """
    project_row = _read_project(conn, request.project_name)
    workspace, _applied = _resolve_workspace(project_row, request.workspace_overrides)
    return project_row.checkout_path, workspace.base_branch


def _read_task_project(conn: Connection, task_id: int) -> str | None:
    row = conn.execute(
        tasks_table.select()
        .with_only_columns(tasks_table.c.project_name)
        .where(tasks_table.c.id == task_id)
    ).first()
    return row.project_name if row is not None else None


def _resolve_attachments(
    conn: Connection,
    request: LaunchRequest,
    evidence: TargetEvidence | None,
    *,
    project_name: str,
) -> tuple[tuple[ResultAttachment, ...], tuple[BaseComparison, ...], bool]:
    """Turn the selected revisions into pinned inputs, or refuse the launch.

    Everything here reads on the caller's connection. Inside acceptance that
    connection holds the write reservation, so a purge committing alongside
    cannot land between "these bytes are intact" and "this task now references
    them" — the two are one transaction or neither happens (ADR-0035).

    Project membership is decided through *current* task records: the manifest
    also carries a project label, but that is what the project was called when
    the bundle was captured. Reading it as authority would turn an ordinary
    project rename into a refused cross-project transfer.
    """
    if not request.result_attachments:
        return (), (), False
    if evidence is None:
        raise LaunchInputError(
            "result_attachments",
            "attaching a result needs the target base to have been read; no "
            "target observation was supplied",
        )

    attachments: list[ResultAttachment] = []
    for selection in request.result_attachments:
        field_name = f"result_attachments.{selection.result_id}"
        producer_project = _read_task_project(conn, selection.producer_task_id)
        if producer_project is None:
            raise LaunchInputError(
                field_name,
                f"the producing task {selection.producer_task_id} no longer "
                "exists, so its result cannot be attached",
            )
        if producer_project != project_name:
            raise LaunchInputError(
                field_name,
                f"result {selection.result_id} belongs to project "
                f"{producer_project!r}; a task can only attach results from its "
                "own project",
            )
        try:
            result = verify_attachable_on(
                conn,
                selection.result_id,
                expected_manifest_id=selection.expected_manifest_id,
            )
        except ResultNotFoundError as exc:
            raise LaunchInputError(field_name, str(exc)) from exc
        except StaleRevisionError as exc:
            raise LaunchInputError(field_name, str(exc)) from exc
        except ResultNotAttachableError as exc:
            raise LaunchInputError(field_name, str(exc)) from exc
        if result.task_id != selection.producer_task_id:
            raise LaunchInputError(
                field_name,
                f"result {selection.result_id} was produced by task "
                f"{result.task_id}, not {selection.producer_task_id}",
            )
        try:
            # Re-checked at consumption, not merely trusted from capture time:
            # an accepted revision is read back from a database that may have
            # been restored or damaged since the decision was made.
            verify_payload_on(conn, result)
        except ResultNotAttachableError as exc:
            raise LaunchInputError(field_name, str(exc)) from exc
        manifest = result.manifest or {}
        attachments.append(
            ResultAttachment(
                result_id=result.id,
                producer_task_id=result.task_id,
                manifest_id=result.manifest_id or "",
                content_id=result.content_id,
                accepted_at=result.accepted_at or "",
                manifest_project_name=str(manifest.get("project_name") or ""),
                files=tuple(
                    AttachedFile(
                        path=entry.path,
                        length=entry.length,
                        sha256=entry.sha256,
                        media_type=entry.media_type,
                    )
                    for entry in result.files
                ),
                provenance=dict(manifest.get("provenance") or {}),
                classification=HANDOFF_CLASSIFICATION,
            )
        )

    try:
        plan_destinations(attachments)
    except HandoffError as exc:
        raise LaunchInputError("result_attachments", exc.detail) from exc

    # The destinations the operator reviewed against this exact commit. A
    # conflict here is resolved by choosing differently — never by overwriting,
    # skipping a file, or merging.
    if evidence.conflicts:
        _reason, detail, _path = evidence.conflicts[0]
        raise LaunchInputError("result_attachments", detail)

    comparisons: list[BaseComparison] = []
    for attachment in attachments:
        comparison = evidence.comparisons.get(attachment.result_id)
        if comparison is None:
            raise LaunchInputError(
                f"result_attachments.{attachment.result_id}",
                "the target base was not compared with this revision's recorded "
                "base observation; review the launch again",
            )
        comparisons.append(comparison)
    needs_acknowledgement = any(
        comparison.needs_acknowledgement for comparison in comparisons
    )
    if needs_acknowledgement and not request.acknowledge_result_base_difference:
        raise LaunchInputError(
            "acknowledge_result_base_difference",
            "these files were captured against a different or unknown base, so "
            "they have not been validated against this target; acknowledge that "
            "before launching",
        )
    return tuple(attachments), tuple(comparisons), needs_acknowledgement


def launch_fingerprint(
    request: LaunchRequest,
    inputs: TaskExecutionInputs,
    revision: WorkflowRevision,
    rows: tuple[PreviewRow, ...],
) -> str:
    """A deterministic value over the submitted selections, everything they
    resolved to, and the workflow descriptor they were resolved against.

    Deliberately *not* a global settings timestamp: editing an unrelated
    profile, renaming another project, or any write that does not change this
    launch must leave a reviewed preview valid.

    Every consumer's *complete* four-role map is covered, not just its active
    pair. An edit that moves only a profile's `slow` binding changes what a
    `/switch slow` in the container would run, so it changes the launch the
    operator reviewed — even though every model shown in the summary line is
    still the same string.

    The workflow's *revision* is covered for the same reason (ADR-0028). It is
    a content identity, so editing a prompt or a route — anything that changes
    what the run would do — changes the fingerprint and invalidates the
    preview, while the step list stayed identical.
    """
    digest = hashlib.sha256()

    def binding_digest(binding: ConsumerBinding) -> dict[str, Any]:
        return {
            "profile": binding.profile_name,
            "profile_source": binding.profile_source,
            "role": binding.role,
            "role_source": binding.role_source,
            "roles": {
                role: [binding.roles[role].model, binding.roles[role].thinking]
                for role in MODEL_ROLES
            },
        }

    def override_digest(
        overrides: Mapping[str, ConsumerOverride],
    ) -> dict[str, list[str | None]]:
        return {
            name: [override.model_profile, override.role]
            for name, override in sorted(overrides.items())
        }

    digest.update(
        json.dumps(
            {
                "request": {
                    "project_name": request.project_name,
                    "workflow_name": request.workflow_name,
                    "slug": request.slug,
                    "prompt": request.prompt,
                    "model_profile": request.model_profile,
                    "profile_explicit": request.profile_explicit,
                    "workspace_overrides": dict(sorted(request.workspace_overrides.items())),
                    "step_overrides": override_digest(request.step_overrides),
                    "auxiliary_overrides": override_digest(request.auxiliary_overrides),
                    "result_attachments": [
                        [
                            selection.producer_task_id,
                            selection.result_id,
                            selection.expected_manifest_id,
                        ]
                        for selection in request.result_attachments
                    ],
                    "acknowledge_result_base_difference": (
                        request.acknowledge_result_base_difference
                    ),
                },
                "resolved": {
                    "profile": inputs.model_profile_name,
                    "profile_source": inputs.model_profile_source,
                    "step_bindings": {
                        name: binding_digest(binding)
                        for name, binding in sorted(inputs.step_bindings.items())
                    },
                    "workspace": [
                        inputs.workspace.base_branch,
                        inputs.workspace.branch_pattern,
                        inputs.workspace.workshop_additions,
                        inputs.workspace.preamble,
                    ],
                    "branch": inputs.branch,
                    "checkout_path": inputs.checkout_path,
                    "fetch_remote": inputs.fetch_remote,
                    "upstream_url": inputs.upstream_url,
                    "fork_url": inputs.fork_url,
                    # Immutable commit and tree data only (ADR-0035). No read
                    # timestamp is covered: re-reading an unchanged base must
                    # leave a reviewed preview valid, while a base that moved,
                    # a revision that was replaced, or a destination that
                    # became occupied must not.
                    "source_commit": inputs.source_commit,
                    "attachments": [
                        {
                            "result_id": attachment.result_id,
                            "producer_task_id": attachment.producer_task_id,
                            "manifest_id": attachment.manifest_id,
                            "content_id": attachment.content_id,
                            "classification": attachment.classification,
                            "destinations": list(attachment.destinations),
                        }
                        for attachment in inputs.result_attachments
                    ],
                    "base_comparisons": [
                        [
                            comparison.result_id,
                            comparison.state,
                            comparison.producer_observation,
                        ]
                        for comparison in inputs.base_comparisons
                    ],
                    "acknowledged_base_difference": (
                        inputs.acknowledged_base_difference
                    ),
                },
                "workflow": {
                    "name": revision.name,
                    "revision": revision.revision,
                    "format": revision.format,
                },
                "catalog": [
                    [
                        row.step,
                        row.kind,
                        row.session,
                        row.declared_role,
                        row.conditional,
                    ]
                    for row in rows
                ],
            },
            sort_keys=True,
        ).encode("utf-8")
    )
    return digest.hexdigest()[:32]


def resolve_launch(
    conn: Connection,
    request: LaunchRequest,
    *,
    evidence: TargetEvidence | None = None,
) -> ResolvedLaunch:
    """Resolve one launch, or refuse it. Reads only; the caller decides
    whether that read sits in a reservation.

    `evidence` is the Git reading made outside the lock — the target commit and
    what its tree already holds at the attached destinations. It is required
    for a launch with attachments and unused without them, which is what keeps
    an ordinary launch resolving exactly as it did before.
    """
    project_row = _read_project(conn, request.project_name)
    if project_row.setup_state != "ready":
        raise ProjectNotLaunchableError(
            project_row.name,
            "checkout-not-ready",
            f"project {project_row.name!r} is not ready "
            f"(setup {project_row.setup_state}); finish or retry its checkout setup first",
        )
    if project_row.launch_config_state != "reconciled":
        raise ProjectNotLaunchableError(
            project_row.name,
            "launch-config-unreconciled",
            f"project {project_row.name!r} still needs launch-configuration "
            "reconciliation; resolve it in the project editor before launching",
        )

    # The library's current selection for this name, read on *this*
    # connection (ADR-0031). Inside acceptance that connection holds the write
    # reservation, so an executable save or an archive committing alongside
    # cannot land between the check and the pin. An archived, draft-only, or
    # damaged entry refuses the launch and says which — it never resolves to
    # some other revision.
    try:
        revision = resolve_current(conn, request.workflow_name)
    except (UnknownWorkflowNameError, WorkflowNotLaunchableError) as exc:
        raise LaunchInputError("workflow_name", str(exc)) from exc

    reader = _ProfileReader(conn)
    # The task-wide decision is still mandatory, and still resolved first:
    # a row that overrides only its role inherits this profile, and a task
    # whose every row is overridden is still a task the operator has to
    # answer the "which profile" question for.
    if request.profile_explicit and request.model_profile is not None:
        profile_name = request.model_profile
        profile_source = PROFILE_SOURCE_TASK
        roles = reader.roles(profile_name, field="model_profile")
    elif project_row.default_model_profile is not None:
        profile_name = project_row.default_model_profile
        profile_source = PROFILE_SOURCE_PROJECT
        roles = reader.roles(profile_name, field="project_name")
    else:
        raise LaunchInputError(
            "model_profile",
            f"project {project_row.name!r} has no default model profile; "
            "select a model profile for this task",
        )

    step_bindings, rows = _resolve_consumers(
        reader,
        request,
        revision,
        task_profile=profile_name,
        task_profile_source=profile_source,
    )

    workspace, applied = _resolve_workspace(project_row, request.workspace_overrides)
    branch = workspace.branch_pattern.replace("<slug>", request.slug)

    attachments, comparisons, needs_acknowledgement = _resolve_attachments(
        conn, request, evidence, project_name=project_row.name
    )

    now = _now_iso()
    inputs = TaskExecutionInputs(
        provenance=PROVENANCE_ACCEPTED,
        accepted_at=now,
        project_name=project_row.name,
        workflow_name=revision.name,
        # Pinned before the first step runs, so there is no history to
        # disclaim: this task's whole run belongs to this revision.
        workflow_binding=WorkflowBinding(
            revision=revision.revision,
            source=WORKFLOW_SOURCE_ACCEPTED,
            bound_at=now,
        ),
        model_profile_name=profile_name,
        model_profile_source=profile_source,
        step_bindings=step_bindings,
        workspace=workspace,
        workspace_overrides=applied,
        branch=branch,
        checkout_path=project_row.checkout_path,
        fetch_remote=project_row.fetch_remote,
        upstream_url=project_row.upstream_url,
        fork_url=project_row.fork_url,
        result_attachments=attachments,
        # Pinned only for an attachment launch. An ordinary launch keeps its
        # branch-based behavior and records no commit observation, so nothing
        # about it changes.
        source_commit=evidence.commit if attachments and evidence else None,
        base_comparisons=comparisons,
        acknowledged_base_difference=(
            needs_acknowledgement and request.acknowledge_result_base_difference
        ),
    )
    return ResolvedLaunch(
        inputs=inputs,
        revision=revision,
        rows=rows,
        fingerprint=launch_fingerprint(request, inputs, revision, rows),
        profile_source=profile_source,
        project_default_profile=project_row.default_model_profile,
        inherited_workspace=WorkspaceInputs(
            base_branch=project_row.base_branch,
            branch_pattern=project_row.branch_pattern,
            workshop_additions=project_row.workshop_additions,
            preamble=project_row.preamble,
        ),
        task_roles=roles,
        needs_base_acknowledgement=needs_acknowledgement,
    )


