"""Workflow run registry: step-record history against `workflow_step_records`
plus the run-status mutators for the workflow columns on `tasks`. No ORM —
Core only, mirroring the `registry/tasks.py` frozen-dataclass pattern.

Architecture: ADR-0008
(docs/adr/0008-model-tasks-as-workflows-over-named-sessions.md)

One row per executed step; identity is `(task_id, seq)` because loops
(ROADMAP #18) revisit step names. `outcome` is the parsed
`.ompire/outcome.json` / command result / decision route / gate note dict,
or NULL when the step produced none (missing/malformed outcome file, or the
kind carries none).

Two kinds of waiting live here and must not be confused (ADR-0028). A
*declared gate* is a step the definition says stops for a human: it carries
its message as its outcome and resuming finishes it `ok`. An *uncertainty
pause* is the engine refusing to guess: the attempt keeps its own kind, its
absent outcome, and the error that stopped it, and `pause` says why and what a
retry would re-enter. Retrying opens a new attempt of the blocked step rather
than falling through as if the missing evidence had been accepted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine

from ompire_daemon.db import tasks as tasks_table
from ompire_daemon.db import workflow_step_records
from ompire_daemon.registry.tasks import Task, _row_to_task, _update

WORKFLOW_STATUSES = ("running", "waiting", "complete", "failed")
STEP_STATUSES = ("running", "waiting", "ok", "failed")
STEP_KINDS = ("agent", "command", "decision", "gate")

# Why the engine stopped rather than choosing. Each one names evidence that is
# absent or unreadable, never a declared negative result: a `failed` outcome
# and a nonzero exit code are data and follow the definition's own routes.
PAUSE_MISSING_OUTCOME = "missing_outcome"
PAUSE_UNRESOLVED_DECISION = "unresolved_decision"
PAUSE_PROMPT_UNRENDERABLE = "prompt_unrenderable"
PAUSE_CONDITION_UNRESOLVED = "condition_unresolved"
PAUSE_VERSION = 1

# Appended to a paused attempt's error when an operator authorizes a retry.
# The attempt stays `failed` with its original reason above this line; the new
# attempt reads it back so a restart still knows it is a re-attempt.
RETRY_NOTE = "retried by the operator; this attempt keeps its unresolved result"


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
    # An uncertainty pause document, or None. Set only while this attempt is
    # the one the run is waiting on; cleared when it is retried.
    pause: dict[str, Any] | None
    prompted_at: str | None
    started_at: str
    finished_at: str | None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_record(row) -> StepRecord:
    outcome = json.loads(row.outcome_json) if row.outcome_json is not None else None
    pause = json.loads(row.pause_json) if row.pause_json is not None else None
    return StepRecord(
        task_id=row.task_id,
        seq=row.seq,
        step=row.step,
        kind=row.kind,
        session=row.session,
        status=row.status,
        outcome=outcome,
        error=row.error,
        pause=pause,
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
                pause_json=None,
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
                pause_json=None,
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


def build_pause(
    *,
    reason: str,
    message: str,
    step: str,
    retry_step: str,
    retry_kind: str | None = None,
) -> dict[str, Any]:
    """One uncertainty pause document.

    `retry_step` is what an operator retry re-enters. For a pause the engine
    raised that is the blocked step itself: retrying is another attempt at the
    thing that could not be decided, never permission to continue past it.
    `retry_kind` differs from the paused record's kind only for the one legacy
    shape a continuation re-labels — an old synthesized escalation gate, whose
    retry belongs to the *decision* it was standing in for.
    """
    return {
        "version": PAUSE_VERSION,
        "reason": reason,
        "message": message,
        "step": step,
        "retry_step": retry_step,
        "retry_kind": retry_kind,
    }


def _set_waiting(
    engine: Engine,
    task_id: int,
    seq: int,
    *,
    step: str,
    values: dict[str, Any],
) -> tuple[StepRecord, Task]:
    """Park one attempt and the run together, in a single transaction.

    Atomicity is the requirement: a record marked waiting while the run row
    still says running (or the reverse) is a state no recovery rule covers,
    and it is reachable by an unlucky restart between two separate writes.
    """
    with engine.begin() as conn:
        conn.execute(
            workflow_step_records.update()
            .where(workflow_step_records.c.task_id == task_id)
            .where(workflow_step_records.c.seq == seq)
            .values(status="waiting", **values)
        )
        conn.execute(
            tasks_table.update()
            .where(tasks_table.c.id == task_id)
            .values(
                workflow_status="waiting", workflow_step=step, updated_at=_now_iso()
            )
        )
        record_row = conn.execute(
            workflow_step_records.select()
            .where(workflow_step_records.c.task_id == task_id)
            .where(workflow_step_records.c.seq == seq)
        ).one()
        task_row = conn.execute(
            tasks_table.select().where(tasks_table.c.id == task_id)
        ).one()
    return _row_to_record(record_row), _row_to_task(task_row)


def pause_step(
    engine: Engine, task_id: int, seq: int, *, pause: dict[str, Any], error: str
) -> tuple[StepRecord, Task]:
    """Record an uncertainty pause on the attempt that could not proceed.

    The attempt keeps its kind and its absent outcome; the error it hit is
    preserved verbatim. Nothing here writes a synthetic result — that is the
    difference between "the engine does not know" and "the engine decided".
    """
    return _set_waiting(
        engine,
        task_id,
        seq,
        step=pause["step"],
        values={"pause_json": json.dumps(pause), "error": error},
    )


def park_gate(
    engine: Engine, task_id: int, seq: int, *, step: str, message: str
) -> tuple[StepRecord, Task]:
    """Park a declared gate: the message is its outcome, so a restart
    re-broadcasts it verbatim."""
    return _set_waiting(
        engine,
        task_id,
        seq,
        step=step,
        values={"outcome_json": json.dumps({"message": message})},
    )


class WorkflowWaitConflictError(Exception):
    """The submitted resume or retry names an attempt the run is not on.

    A stale browser tab and a double submission look identical from here, and
    both must be refused: advancing a *different* attempt would apply an
    operator's decision to evidence they never saw.
    """

    def __init__(self, task_id: int, expected_seq: int, actual_seq: int | None) -> None:
        super().__init__(
            f"task {task_id} is not waiting at step attempt {expected_seq} "
            f"(current: {actual_seq if actual_seq is not None else 'none'})"
        )
        self.task_id = task_id
        self.expected_seq = expected_seq
        self.actual_seq = actual_seq


def retry_paused_step(
    engine: Engine, task_id: int, seq: int, *, target: tuple[str, str] | None = None
) -> tuple[StepRecord, Task]:
    """Authorize one retry of a paused attempt, atomically.

    Everything the retry needs to survive a restart lands in one transaction:
    the paused attempt is finished `failed` with its reason retained, a new
    attempt is opened `running`, and the run's current-step pointer moves to
    it. Execution is scheduled only after that commit, so a crash in between
    leaves an ordinary interrupted attempt for recovery to re-drive rather than
    an authorization nobody recorded.

    `target` is the `(step, kind)` the caller resolved against the task's
    pinned definition. It normally repeats the pause's own `retry_step`, and
    differs only when the blocked step has reached its declared visit bound:
    an operator retry is a human decision, but it is not a way around a bound
    the definition set, so the run goes to the declared exhaustion gate
    instead. Passing None uses what the pause recorded.

    The original attempt is never edited into a success and never deleted:
    the pause, its evidence, and its error stay in the history.
    """
    from ompire_daemon.registry.model_profiles import reserved_write

    now = _now_iso()
    with reserved_write(engine) as conn:
        row = conn.execute(
            workflow_step_records.select()
            .where(workflow_step_records.c.task_id == task_id)
            .order_by(workflow_step_records.c.seq.desc())
            .limit(1)
        ).first()
        if row is None or row.seq != seq or row.status != "waiting" or row.pause_json is None:
            raise WorkflowWaitConflictError(
                task_id, seq, row.seq if row is not None else None
            )
        pause = json.loads(row.pause_json)
        if target is not None:
            retry_step, retry_kind = target
        else:
            retry_step = pause.get("retry_step", row.step)
            retry_kind = pause.get("retry_kind") or row.kind
        conn.execute(
            workflow_step_records.update()
            .where(workflow_step_records.c.task_id == task_id)
            .where(workflow_step_records.c.seq == seq)
            .values(
                status="failed",
                pause_json=None,
                error=(f"{row.error}\n\n" if row.error else "") + RETRY_NOTE,
                finished_at=now,
            )
        )
        next_seq = seq + 1
        conn.execute(
            workflow_step_records.insert().values(
                task_id=task_id,
                seq=next_seq,
                step=retry_step,
                kind=retry_kind,
                session=row.session if retry_kind == row.kind else None,
                status="running",
                outcome_json=None,
                error=None,
                pause_json=None,
                prompted_at=None,
                started_at=now,
                finished_at=None,
            )
        )
        conn.execute(
            tasks_table.update()
            .where(tasks_table.c.id == task_id)
            .values(workflow_status="running", workflow_step=retry_step, updated_at=now)
        )
        record_row = conn.execute(
            workflow_step_records.select()
            .where(workflow_step_records.c.task_id == task_id)
            .where(workflow_step_records.c.seq == next_seq)
        ).one()
        task_row = conn.execute(
            tasks_table.select().where(tasks_table.c.id == task_id)
        ).one()
    return _row_to_record(record_row), _row_to_task(task_row)


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
