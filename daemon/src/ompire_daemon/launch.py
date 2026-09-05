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
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection

from ompire_daemon.db import model_profiles as model_profiles_table
from ompire_daemon.db import projects as projects_table
from ompire_daemon.execution_inputs import (
    PROFILE_SOURCE_PROJECT,
    PROFILE_SOURCE_TASK,
    PROVENANCE_ACCEPTED,
    WORKSPACE_FIELDS,
    TaskExecutionInputs,
    WorkspaceInputs,
)
from ompire_daemon.model_config import JUDGE_ROLE, MODEL_ROLES
from ompire_daemon.registry.model_profiles import RoleBinding
from ompire_daemon.registry.projects import (
    validate_branch_pattern,
    validate_workshop_additions,
)
from ompire_daemon.workflows import (
    JUDGE_SESSION,
    UnknownWorkflowNameError,
    agent_step_roles,
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
class LaunchRequest:
    """Normalized submitted selections.

    `model_profile` is three-valued through `profile_explicit`: an explicit
    task profile, or inheritance from the project. `workspace_overrides`
    carries only the fields the operator actually overrode — an absent key
    means inherit, and an empty `preamble` string is an override to "no
    preamble", not an absence.
    """

    project_name: str
    workflow_name: str
    slug: str
    prompt: str
    model_profile: str | None
    profile_explicit: bool
    workspace_overrides: Mapping[str, str]


@dataclass(frozen=True)
class PreviewRow:
    """One model-consuming (or deliberately model-free) row of the preview."""

    step: str
    kind: str
    session: str | None
    role: str | None
    model: str | None
    # The accepted thinking *policy*, spelled as the profile spells it. omp
    # may resolve `auto`/`max` to a model-specific level at run time; that
    # resolved state is reported separately, from the running process.
    thinking: str | None
    conditional: bool


@dataclass(frozen=True)
class ResolvedLaunch:
    inputs: TaskExecutionInputs
    rows: tuple[PreviewRow, ...]
    fingerprint: str
    # Display facts the form shows beside the rows.
    profile_source: str
    project_default_profile: str | None
    inherited_workspace: WorkspaceInputs


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
    decoded = json.loads(row.roles_json)
    return {
        role: RoleBinding(
            model=decoded[role]["model"], thinking=decoded[role]["thinking"]
        )
        for role in MODEL_ROLES
    }


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
    for field in WORKSPACE_FIELDS:
        if field in overrides:
            values[field] = overrides[field]
            applied.append(field)
        else:
            values[field] = getattr(project_row, field)
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


def _preview_rows(workflow_name: str, roles: Mapping[str, RoleBinding]) -> tuple[PreviewRow, ...]:
    descriptor = describe_workflow(get_workflow(workflow_name))
    rows = [
        PreviewRow(
            step=step.name,
            kind=step.kind,
            session=step.session,
            role=step.role,
            # A command, decision, or gate has no model. Showing one would be
            # a fiction — those steps never reach a provider.
            model=roles[step.role].model if step.role else None,
            thinking=roles[step.role].thinking if step.role else None,
            conditional=step.conditional,
        )
        for step in descriptor.steps
    ]
    rows.append(
        PreviewRow(
            step=descriptor.judge_session,
            kind="judge",
            session=descriptor.judge_session,
            role=descriptor.judge_role,
            model=roles[descriptor.judge_role].model,
            thinking=roles[descriptor.judge_role].thinking,
            # The judge runs only when a deterministic route or outcome fails.
            conditional=True,
        )
    )
    return tuple(rows)


def launch_fingerprint(
    request: LaunchRequest, inputs: TaskExecutionInputs, rows: tuple[PreviewRow, ...]
) -> str:
    """A deterministic value over the submitted selections, everything they
    resolved to, and the workflow descriptor they were resolved against.

    Deliberately *not* a global settings timestamp: editing an unrelated
    profile, renaming another project, or any write that does not change this
    launch must leave a reviewed preview valid.
    """
    digest = hashlib.sha256()
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
                },
                "resolved": {
                    "profile": inputs.model_profile_name,
                    "profile_source": inputs.model_profile_source,
                    "roles": {
                        role: [inputs.roles[role].model, inputs.roles[role].thinking]
                        for role in MODEL_ROLES
                    },
                    "step_roles": dict(sorted(inputs.step_roles.items())),
                    "judge_role": inputs.judge_role,
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
                    [row.step, row.kind, row.session, row.role, row.conditional]
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

    if request.profile_explicit and request.model_profile is not None:
        profile_name = request.model_profile
        profile_source = PROFILE_SOURCE_TASK
        roles = _read_profile_roles(conn, profile_name, field="model_profile")
    elif project_row.default_model_profile is not None:
        profile_name = project_row.default_model_profile
        profile_source = PROFILE_SOURCE_PROJECT
        roles = _read_profile_roles(conn, profile_name, field="project_name")
    else:
        raise LaunchInputError(
            "model_profile",
            f"project {project_row.name!r} has no default model profile; "
            "select a model profile for this task",
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
        roles=roles,
        step_roles=agent_step_roles(workflow),
        judge_role=JUDGE_ROLE,
        workspace=workspace,
        workspace_overrides=applied,
        branch=branch,
        checkout_path=project_row.checkout_path,
        fetch_remote=project_row.fetch_remote,
        upstream_url=project_row.upstream_url,
        fork_url=project_row.fork_url,
    )
    rows = _preview_rows(workflow.name, roles)
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
    )


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
        "judge_role": inputs.judge_role,
        "roles": {
            role: {
                "model": inputs.roles[role].model,
                "thinking": inputs.roles[role].thinking,
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
                "role": row.role,
                "model": row.model,
                "thinking": row.thinking,
                "conditional": row.conditional,
            }
            for row in resolved.rows
        ],
    }
