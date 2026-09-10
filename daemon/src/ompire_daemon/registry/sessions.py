"""Session registry: per-(task, session) rows against `task_sessions`. No ORM
— Core only, mirroring the `work/tasks.py` frozen-dataclass pattern.

A row appears when the workflow engine first spawns the session (lazy spawn);
`omp_session_id` starts NULL and is filled by `mark_session_id` once the
daemon captures the omp session identity for `omp --resume` (crash-recovery
capability). Rows are history: they survive task archival and are deleted
only on purge.

Each row also carries the model policy its child last *successfully* ran
under (ADR-0027). That is a different kind of fact from the task's accepted
inputs: the task says what each consumer may run, the session says what
actually took effect and therefore what a resume, a follow-up, review
feedback, or ship drafting continues with. A started step record is not
evidence of application — a configuration can fail after the step opened —
so the applied record is written only after the native state was verified,
and always before the turn that runs under it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine

from ompire_daemon.db import task_sessions
from ompire_daemon.work.inputs import ModelPolicy

APPLIED_POLICY_VERSION = 1

# How a session came to hold this policy.
APPLIED_ORIGIN_VERIFIED = "verified"
# Carried forward by the upgrade from a task's pinned task-wide inputs. It
# says what the session *continues* under, and deliberately does not claim
# any past turn was configured this way — nobody recorded that.
APPLIED_ORIGIN_MIGRATED = "migrated"


@dataclass(frozen=True)
class AppliedPolicy:
    """The complete policy one session's child last ran under, with the
    accepted consumer it came from."""

    policy: ModelPolicy
    profile_name: str
    role: str
    # `step`, `auxiliary`, or `None` when the upgrade derived it rather than
    # a declared consumer applying it.
    consumer_kind: str | None
    consumer_name: str | None
    origin: str
    applied_at: str

    @property
    def verified(self) -> bool:
        return self.origin == APPLIED_ORIGIN_VERIFIED


@dataclass(frozen=True)
class TaskSession:
    task_id: int
    name: str
    omp_session_id: str | None
    spawned_at: str
    applied_policy: AppliedPolicy | None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def applied_policy_document(applied: AppliedPolicy) -> dict[str, Any]:
    return {
        "version": APPLIED_POLICY_VERSION,
        "policy": applied.policy.payload(),
        "profile_name": applied.profile_name,
        "role": applied.role,
        "consumer_kind": applied.consumer_kind,
        "consumer_name": applied.consumer_name,
        "origin": applied.origin,
        "applied_at": applied.applied_at,
    }


class UnsupportedAppliedPolicyVersionError(ValueError):
    def __init__(self, version: object) -> None:
        super().__init__(
            f"session applied policy is version {version!r}; this daemon "
            f"understands version {APPLIED_POLICY_VERSION}"
        )
        self.version = version


def decode_applied_policy(raw: str | None) -> AppliedPolicy | None:
    if raw is None:
        return None
    document = json.loads(raw)
    version = document.get("version")
    if version != APPLIED_POLICY_VERSION:
        raise UnsupportedAppliedPolicyVersionError(version)
    return AppliedPolicy(
        policy=ModelPolicy.from_payload(document["policy"]),
        profile_name=document["profile_name"],
        role=document["role"],
        consumer_kind=document["consumer_kind"],
        consumer_name=document["consumer_name"],
        origin=document["origin"],
        applied_at=document["applied_at"],
    )


def build_applied_policy(
    policy: ModelPolicy,
    *,
    profile_name: str,
    role: str,
    consumer_kind: str | None,
    consumer_name: str | None,
    origin: str = APPLIED_ORIGIN_VERIFIED,
) -> AppliedPolicy:
    return AppliedPolicy(
        policy=policy,
        profile_name=profile_name,
        role=role,
        consumer_kind=consumer_kind,
        consumer_name=consumer_name,
        origin=origin,
        applied_at=_now_iso(),
    )


def _row_to_session(row) -> TaskSession:
    return TaskSession(
        task_id=row.task_id,
        name=row.name,
        omp_session_id=row.omp_session_id,
        spawned_at=row.spawned_at,
        applied_policy=decode_applied_policy(row.applied_policy_json),
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
                applied_policy_json=None,
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


def record_applied_policy(
    engine: Engine, task_id: int, name: str, applied: AppliedPolicy
) -> TaskSession:
    """Persist what this session's child was just verified to be running.

    Called after the native handshake succeeded and before the turn that
    depends on it, so a crash between the two can only lose the *prompt*, not
    the knowledge of how the process is configured. The row is created if the
    session has not been recorded yet, which keeps the write safe on the
    resume path (where the row exists) and on any future caller that applies
    a policy before `record_session_spawned` ran.
    """
    document = json.dumps(applied_policy_document(applied))
    with engine.begin() as conn:
        updated = conn.execute(
            task_sessions.update()
            .where(task_sessions.c.task_id == task_id)
            .where(task_sessions.c.name == name)
            .values(applied_policy_json=document)
        )
        if updated.rowcount == 0:
            conn.execute(
                task_sessions.insert().values(
                    task_id=task_id,
                    name=name,
                    omp_session_id=None,
                    spawned_at=_now_iso(),
                    applied_policy_json=document,
                )
            )
    session = get_session(engine, task_id, name)
    assert session is not None
    return session


def delete_sessions(engine: Engine, task_id: int) -> None:
    """Drop all session rows for a task (purge path only)."""
    with engine.begin() as conn:
        conn.execute(task_sessions.delete().where(task_sessions.c.task_id == task_id))
