"""Workflow run registry: step-record history against `workflow_step_records`
plus the run-status mutators for the workflow columns on `tasks`. No ORM —
Core only, mirroring the `registry/tasks.py` frozen-dataclass pattern.

One row per executed step; identity is `(task_id, seq)` because loops
(ROADMAP #18) revisit step names. `outcome` is the parsed
`.ompire/outcome.json` / command result / decision route / gate note dict,
or NULL when the step produced none (missing/malformed outcome file, or the
kind carries none).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine

from ompire_daemon.db import workflow_step_records
from ompire_daemon.registry.tasks import Task, _update

WORKFLOW_STATUSES = ("running", "waiting", "complete", "failed")
STEP_STATUSES = ("running", "waiting", "ok", "failed")
STEP_KINDS = ("agent", "command", "decision", "gate")


@dataclass(frozen=True)
class StepRecord:
    task_id: int
    seq: int
    step: str
    kind: str
    session: str | None
    status: str
    outcome: dict[str, Any] | None
    error: str | None
    prompted_at: str | None
    started_at: str
    finished_at: str | None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_record(row) -> StepRecord:  # noqa: ANN001
    outcome = json.loads(row.outcome_json) if row.outcome_json is not None else None
    return StepRecord(
        task_id=row.task_id,
        seq=row.seq,
        step=row.step,
        kind=row.kind,
        session=row.session,
        status=row.status,
        outcome=outcome,
        error=row.error,
        prompted_at=row.prompted_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


def append_step_record(
    engine: Engine,
    task_id: int,
    *,
    step: str,
    kind: str,
    session: str | None = None,
    status: str = "running",
    outcome: dict[str, Any] | None = None,
) -> StepRecord:
    """Append the next step record (seq = max+1) and return it."""
    now = _now_iso()
    with engine.begin() as conn:
        row = conn.execute(
            workflow_step_records.select()
            .where(workflow_step_records.c.task_id == task_id)
            .order_by(workflow_step_records.c.seq.desc())
            .limit(1)
        ).first()
        seq = (row.seq + 1) if row is not None else 1
        conn.execute(
            workflow_step_records.insert().values(
                task_id=task_id,
                seq=seq,
                step=step,
                kind=kind,
                session=session,
                status=status,
                outcome_json=json.dumps(outcome) if outcome is not None else None,
                error=None,
                prompted_at=None,
                started_at=now,
                finished_at=None,
            )
        )
    record = get_step_record(engine, task_id, seq)
    assert record is not None
    return record


def get_step_record(engine: Engine, task_id: int, seq: int) -> StepRecord | None:
    with engine.connect() as conn:
        row = conn.execute(
            workflow_step_records.select()
            .where(workflow_step_records.c.task_id == task_id)
            .where(workflow_step_records.c.seq == seq)
        ).first()
    return _row_to_record(row) if row is not None else None


def finish_step_record(
    engine: Engine,
    task_id: int,
    seq: int,
    *,
    status: str,
    outcome: dict[str, Any] | None = None,
    error: str | None = None,
) -> StepRecord:
    with engine.begin() as conn:
        conn.execute(
            workflow_step_records.update()
            .where(workflow_step_records.c.task_id == task_id)
            .where(workflow_step_records.c.seq == seq)
            .values(
                status=status,
                outcome_json=json.dumps(outcome) if outcome is not None else None,
                error=error,
                finished_at=_now_iso(),
            )
        )
    record = get_step_record(engine, task_id, seq)
    assert record is not None
    return record


def mark_prompt_sent(engine: Engine, task_id: int, seq: int) -> None:
    """Stamp an agent step's prompt as sent (restart recovery distinguishes
    "never prompted" from "turn lost"; workflow-engine design D-6)."""
    with engine.begin() as conn:
        conn.execute(
            workflow_step_records.update()
            .where(workflow_step_records.c.task_id == task_id)
            .where(workflow_step_records.c.seq == seq)
            .values(prompted_at=_now_iso())
        )


def set_gate_waiting(
    engine: Engine, task_id: int, seq: int, *, message: str
) -> StepRecord:
    """Park a gate step record `waiting`, carrying the operator-facing
    message in its outcome so a restart re-broadcasts it verbatim."""
    with engine.begin() as conn:
        conn.execute(
            workflow_step_records.update()
            .where(workflow_step_records.c.task_id == task_id)
            .where(workflow_step_records.c.seq == seq)
            .values(status="waiting", outcome_json=json.dumps({"message": message}))
        )
    record = get_step_record(engine, task_id, seq)
    assert record is not None
    return record


def list_step_records(engine: Engine, task_id: int) -> list[StepRecord]:
    with engine.connect() as conn:
        rows = conn.execute(
            workflow_step_records.select()
            .where(workflow_step_records.c.task_id == task_id)
            .order_by(workflow_step_records.c.seq)
        ).all()
    return [_row_to_record(row) for row in rows]


def latest_step_record(engine: Engine, task_id: int) -> StepRecord | None:
    with engine.connect() as conn:
        row = conn.execute(
            workflow_step_records.select()
            .where(workflow_step_records.c.task_id == task_id)
            .order_by(workflow_step_records.c.seq.desc())
            .limit(1)
        ).first()
    return _row_to_record(row) if row is not None else None


# --- run-status mutators (the workflow_* columns on `tasks`) -----------------


def set_run_status(
    engine: Engine, task_id: int, status: str | None, step: str | None
) -> Task:
    """Set the run status and current step. `step` is NULL whenever the run
    is not active (complete/failed), matching the card-pill derivation."""
    return _update(engine, task_id, workflow_status=status, workflow_step=step)


def set_run_failed(engine: Engine, task_id: int, error: str) -> Task:
    """Land the run `failed`: workflow status/step plus the error on the task
    row (the task's registry `state` stays `created` — workflow failure is
    not workspace failure)."""
    return _update(
        engine, task_id, workflow_status="failed", workflow_step=None, error=error
    )


def delete_step_records(engine: Engine, task_id: int) -> None:
    """Drop all step records for a task (purge path only)."""
    with engine.begin() as conn:
        conn.execute(
            workflow_step_records.delete().where(workflow_step_records.c.task_id == task_id)
        )
