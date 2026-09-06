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

The same is true, one layer up, of the *workflow definition* (ADR-0028). Every
task that existed before definitions were retained recorded a workflow name,
and a name is not a procedure: what those prompts and routes actually said is
gone. So no such task is assigned today's definition behind the operator's
back. It is offered the current definition of its own workflow name as a
*candidate*, with the compatibility check that says whether that candidate can
even explain the steps and sessions already on record, and it executes nothing
further until a person confirms it. The confirmation records exactly what was
confirmed and where the honest boundary lies: everything through
`legacy_through_seq` happened under a definition nobody kept, and one attempt
may span the boundary, having been opened before the confirmation and finished
after it.

A task that needs both decisions gets one confirmation, not two: they are the
same question — "what does this task continue under" — asked about different
parts of the same launch.

Startup runs `initialize` before task classification and recovery, so a
blocked task is blocked before anything tries to resume it.

ADR-0026 (docs/adr/0026-resolve-launch-inputs-once-and-pin-them-to-the-task.md)
ADR-0028 (docs/adr/0028-retain-declarative-workflow-revisions.md)
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
    RETIRED_AUXILIARY_JUDGE,
    ROLE_SOURCE_WORKFLOW,
    WORKFLOW_SOURCE_LEGACY_CONFIRMED,
    ConsumerBinding,
    MissingConsumerBindingError,
    ModelPolicy,
    TaskExecutionInputs,
    WorkflowBinding,
    WorkspaceInputs,
    encode_execution_inputs,
    execution_inputs_payload,
)
from ompire_daemon.launch import LaunchInputError, _read_profile_roles
from ompire_daemon.registry.launch import (
    DECISION_JUDGE_MODEL,
    DECISION_LAUNCH_CONFIG,
    DECISION_NEW_DEFAULTS,
    DECISION_WORKFLOW_CONTINUATION,
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
from ompire_daemon.registry.sessions import (
    APPLIED_ORIGIN_MIGRATED,
    build_applied_policy,
    list_resumable_sessions,
    record_applied_policy,
)
from ompire_daemon.registry.tasks import (
    Task,
    TaskNotFoundError,
    get_task,
    list_unconfigured_tasks,
    pin_execution_inputs,
    pin_workflow_binding,
)
from ompire_daemon.registry.workflows import (
    PAUSE_UNRESOLVED_DECISION,
    build_pause,
    list_step_records,
    pause_step,
)
from ompire_daemon.taskdefinition import (
    READINESS_NEEDS_WORKFLOW_CONFIRMATION,
    workflow_readiness,
)
from ompire_daemon.workflow_definitions import (
    RESULT_ENVELOPE_VERSION,
    DecisionStep,
    WorkflowRevision,
    describe,
)
from ompire_daemon.workflows import UnknownWorkflowNameError, current_revision

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
    every project that carries legacy template evidence has to acknowledge the
    setting before it can launch again. The acknowledgement history itself is
    preserved across the judge's removal: what changed is what the operator is
    acknowledging. The value configured no model even before — and now there
    is no engine-reserved model consumer at all, so it configures nothing that
    exists. An unchanged value that has already been acknowledged stays quiet
    across restarts; a *changed* one is new evidence and reopens the
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
                "config key %r is retired: the workflow engine no longer runs "
                "an implicit judge at all, and unresolved evidence pauses for "
                "the operator instead. The value %r configures nothing; "
                "acknowledge it per project to clear the launch block.",
                RETIRED_JUDGE_KEY,
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
        # No role replaces it. The engine has no reserved model consumer, so
        # there is nothing to point the operator at as "where it went".
        "judge_removed": True,
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
                f"acknowledge that the retired judge model {pending_judge!r} "
                "configures nothing: the workflow engine no longer runs an "
                "implicit judge, and unresolved evidence pauses for you",
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


# --- legacy task continuation -------------------------------------------------

# Inputs a task created before ADR-0026 simply does not have. They are listed
# as unknown on the task and carried into the confirmed document, so the
# record never claims the confirmed values were the ones already used.
LEGACY_UNKNOWN_INPUTS = (
    "model_profile",
    "thinking",
    "preamble",
    "workspace_overrides",
)

# What a task upgraded across ADR-0028 does not have: the definition itself.
LEGACY_UNKNOWN_WORKFLOW = ("workflow_definition",)

# The one uncertainty-policy change a confirming operator is told about, in
# their own terms. Kept beside the confirmation because it is the only way the
# task's *future* behavior differs from its past for reasons unrelated to the
# candidate definition's own content.
UNCERTAINTY_NOTICE = (
    "This task will no longer ask a model to classify a result it cannot read. "
    "When a step produces no valid outcome, or a route cannot be decided from "
    "the recorded evidence, the run stops and waits for you with the reason "
    "attached, and you decide whether to retry the step."
)


def _candidate_revision(task: Task) -> WorkflowRevision | None:
    """The current definition of *this task's own* workflow name, or None.

    Only that one name is offered. There is no revision picker and no way to
    switch a task to a different workflow: the task's history was produced by
    something calling itself `bugfix`, and offering anything else would be
    inviting the operator to relabel a run rather than continue it.
    """
    try:
        return current_revision(task.workflow_name)
    except UnknownWorkflowNameError:
        return None


def _legacy_gate_pair(records: list[Any], index: int) -> bool:
    """The one known pre-ADR-0028 record shape a `decision` step can explain.

    The old engine escalated an unresolvable decision by finishing the
    decision record `ok` with the escalation message in its error field, then
    appending a *separate* `gate` record under the same name. That gate is not
    a declared gate, so a strict kind check would call this history
    incompatible. It is admitted only in exactly that shape — immediately
    preceded by a same-name decision record with no outcome and a recorded
    message — and never as a general "kinds may differ".
    """
    if index == 0:
        return False
    previous = records[index - 1]
    current = records[index]
    return (
        previous.step == current.step
        and previous.kind == "decision"
        and previous.outcome is None
        and bool(previous.error)
    )


def _compatibility_problems(
    engine: Engine, task: Task, revision: WorkflowRevision
) -> list[str]:
    """Whether this candidate can explain what is already on record.

    Confirmation is allowed only when the answer is "yes, entirely". A
    definition that cannot account for a step name, a step's kind, a session,
    or the run's current position is not a continuation of this task — it is a
    different procedure being pointed at somebody else's history.
    """
    definition = revision.definition
    problems: list[str] = []
    records = list_step_records(engine, task.id)
    if definition.format >= 2:
        # A format-2 definition reads results by declared name; this history
        # was written under the generic success/failed envelope. Even where
        # every step name still matched, the recorded outcomes would mean
        # something the candidate cannot express, so there is no honest
        # continuation — and no automatic upgrade is offered in its place.
        legacy_outcomes = [
            record.seq
            for record in records
            if record.kind == "agent"
            and record.outcome is not None
            and record.outcome.get("version") != RESULT_ENVELOPE_VERSION
        ]
        if legacy_outcomes:
            listed = ", ".join(f"#{seq}" for seq in legacy_outcomes)
            problems.append(
                f"the current {revision.name!r} definition is workflow format "
                f"{definition.format}, which reads results by declared name, "
                f"but this task's attempts ({listed}) recorded results under "
                "the older success/failed envelope; those cannot be "
                "reinterpreted under the new contract"
            )
    for index, record in enumerate(records):
        step = definition.step_named(record.step)
        if step is None:
            problems.append(
                f"attempt #{record.seq} ran a step {record.step!r} that the "
                f"current {revision.name!r} definition does not declare"
            )
            continue
        if step.kind != record.kind and not (
            record.kind == "gate"
            and isinstance(step, DecisionStep)
            and _legacy_gate_pair(records, index)
        ):
            problems.append(
                f"attempt #{record.seq} recorded step {record.step!r} as a "
                f"{record.kind}, but the current definition declares it as a "
                f"{step.kind}"
            )
        if record.session is not None and record.session not in definition.sessions:
            problems.append(
                f"attempt #{record.seq} ran in session {record.session!r}, "
                "which the current definition does not declare"
            )
    for session in list_resumable_sessions(engine, task.id):
        if session.name == RETIRED_AUXILIARY_JUDGE:
            # The retired engine session. It is not declared by any definition
            # and is deliberately not resumed; its transcript stays readable.
            continue
        if session.name not in definition.sessions:
            problems.append(
                f"session {session.name!r} exists on this task but the current "
                "definition does not declare it"
            )
    if task.workflow_step is not None and definition.step_named(task.workflow_step) is None:
        problems.append(
            f"the run is currently at step {task.workflow_step!r}, which the "
            "current definition does not declare"
        )
    inputs = task.execution_inputs
    if inputs is not None:
        # A task that already has accepted inputs cannot be given new model
        # policy here: that would be a launch decision, made for turns that
        # may already be under way. Missing bindings block instead.
        for step in definition.agent_steps():
            try:
                inputs.binding_for_step(step.name)
            except MissingConsumerBindingError:
                problems.append(
                    f"step {step.name!r} needs an accepted model binding and "
                    "this task has none; it cannot be added by a continuation"
                )
    return problems


def _history_boundary(engine: Engine, task: Task) -> tuple[int, int | None]:
    """Where the retained definition's authority starts.

    Everything up to `legacy_through_seq` was produced by a definition nobody
    kept. An attempt still open at confirmation time is named separately: it
    began under the old definition and will finish under the confirmed one, and
    calling it either would be a claim about a turn whose beginning is unknown.
    """
    records = list_step_records(engine, task.id)
    if not records:
        return 0, None
    last = records[-1]
    interrupted = last.seq if last.status in ("running", "waiting") else None
    return last.seq, interrupted


def workflow_continuation(engine: Engine, task: Task) -> dict[str, Any] | None:
    """The candidate this task would continue under, and whether it fits.

    None when the task already has a pinned revision: there is nothing to
    decide, and offering a choice would imply the accepted one is negotiable.
    """
    readiness = workflow_readiness(engine, task)
    if readiness.reason != READINESS_NEEDS_WORKFLOW_CONFIRMATION and (
        task.execution_inputs is not None
    ):
        return None
    if task.execution_inputs is not None and task.execution_inputs.workflow_binding:
        return None
    revision = _candidate_revision(task)
    legacy_through, interrupted = _history_boundary(engine, task)
    if revision is None:
        return {
            "workflow_name": task.workflow_name,
            "revision": None,
            "available": False,
            "compatible": False,
            "problems": [
                (
                    f"this daemon installs no workflow named "
                    f"{task.workflow_name!r}, so there is no candidate "
                    "definition to continue under"
                )
            ],
            "legacy_through_seq": legacy_through,
            "interrupted_legacy_seq": interrupted,
            "uncertainty_notice": UNCERTAINTY_NOTICE,
        }
    problems = _compatibility_problems(engine, task, revision)
    descriptor = describe(revision)
    return {
        "workflow_name": revision.name,
        "revision": revision.revision,
        "format": revision.format,
        "available": True,
        "compatible": not problems,
        "problems": problems,
        "primary_session": descriptor.primary_session,
        "sessions": list(descriptor.sessions),
        "steps": [
            {
                "name": step.name,
                "kind": step.kind,
                "session": step.session,
                "role": step.role,
                "conditional": step.conditional,
            }
            for step in descriptor.steps
        ],
        "current_step": task.workflow_step,
        "workflow_status": task.workflow_status,
        "legacy_through_seq": legacy_through,
        "interrupted_legacy_seq": interrupted,
        "uncertainty_notice": UNCERTAINTY_NOTICE,
    }


def task_configuration(engine: Engine, task_id: int) -> dict[str, Any]:
    """What is known, what was reconstructed, and what is simply unknown."""
    task = get_task(engine, task_id)
    evidence = list_evidence(engine, SCOPE_TASK, str(task_id))
    project: Project | None
    try:
        project = get_project(engine, task.project_name)
    except ProjectNotFoundError:
        project = None
    readiness = workflow_readiness(engine, task)
    continuation = workflow_continuation(engine, task)
    payload: dict[str, Any] = {
        "task_id": task_id,
        "needs_configuration": task.execution_inputs is None,
        "needs_workflow_confirmation": continuation is not None,
        # Distinguishable on purpose: "never configured", "no retained
        # definition", "the stored definition is damaged", and "this daemon
        # cannot read that format" call for different actions, and only the
        # second is something the operator can confirm away.
        "workflow_readiness": {
            "ready": readiness.ready,
            "reason": readiness.reason,
            "detail": readiness.detail,
            "confirmable": readiness.confirmable,
        },
        "workflow_candidate": continuation,
        "archived": task.state == "archived",
        # Facts the registry actually holds. These are never recreated or
        # discarded by a confirmation.
        "known": {
            "project_name": task.project_name,
            "slug": task.slug,
            "branch": task.branch,
            "clone_path": task.clone_path,
            "workflow_name": task.workflow_name,
            "workflow_revision": (
                task.execution_inputs.workflow_revision
                if task.execution_inputs is not None
                else None
            ),
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
        "unknown_inputs": list(
            LEGACY_UNKNOWN_INPUTS if task.execution_inputs is None else ()
        )
        + list(LEGACY_UNKNOWN_WORKFLOW if continuation is not None else ()),
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
    """What the operator supplies to pin a legacy task's *future* behavior.

    The launch fields are required only when the task has no accepted inputs
    at all. A task that was accepted normally and merely predates retained
    definitions supplies none of them: its model, branch, and preamble were
    reviewed once and are not re-decided here.
    """

    model_profile: str | None = None
    base_branch: str | None = None
    workshop_additions: str | None = None
    preamble: str | None = None


def _require_launch_fields(continuation: TaskContinuation) -> None:
    if not continuation.model_profile:
        raise LaunchInputError(
            "model_profile", "select a model profile for this task to continue under"
        )
    if not (continuation.base_branch or "").strip():
        raise LaunchInputError("base_branch", "base branch must not be empty")
    validate_workshop_additions(continuation.workshop_additions or "")


def _legacy_inputs(
    conn: Connection,
    task: Task,
    project: Project,
    continuation: TaskContinuation,
    revision: WorkflowRevision,
    *,
    legacy_through_seq: int,
    interrupted_legacy_seq: int | None,
) -> TaskExecutionInputs:
    assert continuation.model_profile is not None
    roles = _read_profile_roles(conn, continuation.model_profile, field="model_profile")
    now = _now_iso()

    def binding(role: str) -> ConsumerBinding:
        # Every consumer inherits the one confirmed profile and its declared
        # role. The legacy confirmation UI does not offer row overrides: the
        # operator is pinning what this task *continues* under, and inventing
        # per-step choices for a run already in progress would be a claim
        # about turns that already happened.
        return ConsumerBinding(
            profile_name=continuation.model_profile or "",
            profile_source=PROFILE_SOURCE_LEGACY,
            role=role,
            role_source=ROLE_SOURCE_WORKFLOW,
            roles=dict(roles),
        )

    return TaskExecutionInputs(
        provenance=PROVENANCE_LEGACY_CONFIRMED,
        accepted_at=now,
        project_name=task.project_name,
        workflow_name=revision.name,
        workflow_binding=WorkflowBinding(
            revision=revision.revision,
            source=WORKFLOW_SOURCE_LEGACY_CONFIRMED,
            bound_at=now,
            legacy_through_seq=legacy_through_seq,
            interrupted_legacy_seq=interrupted_legacy_seq,
        ),
        model_profile_name=continuation.model_profile,
        model_profile_source=PROFILE_SOURCE_LEGACY,
        step_bindings={
            step.name: binding(step.role) for step in revision.definition.agent_steps()
        },
        workspace=WorkspaceInputs(
            base_branch=continuation.base_branch or "",
            # The branch already exists on this task; the pattern that made it
            # is history nobody recorded. Storing the rendered branch as the
            # pattern would be a lie, so it is stated as the literal name.
            branch_pattern=task.branch,
            workshop_additions=continuation.workshop_additions or "",
            preamble=continuation.preamble or "",
        ),
        workspace_overrides=(),
        branch=task.branch,
        checkout_path=project.checkout_path,
        fetch_remote=project.fetch_remote,
        upstream_url=project.upstream_url,
        fork_url=project.fork_url,
        unknown_inputs=LEGACY_UNKNOWN_INPUTS,
    )


@dataclass(frozen=True)
class _Resolution:
    """One prospective continuation, before anything is written."""

    task: Task
    revision: WorkflowRevision
    inputs: TaskExecutionInputs | None  # None when only the binding is missing
    binding: WorkflowBinding
    problems: list[str]
    legacy_through_seq: int
    interrupted_legacy_seq: int | None


def _resolve_continuation(
    engine: Engine, task_id: int, continuation: TaskContinuation
) -> _Resolution:
    """Work out what confirming would pin, without writing any of it."""
    task = get_task(engine, task_id)
    readiness = workflow_readiness(engine, task)
    if readiness.ready:
        raise ReconciliationConflictError(
            f"task {task_id} already has a pinned workflow revision and "
            "accepted launch inputs"
        )
    if not readiness.confirmable and task.execution_inputs is not None:
        raise ReconciliationConflictError(
            f"task {task_id} cannot be confirmed: {readiness.detail}"
        )
    revision = _candidate_revision(task)
    if revision is None:
        raise ReconciliationConflictError(
            f"this daemon installs no workflow named {task.workflow_name!r}, so "
            f"task {task_id} has no candidate definition to continue under"
        )
    problems = _compatibility_problems(engine, task, revision)
    legacy_through, interrupted = _history_boundary(engine, task)
    now = _now_iso()
    binding = WorkflowBinding(
        revision=revision.revision,
        source=WORKFLOW_SOURCE_LEGACY_CONFIRMED,
        bound_at=now,
        legacy_through_seq=legacy_through,
        interrupted_legacy_seq=interrupted,
    )
    inputs: TaskExecutionInputs | None = None
    if task.execution_inputs is None:
        _require_launch_fields(continuation)
        project = get_project(engine, task.project_name)
        with engine.connect() as conn:
            inputs = _legacy_inputs(
                conn,
                task,
                project,
                continuation,
                revision,
                legacy_through_seq=legacy_through,
                interrupted_legacy_seq=interrupted,
            )
        binding = inputs.workflow_binding or binding
    return _Resolution(
        task=task,
        revision=revision,
        inputs=inputs,
        binding=binding,
        problems=problems,
        legacy_through_seq=legacy_through,
        interrupted_legacy_seq=interrupted,
    )


def _continuation_token(resolution: _Resolution) -> str:
    """Names exactly what the operator reviewed.

    Covers the candidate revision, the persisted inputs the confirmation would
    write, the run's position and history boundary, and the compatibility
    result — so a confirmation cannot be applied after the candidate changed,
    after the run moved on, or after a problem appeared that the operator
    never saw.
    """
    document: dict[str, Any] = {
        "revision": resolution.revision.revision,
        "legacy_through_seq": resolution.legacy_through_seq,
        "interrupted_legacy_seq": resolution.interrupted_legacy_seq,
        "workflow_status": resolution.task.workflow_status,
        "workflow_step": resolution.task.workflow_step,
        "problems": resolution.problems,
    }
    if resolution.inputs is not None:
        inputs_document = json.loads(encode_execution_inputs(resolution.inputs))
        # Timestamps are what make two otherwise identical previews differ,
        # and they are not part of what the operator reviewed.
        inputs_document.pop("accepted_at", None)
        if isinstance(inputs_document.get("workflow_binding"), dict):
            inputs_document["workflow_binding"].pop("bound_at", None)
        document["inputs"] = inputs_document
    return hashlib.sha256(
        json.dumps(document, sort_keys=True).encode("utf-8")
    ).hexdigest()[:32]


def preview_task_configuration(
    engine: Engine, task_id: int, continuation: TaskContinuation
) -> dict[str, Any]:
    """Resolve a continuation configuration without writing it.

    Task identity, branch, clone, workflow name, and session ids are fixed
    facts here — they are shown, never chosen. The current project's routing is
    offered as a candidate, because it is the only routing that exists, and
    confirming it is the operator saying so rather than the daemon assuming.
    """
    resolution = _resolve_continuation(engine, task_id, continuation)
    return {
        "task_id": task_id,
        "preview_token": _continuation_token(resolution),
        "inputs": (
            execution_inputs_payload(resolution.inputs)
            if resolution.inputs is not None
            else None
        ),
        "workflow": {
            "name": resolution.revision.name,
            "revision": resolution.revision.revision,
            "format": resolution.revision.format,
            "compatible": not resolution.problems,
            "problems": resolution.problems,
            "legacy_through_seq": resolution.legacy_through_seq,
            "interrupted_legacy_seq": resolution.interrupted_legacy_seq,
            "uncertainty_notice": UNCERTAINTY_NOTICE,
        },
        "unknown_inputs": list(
            LEGACY_UNKNOWN_INPUTS if resolution.inputs is not None else ()
        )
        + list(LEGACY_UNKNOWN_WORKFLOW),
    }


def confirm_task_configuration(
    engine: Engine,
    task_id: int,
    continuation: TaskContinuation,
    *,
    preview_token: str,
    acknowledge_unknown: bool,
    acknowledge_workflow: bool = False,
) -> Task:
    """Pin a legacy task's continuation configuration, once.

    This changes what happens from here on. It does not respawn the workspace,
    replay a privileged operation, start execution, or alter the recorded
    branch, session identities, workflow history, review history, or PR facts
    — and it does not claim the turns already taken used these values. The
    existing explicit Continue action is still what resumes the run, and it
    still applies the normal recovery checks.
    """
    resolution = _resolve_continuation(engine, task_id, continuation)
    # Staleness first. An acknowledgement of something the operator did not
    # actually review is not worth validating, and the fix is the same either
    # way: read the current continuation and decide again.
    if _continuation_token(resolution) != preview_token:
        raise ReconciliationConflictError(
            "the continuation changed since it was previewed; review it "
            "again before confirming"
        )
    if resolution.inputs is not None and not acknowledge_unknown:
        raise LaunchInputError(
            "acknowledge_unknown",
            "acknowledge that the original model, thinking level, preamble, and "
            "overrides for this task are unknown and cannot be recovered",
        )
    if not acknowledge_workflow:
        raise LaunchInputError(
            "acknowledge_workflow",
            "acknowledge that the exact workflow definition this task already "
            "ran was never recorded, and that the current definition governs "
            "only what happens next",
        )
    if resolution.problems:
        raise LaunchInputError(
            "workflow_candidate",
            "the current definition cannot explain this task's recorded steps "
            "and sessions: " + "; ".join(resolution.problems),
        )
    # Re-resolved under the reservation and compared, so a confirmation made
    # against a preview that has since changed is refused rather than applied.
    with reserved_write(engine) as conn:
        fresh = _resolve_continuation(engine, task_id, continuation)
        if _continuation_token(fresh) != preview_token:
            raise ReconciliationConflictError(
                "the continuation changed since it was previewed; review it "
                "again before confirming"
            )
        record_decision(
            conn,
            scope_kind=SCOPE_TASK,
            scope=str(task_id),
            kind=DECISION_WORKFLOW_CONTINUATION,
            acknowledged_value=json.dumps(
                {
                    "revision": fresh.binding.revision,
                    "legacy_through_seq": fresh.binding.legacy_through_seq,
                    "interrupted_legacy_seq": fresh.binding.interrupted_legacy_seq,
                },
                sort_keys=True,
            ),
        )
    if fresh.inputs is not None:
        pinned = pin_execution_inputs(engine, task_id, fresh.inputs)
        _seed_session_continuation(engine, task_id, fresh.inputs, fresh.revision)
    else:
        pinned = pin_workflow_binding(engine, task_id, fresh.binding)
    _convert_legacy_escalation_gate(engine, pinned, fresh.revision)
    return get_task(engine, task_id)


def _convert_legacy_escalation_gate(
    engine: Engine, task: Task, revision: WorkflowRevision
) -> None:
    """Re-label an old synthesized escalation gate as what it always was.

    The pre-ADR-0028 engine parked an unresolvable decision on a *synthesized*
    gate under the decision's own name, and resuming it fell through to the
    step after the decision — which is exactly the silent "continue as if the
    evidence had been accepted" this change removes. After confirmation, that
    waiting record becomes an uncertainty pause whose operator action retries
    the decision. Both records are preserved; only what the button does
    changes.
    """
    records = list_step_records(engine, task.id)
    if not records:
        return
    last = records[-1]
    if last.status != "waiting" or last.pause is not None or last.kind != "gate":
        return
    step = revision.definition.step_named(last.step)
    if not isinstance(step, DecisionStep):
        return
    if not _legacy_gate_pair(records, len(records) - 1):
        return
    message = (last.outcome or {}).get("message") or "the route could not be decided"
    pause_step(
        engine,
        task.id,
        last.seq,
        pause=build_pause(
            reason=PAUSE_UNRESOLVED_DECISION,
            message=(
                f"{message}\n\n"
                "This run stopped here before workflow definitions were "
                "retained, when an unresolved route fell through to the next "
                "step. Retrying now re-evaluates the "
                f"{last.step!r} decision against the recorded evidence; if "
                "that evidence still does not decide, it will stop here again."
            ),
            step=last.step,
            retry_step=last.step,
            retry_kind=step.kind,
        ),
        error="unresolved route recorded before workflow revisions were retained",
    )


def _seed_session_continuation(
    engine: Engine,
    task_id: int,
    inputs: TaskExecutionInputs,
    revision: WorkflowRevision,
) -> None:
    """Give this task's already-existing sessions a continuation policy.

    Those sessions were spawned before anything was pinned, so nothing
    records what they actually ran under, and a resume needs *some* complete
    policy. The confirmed inputs are that policy — for each declared session,
    the binding of the first declared step that uses it. It is stored as
    `migrated`, because it says what these sessions continue under and makes
    no claim about the turns they have already taken (ADR-0027).

    The retired `judge` session gets nothing. It is not declared by any
    definition, nothing will prompt it again, and writing it a continuation
    policy would suggest otherwise. Its transcript and its old applied policy
    stay exactly as they are.
    """
    session_step = {
        step.session: step.name for step in reversed(revision.definition.agent_steps())
    }
    for session in list_resumable_sessions(engine, task_id):
        step_name = session_step.get(session.name)
        if step_name is None:
            continue
        binding = inputs.step_bindings.get(step_name)
        if binding is None:
            continue
        record_applied_policy(
            engine,
            task_id,
            session.name,
            build_applied_policy(
                ModelPolicy.from_binding(binding),
                profile_name=binding.profile_name,
                role=binding.role,
                consumer_kind="step",
                consumer_name=step_name,
                origin=APPLIED_ORIGIN_MIGRATED,
            ),
        )


def unconfigured_task_ids(engine: Engine) -> list[int]:
    return [task.id for task in list_unconfigured_tasks(engine)]


__all__ = [
    "LEGACY_UNKNOWN_INPUTS",
    "LEGACY_UNKNOWN_WORKFLOW",
    "UNCERTAINTY_NOTICE",
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
    "workflow_continuation",
]
