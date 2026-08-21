"""Session registry: per-(task, session) rows against `task_sessions`. No ORM
— Core only, mirroring the `registry/tasks.py` frozen-dataclass pattern.

A row appears when the workflow engine first spawns the session (lazy spawn);
`omp_session_id` starts NULL and is filled by `mark_session_id` once the
daemon captures the omp session identity for `omp --resume` (crash-recovery
capability). Rows are history: they survive task archival and are deleted
only on purge.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Engine

from ompire_daemon.db import task_sessions


@dataclass(frozen=True)
class TaskSession:
    task_id: int
    name: str
    omp_session_id: str | None
    spawned_at: str


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_session(row) -> TaskSession:
    return TaskSession(
        task_id=row.task_id,
        name=row.name,
        omp_session_id=row.omp_session_id,
        spawned_at=row.spawned_at,
    )


def record_session_spawned(engine: Engine, task_id: int, name: str) -> TaskSession:
    """Insert the session row at first spawn. Idempotent per (task, name):
    a re-spawn after a rejected/raced start keeps the original row."""
    existing = get_session(engine, task_id, name)
    if existing is not None:
        return existing
    with engine.begin() as conn:
        conn.execute(
            task_sessions.insert().values(
                task_id=task_id,
                name=name,
                omp_session_id=None,
                spawned_at=_now_iso(),
            )
        )
    session = get_session(engine, task_id, name)
    assert session is not None
    return session


def get_session(engine: Engine, task_id: int, name: str) -> TaskSession | None:
    with engine.connect() as conn:
        row = conn.execute(
            task_sessions.select()
            .where(task_sessions.c.task_id == task_id)
            .where(task_sessions.c.name == name)
        ).first()
    return _row_to_session(row) if row is not None else None


def list_sessions(engine: Engine, task_id: int) -> list[TaskSession]:
    with engine.connect() as conn:
        rows = conn.execute(
            task_sessions.select()
            .where(task_sessions.c.task_id == task_id)
            .order_by(task_sessions.c.spawned_at, task_sessions.c.name)
        ).all()
    return [_row_to_session(row) for row in rows]


def list_resumable_sessions(engine: Engine, task_id: int) -> list[TaskSession]:
    """Sessions with a captured omp identity — the set `omp --resume` can
    bring back on startup recovery."""
    with engine.connect() as conn:
        rows = conn.execute(
            task_sessions.select()
            .where(task_sessions.c.task_id == task_id)
            .where(task_sessions.c.omp_session_id.isnot(None))
            .order_by(task_sessions.c.spawned_at, task_sessions.c.name)
        ).all()
    return [_row_to_session(row) for row in rows]


def mark_session_id(engine: Engine, task_id: int, name: str, omp_session_id: str) -> TaskSession:
    with engine.begin() as conn:
        conn.execute(
            task_sessions.update()
            .where(task_sessions.c.task_id == task_id)
            .where(task_sessions.c.name == name)
            .values(omp_session_id=omp_session_id)
        )
    session = get_session(engine, task_id, name)
    assert session is not None
    return session


def delete_sessions(engine: Engine, task_id: int) -> None:
    """Drop all session rows for a task (purge path only)."""
    with engine.begin() as conn:
        conn.execute(task_sessions.delete().where(task_sessions.c.task_id == task_id))
