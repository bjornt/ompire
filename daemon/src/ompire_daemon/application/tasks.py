"""Task read composition and explicit continuation.

Assembles what a task reader is told — the canonical task payload, the
on-demand workshop status, and the workflow attempt history — and guards the
one explicit lifecycle command that belongs to work: Continue. No lifecycle
algorithm lives here; an eligible Continue is delegated through the same
recovery operation startup uses.

ADR-0026 (configuration guard), crash-recovery design D-4.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field

from sqlalchemy import Engine

from ompire_daemon.oversight.tasks import task_payload
from ompire_daemon.registry.workflows import list_step_records
from ompire_daemon.work.tasks import (
    Task,
    get_task,
    list_tasks,
    require_task_inputs,
)
from ompire_daemon.workshop import workshop_status


class ContinueIneligibleError(Exception):
    """Only an interrupted run — a task left `running` or `waiting` — can be
    continued. A failed or completed task is not silently restarted, and
    review and ship remain their own actions."""

    def __init__(self, task_id: int, workflow_status: str | None) -> None:
        super().__init__(
            f"task {task_id} workflow is {workflow_status or 'not running'}; "
            "only an interrupted run can be continued"
        )
        self.task_id = task_id
        self.workflow_status = workflow_status


@dataclass(frozen=True)
class TaskDetail:
    """A task row plus the reads derived around it for one detail view."""

    task: Task
    workshop_status: str | None
    workflow_steps: list[dict] = field(default_factory=list)


def task_list_payloads(engine: Engine) -> list[dict]:
    """Every task in the canonical wire shape, newest first."""
    return [task_payload(task, engine=engine) for task in list_tasks(engine)]


async def task_detail(engine: Engine, task_id: int) -> TaskDetail:
    """One task with its derived workshop status and attempt history.

    Workshop status is derived on demand from the workshop CLI, never
    persisted (design D-3); the step records are the same records the
    snapshot replays, from the same registry.
    """
    task = get_task(engine, task_id)
    derived_status = (
        await workshop_status(task.clone_path) if task.workshop_id else None
    )
    return TaskDetail(
        task=task,
        workshop_status=derived_status,
        workflow_steps=[
            asdict(record) for record in list_step_records(engine, task_id)
        ],
    )


async def continue_task(
    engine: Engine,
    task_id: int,
    resume: Callable[[Task], Awaitable[None]],
) -> dict:
    """Resume a task whose run was left in place, through the injected
    recovery operation (the same one startup uses, and therefore the same run
    guard, so a Continue that races an already-running run is a no-op rather
    than a second run)."""
    task = get_task(engine, task_id)
    require_task_inputs(task)
    if task.workflow_status not in ("running", "waiting"):
        raise ContinueIneligibleError(task_id, task.workflow_status)
    await resume(task)
    return task_payload(task, engine=engine)
