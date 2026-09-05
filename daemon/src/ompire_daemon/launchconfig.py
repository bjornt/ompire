"""Upgrade reconciliation: what the operator still has to decide after the
template retirement, and how those decisions are recorded.

Two independent flows live here.

*Projects* carry launch configuration that used to live on templates. Where
every template of a project agreed, the migration copied the value across and
there is nothing to decide. Where they disagreed — or where a template pinned
a model, or the retired `judge_model` key is still set — the project is marked
`needs-reconciliation` and cannot launch until the operator picks. What they
are shown is every distinct candidate with the template it came from; what
they pick is a final value, not "the first one".

*Tasks* created before pinned inputs existed have no accepted model, base
branch, or preamble, and nothing can recover them: the original spawn's
overrides were never persisted. Their workspace, branch, sessions, workflow
history, review history, and PR facts are all intact and stay untouched. What
is missing is stated as missing, and the operator confirms a configuration for
what happens *next* — which is not a claim about what already happened.

Startup runs `initialize` before task classification and recovery, so a
blocked task is blocked before anything tries to resume it.

ADR-0026 (docs/adr/0026-resolve-launch-inputs-once-and-pin-them-to-the-task.md)
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, Engine

from ompire_daemon.config import Config
from ompire_daemon.db import launch_migration_evidence
from ompire_daemon.db import projects as projects_table
from ompire_daemon.execution_inputs import (
    PROFILE_SOURCE_LEGACY,
    PROVENANCE_LEGACY_CONFIRMED,
    TaskExecutionInputs,
    WorkspaceInputs,
    encode_execution_inputs,
    execution_inputs_payload,
)
from ompire_daemon.launch import LaunchInputError, _read_profile_roles
from ompire_daemon.model_config import JUDGE_ROLE
from ompire_daemon.registry.launch import (
    DECISION_JUDGE_MODEL,
    DECISION_LAUNCH_CONFIG,
    DECISION_NEW_DEFAULTS,
    KIND_MODEL_CANDIDATES,
    KIND_NEW_DEFAULTS,
    KIND_RETIRED_JUDGE_MODEL,
    KIND_TEMPLATE,
    KIND_WORKSPACE_CONFLICT,
    SCOPE_DAEMON,
    SCOPE_PROJECT,
    SCOPE_TASK,
    Evidence,
    evidence_fingerprint,
    get_decision,
    list_evidence,
    list_evidence_conn,
    record_decision,
    record_evidence,
)
from ompire_daemon.registry.model_profiles import reserved_write
from ompire_daemon.registry.projects import (
    Project,
    ProjectNotFoundError,
    get_project,
    validate_branch_pattern,
    validate_workshop_additions,
)
from ompire_daemon.registry.tasks import (
    Task,
    TaskNotFoundError,
    get_task,
    list_unconfigured_tasks,
    pin_execution_inputs,
)
from ompire_daemon.workflows import agent_step_roles, get_workflow

logger = logging.getLogger(__name__)

RETIRED_JUDGE_KEY = "judge_model"


class ReconciliationConflictError(Exception):
    """The submitted decision was made against evidence that has since
    changed, or against a project that no longer needs one. Nothing is
    written; the operator re-reads and decides again."""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# --- startup ------------------------------------------------------------------


def initialize(engine: Engine, config: Config) -> None:
    """Finish what a migration could not, idempotently.

    Two things need the daemon's configuration rather than only the database:
    the branch pattern a zero-template project should start from, and the
    retired `judge_model` value, which lives in `config.toml`. Both are
    applied here, before any task is classified or recovered.
    """
    _seed_new_defaults(engine, config)
    _capture_retired_judge_model(engine, config)


def _seed_new_defaults(engine: Engine, config: Config) -> None:
    """Give projects that had no templates the daemon's ordinary defaults.

    Recorded as a decision so it happens once: this is a *new* value, not
    recovered history, and re-applying it on every restart would quietly
    overwrite whatever the operator has since chosen.
    """
    with reserved_write(engine) as conn:
        for evidence in _all_evidence(conn, KIND_NEW_DEFAULTS):
            name = evidence.scope
            if get_decision(conn, SCOPE_PROJECT, name, DECISION_NEW_DEFAULTS):
                continue
            conn.execute(
                projects_table.update()
                .where(projects_table.c.name == name)
                .values(branch_pattern=config.default_branch_pattern)
            )
            record_decision(
                conn,
                scope_kind=SCOPE_PROJECT,
                scope=name,
                kind=DECISION_NEW_DEFAULTS,
                acknowledged_value=config.default_branch_pattern,
            )


def _capture_retired_judge_model(engine: Engine, config: Config) -> None:
    """Record an explicitly configured, now-retired `judge_model`.

    The daemon never rewrites `config.toml`. It records what it found, and
    every project that carries legacy template evidence has to acknowledge
    that this model is replaced by its profile's `slow` binding before it can
    launch again. An unchanged value that has already been acknowledged stays
    quiet across restarts; a *changed* one is new evidence and reopens the
    acknowledgement rather than being applied to anything.
    """
    value = config.retired.get(RETIRED_JUDGE_KEY)
    if value is None:
        return
    recorded = str(value)
    with reserved_write(engine) as conn:
        existing = [
            row
            for row in _all_evidence(conn, KIND_RETIRED_JUDGE_MODEL)
            if row.payload.get("value") == recorded
        ]
        if not existing:
            record_evidence(
                conn,
                kind=KIND_RETIRED_JUDGE_MODEL,
                scope_kind=SCOPE_DAEMON,
                scope="",
                source="config.toml",
                payload={"key": RETIRED_JUDGE_KEY, "value": recorded},
            )
            logger.warning(
                "config key %r is retired: the workflow judge now runs on the "
                "task profile's %r binding. The value %r configures nothing; "
                "acknowledge it per project to clear the launch block.",
                RETIRED_JUDGE_KEY,
                JUDGE_ROLE,
                recorded,
            )
        for name in _projects_with_legacy_templates(conn):
            decision = get_decision(conn, SCOPE_PROJECT, name, DECISION_JUDGE_MODEL)
            if decision is not None and decision.acknowledged_value == recorded:
                continue
            conn.execute(
                projects_table.update()
                .where(projects_table.c.name == name)
                .values(launch_config_state="needs-reconciliation")
            )


def _all_evidence(conn: Connection, kind: str) -> list[Evidence]:
    """Every evidence row of one kind, across scopes."""
    rows = conn.execute(
        launch_migration_evidence.select()
        .where(launch_migration_evidence.c.kind == kind)
        .order_by(launch_migration_evidence.c.id)
    ).all()
    return [
        Evidence(
            id=row.id,
            kind=row.kind,
            scope_kind=row.scope_kind,
            scope=row.scope,
            source=row.source,
            payload=json.loads(row.payload_json),
            recorded_at=row.recorded_at,
        )
        for row in rows
    ]


def _projects_with_legacy_templates(conn: Connection) -> list[str]:
    return sorted({row.scope for row in _all_evidence(conn, KIND_TEMPLATE)})


# --- project reconciliation ---------------------------------------------------


def _pending_judge_value(engine: Engine, name: str) -> str | None:
    """The retired judge value this project still has to acknowledge, if any."""
    daemon_evidence = list_evidence(engine, SCOPE_DAEMON, "", kind=KIND_RETIRED_JUDGE_MODEL)
    if not daemon_evidence:
        return None
    current = str(daemon_evidence[-1].payload.get("value"))
    with engine.connect() as conn:
        if not any(
            row.scope == name for row in _all_evidence(conn, KIND_TEMPLATE)
        ):
            return None
        decision = get_decision(conn, SCOPE_PROJECT, name, DECISION_JUDGE_MODEL)
    if decision is not None and decision.acknowledged_value == current:
        return None
    return current


def project_reconciliation(engine: Engine, name: str) -> dict[str, Any]:
    """Everything the operator needs to decide this project's configuration.

    Candidates are presented with their source and never pre-selected: the
    editor shows what each template said, and the operator supplies the final
    value. A legacy `model`/`thinking` pair is shown as what it is — one old
    concrete choice — not as a partially filled profile.
    """
    project = get_project(engine, name)
    evidence = list_evidence(engine, SCOPE_PROJECT, name)
    conflicts: dict[str, list[Any]] = {}
    model_candidates: list[dict[str, Any]] = []
    templates: list[dict[str, Any]] = []
    for row in evidence:
        if row.kind == KIND_WORKSPACE_CONFLICT:
            for field, values in row.payload.items():
                conflicts.setdefault(field, [])
                for value in values:
                    if value not in conflicts[field]:
                        conflicts[field].append(value)
        elif row.kind == KIND_MODEL_CANDIDATES:
            for candidate in row.payload:
                if candidate not in model_candidates:
                    model_candidates.append(candidate)
        elif row.kind == KIND_TEMPLATE:
            templates.append({"source": row.source, "values": row.payload})
    judge_value = _pending_judge_value(engine, name)
    return {
        "project_name": name,
        "state": project.launch_config_state,
        "needs_reconciliation": project.launch_config_state != "reconciled",
        "evidence_fingerprint": evidence_fingerprint(evidence),
        "current": {
            "base_branch": project.base_branch,
            "branch_pattern": project.branch_pattern,
            "workshop_additions": project.workshop_additions,
            "preamble": project.preamble,
            "default_model_profile": project.default_model_profile,
        },
        # Distinct old values per field, each still attributable to a source.
        "workspace_conflicts": conflicts,
        # Old concrete model/thinking pairs. These are candidates for *one*
        # role binding at most; they are never turned into a profile.
        "model_candidates": model_candidates,
        "retired_judge_model": judge_value,
        "judge_role": JUDGE_ROLE,
        # Inert history, kept after reconciliation so an unselected preamble
        # or candidate is not lost.
        "source_templates": templates,
    }


@dataclass(frozen=True)
class ProjectDecision:
    evidence_fingerprint: str
    base_branch: str
    branch_pattern: str
    workshop_additions: str
    preamble: str
    # Explicit: a name selects that profile as the project default, `None`
    # accepts having none and choosing one at each launch. There is no
    # "leave it" — that is the whole point of asking.
    default_model_profile: str | None
    acknowledge_model_candidates: bool
    acknowledge_judge_model: bool


def confirm_project_reconciliation(
    engine: Engine, name: str, decision: ProjectDecision
) -> Project:
    """Record the operator's decision and unblock the project, atomically.

    The whole result is validated before anything is written, the evidence
    fingerprint is compared inside the reservation, and the state change plus
    every acknowledgement land in one transaction — so a restart in the middle
    leaves the project either fully blocked or fully decided, never half.
    """
    validate_branch_pattern(decision.branch_pattern)
    validate_workshop_additions(decision.workshop_additions)
    if not decision.base_branch.strip():
        raise LaunchInputError("base_branch", "base branch must not be empty")

    pending_judge = _pending_judge_value(engine, name)
    with reserved_write(engine) as conn:
        row = conn.execute(
            projects_table.select().where(projects_table.c.name == name)
        ).first()
        if row is None:
            raise ProjectNotFoundError(name)
        evidence = list_evidence_conn(conn, SCOPE_PROJECT, name)
        if evidence_fingerprint(evidence) != decision.evidence_fingerprint:
            raise ReconciliationConflictError(
                "the migration evidence changed since it was read; "
                "re-read the reconciliation and decide again"
            )
        has_model_candidates = any(
            item.kind == KIND_MODEL_CANDIDATES for item in evidence
        )
        if has_model_candidates and not decision.acknowledge_model_candidates:
            raise LaunchInputError(
                "acknowledge_model_candidates",
                "acknowledge that the old model/thinking choices are replaced "
                "by the selected model profile",
            )
        if pending_judge is not None and not decision.acknowledge_judge_model:
            raise LaunchInputError(
                "acknowledge_judge_model",
                f"acknowledge that the retired judge model {pending_judge!r} is "
                f"replaced by the profile's {JUDGE_ROLE!r} binding",
            )
        if decision.default_model_profile is not None:
            # Reuses the normal profile lookup: reconciliation never bypasses
            # the four-role validator or invents a partial profile.
            _read_profile_roles(
                conn, decision.default_model_profile, field="default_model_profile"
            )
        conn.execute(
            projects_table.update()
            .where(projects_table.c.name == name)
            .values(
                base_branch=decision.base_branch,
                branch_pattern=decision.branch_pattern,
                workshop_additions=decision.workshop_additions,
                preamble=decision.preamble,
                default_model_profile=decision.default_model_profile,
                launch_config_state="reconciled",
            )
        )
        record_decision(
            conn,
            scope_kind=SCOPE_PROJECT,
            scope=name,
            kind=DECISION_LAUNCH_CONFIG,
            acknowledged_value=decision.evidence_fingerprint,
        )
        if pending_judge is not None:
            record_decision(
                conn,
                scope_kind=SCOPE_PROJECT,
                scope=name,
                kind=DECISION_JUDGE_MODEL,
                acknowledged_value=pending_judge,
            )
    return get_project(engine, name)


# --- legacy task confirmation -------------------------------------------------

# Inputs a task created before ADR-0026 simply does not have. They are listed
# as unknown on the task and carried into the confirmed document, so the
# record never claims the confirmed values were the ones already used.
LEGACY_UNKNOWN_INPUTS = (
    "model_profile",
    "thinking",
    "preamble",
    "workspace_overrides",
)


def task_configuration(engine: Engine, task_id: int) -> dict[str, Any]:
    """What is known, what was reconstructed, and what is simply unknown."""
    task = get_task(engine, task_id)
    evidence = list_evidence(engine, SCOPE_TASK, str(task_id))
    project: Project | None
    try:
        project = get_project(engine, task.project_name)
    except ProjectNotFoundError:
        project = None
    payload: dict[str, Any] = {
        "task_id": task_id,
        "needs_configuration": task.execution_inputs is None,
        "archived": task.state == "archived",
        # Facts the registry actually holds. These are never recreated or
        # discarded by a confirmation.
        "known": {
            "project_name": task.project_name,
            "slug": task.slug,
            "branch": task.branch,
            "clone_path": task.clone_path,
            "workflow_name": task.workflow_name,
            "workflow_status": task.workflow_status,
            "workflow_step": task.workflow_step,
            "pr_url": task.pr_url,
            "state": task.state,
        },
        # Where the task came from, as recorded at migration time. A template
        # name, not its contents: what that template says today is not
        # evidence of what this task ran under.
        "source_attribution": [
            {"source": row.source, "values": row.payload} for row in evidence
        ],
        "unknown_inputs": list(LEGACY_UNKNOWN_INPUTS),
        "candidates": {
            "checkout_path": project.checkout_path if project else None,
            "fetch_remote": project.fetch_remote if project else None,
            "upstream_url": project.upstream_url if project else None,
            "fork_url": project.fork_url if project else None,
            "base_branch": project.base_branch if project else None,
            "preamble": project.preamble if project else None,
            "workshop_additions": project.workshop_additions if project else None,
            "default_model_profile": project.default_model_profile if project else None,
        },
    }
    if task.execution_inputs is not None:
        payload["accepted"] = execution_inputs_payload(task.execution_inputs)
    return payload


@dataclass(frozen=True)
class TaskContinuation:
    """What the operator supplies to pin a legacy task's *future* behavior."""

    model_profile: str
    base_branch: str
    workshop_additions: str
    preamble: str


def _legacy_inputs(
    conn: Connection, task: Task, project: Project, continuation: TaskContinuation
) -> TaskExecutionInputs:
    roles = _read_profile_roles(conn, continuation.model_profile, field="model_profile")
    workflow = get_workflow(task.workflow_name)
    return TaskExecutionInputs(
        provenance=PROVENANCE_LEGACY_CONFIRMED,
        accepted_at=_now_iso(),
        project_name=task.project_name,
        workflow_name=task.workflow_name,
        model_profile_name=continuation.model_profile,
        model_profile_source=PROFILE_SOURCE_LEGACY,
        roles=roles,
        step_roles=agent_step_roles(workflow),
        judge_role=JUDGE_ROLE,
        workspace=WorkspaceInputs(
            base_branch=continuation.base_branch,
            # The branch already exists on this task; the pattern that made it
            # is history nobody recorded. Storing the rendered branch as the
            # pattern would be a lie, so it is stated as the literal name.
            branch_pattern=task.branch,
            workshop_additions=continuation.workshop_additions,
            preamble=continuation.preamble,
        ),
        workspace_overrides=(),
        branch=task.branch,
        checkout_path=project.checkout_path,
        fetch_remote=project.fetch_remote,
        upstream_url=project.upstream_url,
        fork_url=project.fork_url,
        unknown_inputs=LEGACY_UNKNOWN_INPUTS,
    )


def preview_task_configuration(
    engine: Engine, task_id: int, continuation: TaskContinuation
) -> dict[str, Any]:
    """Resolve a continuation configuration without writing it.

    Task identity, branch, clone, workflow, and session ids are fixed facts
    here — they are shown, never chosen. The current project's routing is
    offered as a candidate, because it is the only routing that exists, and
    confirming it is the operator saying so rather than the daemon assuming.
    """
    task = get_task(engine, task_id)
    if task.execution_inputs is not None:
        raise ReconciliationConflictError(
            f"task {task_id} already has pinned execution inputs"
        )
    validate_workshop_additions(continuation.workshop_additions)
    if not continuation.base_branch.strip():
        raise LaunchInputError("base_branch", "base branch must not be empty")
    project = get_project(engine, task.project_name)
    with engine.connect() as conn:
        inputs = _legacy_inputs(conn, task, project, continuation)
    payload = execution_inputs_payload(inputs)
    return {
        "task_id": task_id,
        "preview_token": _continuation_token(inputs),
        "inputs": payload,
        "unknown_inputs": list(LEGACY_UNKNOWN_INPUTS),
    }


def _continuation_token(inputs: TaskExecutionInputs) -> str:
    document = json.loads(encode_execution_inputs(inputs))
    # The timestamp is what makes two otherwise identical previews differ, and
    # it is not part of what the operator reviewed.
    document.pop("accepted_at", None)
    return hashlib.sha256(
        json.dumps(document, sort_keys=True).encode("utf-8")
    ).hexdigest()[:32]


def confirm_task_configuration(
    engine: Engine,
    task_id: int,
    continuation: TaskContinuation,
    *,
    preview_token: str,
    acknowledge_unknown: bool,
) -> Task:
    """Pin a legacy task's continuation configuration, once.

    This changes what happens from here on. It does not respawn the workspace,
    replay a privileged operation, or alter the recorded branch, session
    identities, workflow history, review history, or PR facts — and it does
    not claim the turns already taken used these values.
    """
    if not acknowledge_unknown:
        raise LaunchInputError(
            "acknowledge_unknown",
            "acknowledge that the original model, thinking level, preamble, and "
            "overrides for this task are unknown and cannot be recovered",
        )
    task = get_task(engine, task_id)
    if task.execution_inputs is not None:
        raise ReconciliationConflictError(
            f"task {task_id} already has pinned execution inputs"
        )
    project = get_project(engine, task.project_name)
    validate_workshop_additions(continuation.workshop_additions)
    with engine.connect() as conn:
        inputs = _legacy_inputs(conn, task, project, continuation)
    if _continuation_token(inputs) != preview_token:
        raise ReconciliationConflictError(
            "the continuation configuration changed since it was previewed; "
            "review it again before confirming"
        )
    return pin_execution_inputs(engine, task_id, inputs)


def unconfigured_task_ids(engine: Engine) -> list[int]:
    return [task.id for task in list_unconfigured_tasks(engine)]


__all__ = [
    "LEGACY_UNKNOWN_INPUTS",
    "ProjectDecision",
    "ReconciliationConflictError",
    "TaskContinuation",
    "TaskNotFoundError",
    "confirm_project_reconciliation",
    "confirm_task_configuration",
    "initialize",
    "preview_task_configuration",
    "project_reconciliation",
    "task_configuration",
    "unconfigured_task_ids",
]
