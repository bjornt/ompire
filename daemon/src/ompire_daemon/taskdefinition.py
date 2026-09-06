"""The one way to get a task's workflow definition.

Every runtime consumer — the runner, recovery, session admission, the primary
session behind review and shipping, and the REST/WebSocket projections — comes
through here, and what it gets back is the revision the task itself pinned. The
workflow *name* is never looked up in today's catalog to answer "what does this
task run": that lookup is exactly how an edited definition would silently
change an accepted task, which is what pinning exists to prevent. Current-name
lookup survives in only two places, both of them prospective: resolving a new
launch, and offering a legacy task a continuation candidate.

Readiness is classified, not boolean. "This task was never configured", "its
revision predates retained definitions", "the stored document is damaged", and
"this daemon is too old to read it" call for four different operator actions,
and only one of them is confirmable. A task in any of those states stays
listed, readable, stoppable, and cleanable: one damaged row must not take the
dashboard with it.

ADR-0026, ADR-0028
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import Connection, Engine

from ompire_daemon.registry.workflow_definitions import (
    UNAVAILABLE_INTEGRITY,
    UNAVAILABLE_INVALID,
    UNAVAILABLE_MISSING,
    UNAVAILABLE_UNSUPPORTED,
    WorkflowRevisionUnavailableError,
    get_revision,
    get_revision_conn,
)
from ompire_daemon.workflow_definitions import WorkflowRevision

if TYPE_CHECKING:
    from ompire_daemon.registry.tasks import Task

# The task has no accepted launch inputs at all (ADR-0026).
READINESS_NEEDS_CONFIGURATION = "needs_configuration"
# The task has inputs but no pinned revision: it predates retained definitions
# and the operator has not confirmed a continuation. The one confirmable state.
READINESS_NEEDS_WORKFLOW_CONFIRMATION = "needs_workflow_confirmation"
# The pinned revision cannot be read. Mirrors the registry's reasons so the
# operator can tell a damaged row from a daemon that is simply too old.
READINESS_REVISION_MISSING = UNAVAILABLE_MISSING
READINESS_REVISION_UNSUPPORTED = UNAVAILABLE_UNSUPPORTED
READINESS_REVISION_INTEGRITY = UNAVAILABLE_INTEGRITY
READINESS_REVISION_INVALID = UNAVAILABLE_INVALID

CONFIRMABLE_READINESS = (READINESS_NEEDS_WORKFLOW_CONFIRMATION,)


class TaskDefinitionUnavailableError(Exception):
    """This task's definition cannot be resolved, and why.

    Blocks the task's own execution, session admission, and privileged
    actions. It never falls back to the catalog's current definition of the
    same name — running a task under a document it never accepted is the
    failure this error exists to report.
    """

    def __init__(self, task_id: int, reason: str, detail: str) -> None:
        super().__init__(f"task {task_id}: {detail}")
        self.task_id = task_id
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class WorkflowReadiness:
    """Whether this task's definition can be resolved, and what would fix it."""

    ready: bool
    reason: str | None
    detail: str | None
    revision: str | None

    @property
    def confirmable(self) -> bool:
        return self.reason in CONFIRMABLE_READINESS


_READY = WorkflowReadiness(ready=True, reason=None, detail=None, revision=None)


def _classify(task: Task) -> WorkflowReadiness | None:
    """The two states that need no database read."""
    inputs = task.execution_inputs
    if inputs is None:
        return WorkflowReadiness(
            ready=False,
            reason=READINESS_NEEDS_CONFIGURATION,
            detail=(
                "this task has no confirmed launch configuration; confirm one "
                "before continuing, reviewing, or shipping it"
            ),
            revision=None,
        )
    if inputs.workflow_binding is None:
        return WorkflowReadiness(
            ready=False,
            reason=READINESS_NEEDS_WORKFLOW_CONFIRMATION,
            detail=(
                "this task ran before workflow definitions were retained, so "
                f"the exact procedure it used was never recorded. Review the "
                f"current {inputs.workflow_name!r} definition and confirm it as "
                "the one this task continues under."
            ),
            revision=None,
        )
    return None


def workflow_readiness(engine: Engine, task: Task) -> WorkflowReadiness:
    """Classify without raising. Safe to call while serializing a task list."""
    early = _classify(task)
    if early is not None:
        return early
    assert task.execution_inputs is not None
    binding = task.execution_inputs.workflow_binding
    assert binding is not None
    try:
        get_revision(engine, binding.revision)
    except WorkflowRevisionUnavailableError as exc:
        return WorkflowReadiness(
            ready=False, reason=exc.reason, detail=exc.detail, revision=binding.revision
        )
    return WorkflowReadiness(
        ready=True, reason=None, detail=None, revision=binding.revision
    )


def resolve_task_definition(engine: Engine, task: Task) -> WorkflowRevision:
    """The definition this task actually runs, or a classified refusal."""
    readiness = _classify(task)
    if readiness is not None:
        raise TaskDefinitionUnavailableError(
            task.id, readiness.reason or "", readiness.detail or ""
        )
    assert task.execution_inputs is not None
    binding = task.execution_inputs.workflow_binding
    assert binding is not None
    try:
        return get_revision(engine, binding.revision)
    except WorkflowRevisionUnavailableError as exc:
        raise TaskDefinitionUnavailableError(task.id, exc.reason, exc.detail) from exc


def resolve_task_definition_conn(conn: Connection, task: Task) -> WorkflowRevision:
    """Same resolution, inside an already-open transaction."""
    readiness = _classify(task)
    if readiness is not None:
        raise TaskDefinitionUnavailableError(
            task.id, readiness.reason or "", readiness.detail or ""
        )
    assert task.execution_inputs is not None
    binding = task.execution_inputs.workflow_binding
    assert binding is not None
    try:
        return get_revision_conn(conn, binding.revision)
    except WorkflowRevisionUnavailableError as exc:
        raise TaskDefinitionUnavailableError(task.id, exc.reason, exc.detail) from exc


def task_primary_session(engine: Engine, task: Task) -> str:
    """The pinned definition's primary session: the target of task-scoped
    operations that mean "the agent" (review, ship; design D-8)."""
    return resolve_task_definition(engine, task).definition.primary


def task_declares_session(engine: Engine, task: Task, session: str) -> bool:
    """Whether the pinned definition declares this session.

    Legacy sessions a *retired* engine facility created — the implicit judge's
    `judge` session most of all — are not declared by any definition and are
    deliberately not admitted here. Their transcripts stay readable through
    task history; they are not addressable as live workflow sessions.
    """
    return session in resolve_task_definition(engine, task).definition.sessions
