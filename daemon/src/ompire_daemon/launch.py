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

ADR-0026 (docs/adr/0026-resolve-launch-inputs-once-and-pin-them-to-the-task.md)
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
from ompire_daemon.execution_inputs import (
    AUXILIARY_CONSUMERS,
    AUXILIARY_JUDGE,
    PROFILE_SOURCE_PROJECT,
    PROFILE_SOURCE_STEP,
    PROFILE_SOURCE_TASK,
    PROVENANCE_ACCEPTED,
    ROLE_SOURCE_STEP,
    ROLE_SOURCE_WORKFLOW,
    WORKSPACE_FIELDS,
    ConsumerBinding,
    TaskExecutionInputs,
    WorkspaceInputs,
    decode_roles,
    encode_binding,
)
from ompire_daemon.model_config import JUDGE_ROLE, MODEL_ROLES, validate_model_role
from ompire_daemon.registry.model_profiles import RoleBinding
from ompire_daemon.registry.projects import (
    validate_branch_pattern,
    validate_workshop_additions,
)
from ompire_daemon.workflows import (
    JUDGE_SESSION,
    UnknownWorkflowNameError,
    describe_workflow,
    get_workflow,
)


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
    purpose: a decision step can never become an agent binding by sharing a
    name with the judge, and an unknown key in either is refused rather than
    quietly resolved against the other.
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
    # The role the *workflow* declares for this step (or the judge's fixed
    # auxiliary role). Kept beside the effective role so the form can say what
    # resetting the role override would restore.
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
    rows: tuple[PreviewRow, ...]
    fingerprint: str
    # Display facts the form shows beside the rows.
    profile_source: str
    project_default_profile: str | None
    inherited_workspace: WorkspaceInputs
    # The task-wide effective profile's own four-role map: what a row that
    # inherits both dimensions resolves against.
    task_roles: dict[str, RoleBinding]


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
    workflow_name: str,
    *,
    task_profile: str,
    task_profile_source: str,
) -> tuple[
    dict[str, ConsumerBinding], dict[str, ConsumerBinding], tuple[PreviewRow, ...]
]:
    """Resolve every declared consumer and build the preview in one pass, so
    what the operator reviews and what the task stores cannot drift apart."""
    descriptor = describe_workflow(get_workflow(workflow_name))
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
        raise LaunchInputError(
            f"auxiliary_overrides.{unknown_auxiliary[0]}",
            f"unknown auxiliary model consumer {unknown_auxiliary[0]!r}",
        )

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
    auxiliary_bindings = {
        AUXILIARY_JUDGE: _resolve_binding(
            reader,
            request.auxiliary_overrides.get(AUXILIARY_JUDGE),
            field=f"auxiliary_overrides.{AUXILIARY_JUDGE}",
            declared_role=descriptor.judge_role,
            task_profile=task_profile,
            task_profile_source=task_profile_source,
        )
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
    rows.append(
        PreviewRow(
            step=descriptor.judge_session,
            kind="judge",
            session=descriptor.judge_session,
            conditional=True,  # only when a deterministic route or outcome fails
            declared_role=descriptor.judge_role,
            binding=auxiliary_bindings[AUXILIARY_JUDGE],
        )
    )
    return step_bindings, auxiliary_bindings, tuple(rows)


def launch_fingerprint(
    request: LaunchRequest, inputs: TaskExecutionInputs, rows: tuple[PreviewRow, ...]
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
                },
                "resolved": {
                    "profile": inputs.model_profile_name,
                    "profile_source": inputs.model_profile_source,
                    "step_bindings": {
                        name: binding_digest(binding)
                        for name, binding in sorted(inputs.step_bindings.items())
                    },
                    "auxiliary_bindings": {
                        name: binding_digest(binding)
                        for name, binding in sorted(inputs.auxiliary_bindings.items())
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


def resolve_launch(conn: Connection, request: LaunchRequest) -> ResolvedLaunch:
    """Resolve one launch, or refuse it. Reads only; the caller decides
    whether that read sits in a reservation."""
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

    try:
        workflow = get_workflow(request.workflow_name)
    except UnknownWorkflowNameError as exc:
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

    step_bindings, auxiliary_bindings, rows = _resolve_consumers(
        reader,
        request,
        workflow.name,
        task_profile=profile_name,
        task_profile_source=profile_source,
    )

    workspace, applied = _resolve_workspace(project_row, request.workspace_overrides)
    branch = workspace.branch_pattern.replace("<slug>", request.slug)

    inputs = TaskExecutionInputs(
        provenance=PROVENANCE_ACCEPTED,
        accepted_at=_now_iso(),
        project_name=project_row.name,
        workflow_name=workflow.name,
        model_profile_name=profile_name,
        model_profile_source=profile_source,
        step_bindings=step_bindings,
        auxiliary_bindings=auxiliary_bindings,
        workspace=workspace,
        workspace_overrides=applied,
        branch=branch,
        checkout_path=project_row.checkout_path,
        fetch_remote=project_row.fetch_remote,
        upstream_url=project_row.upstream_url,
        fork_url=project_row.fork_url,
    )
    return ResolvedLaunch(
        inputs=inputs,
        rows=rows,
        fingerprint=launch_fingerprint(request, inputs, rows),
        profile_source=profile_source,
        project_default_profile=project_row.default_model_profile,
        inherited_workspace=WorkspaceInputs(
            base_branch=project_row.base_branch,
            branch_pattern=project_row.branch_pattern,
            workshop_additions=project_row.workshop_additions,
            preamble=project_row.preamble,
        ),
        task_roles=roles,
    )


def binding_payload(binding: ConsumerBinding) -> dict[str, Any]:
    """One consumer's wire shape.

    Byte-identical to what acceptance stores for that consumer, so the row
    the operator reviewed and the row the run executes are comparable
    without translation. The effective model and thinking level stay on the
    preview row itself rather than being duplicated in here.
    """
    return encode_binding(binding)


def resolution_payload(resolved: ResolvedLaunch) -> dict[str, Any]:
    """The preview's wire shape."""
    inputs = resolved.inputs
    return {
        "preview_token": resolved.fingerprint,
        "project_name": inputs.project_name,
        "workflow_name": inputs.workflow_name,
        "model_profile": inputs.model_profile_name,
        "model_profile_source": inputs.model_profile_source,
        "project_default_model_profile": resolved.project_default_profile,
        "judge_session": JUDGE_SESSION,
        "judge_role": JUDGE_ROLE,
        "auxiliary_consumers": list(AUXILIARY_CONSUMERS),
        # The task-wide profile's own map: what a row inheriting both
        # dimensions resolves against.
        "roles": {
            role: {
                "model": resolved.task_roles[role].model,
                "thinking": resolved.task_roles[role].thinking,
            }
            for role in MODEL_ROLES
        },
        "workspace": {
            "base_branch": inputs.workspace.base_branch,
            "branch_pattern": inputs.workspace.branch_pattern,
            "workshop_additions": inputs.workspace.workshop_additions,
            "preamble": inputs.workspace.preamble,
        },
        "inherited_workspace": {
            "base_branch": resolved.inherited_workspace.base_branch,
            "branch_pattern": resolved.inherited_workspace.branch_pattern,
            "workshop_additions": resolved.inherited_workspace.workshop_additions,
            "preamble": resolved.inherited_workspace.preamble,
        },
        "workspace_overrides": list(inputs.workspace_overrides),
        "branch": inputs.branch,
        "steps": [
            {
                "step": row.step,
                "kind": row.kind,
                "session": row.session,
                "conditional": row.conditional,
                "declared_role": row.declared_role,
                # `None` for a command, decision, or gate: no binding, and no
                # override controls.
                "binding": binding_payload(row.binding) if row.binding else None,
                # Kept flat as well, because every existing consumer of this
                # payload reads the effective pair straight off the row.
                "role": row.role,
                "model": row.model,
                "thinking": row.thinking,
            }
            for row in resolved.rows
        ],
    }
