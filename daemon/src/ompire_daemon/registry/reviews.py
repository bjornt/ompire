"""Review registry: durable review status and ordered iteration history
against `reviews` and `review_iterations`. No ORM — Core only, mirroring the
`registry/workflows.py` step-record pattern.

Architecture: ADR-0011
(docs/adr/0011-keep-review-and-publishing-authority-outside-agent-sandbox.md);
durability boundary: ADR-0016
(docs/adr/0016-persist-authority-bearing-task-history-and-provenance.md)

One `reviews` row per task, upserted on every start: re-review after comments
reopens the same review and appends to the same ordered history, so the loop
stays visible as one review rather than a sequence of unrelated ones.

`process_started_at` is a write-ahead marker rather than a display field. It
is stamped before llmvet is launched and cleared when the process is observed
exiting, which is the only reliable way for startup to tell an interrupted
reviewer from a review that is `open` only because its comments went back to
the agent — both are persisted `open`. The reviewer's URL and port are
process facts and are deliberately not persisted: a restored review must not
offer a dead external link.

Rows are history: they survive task archival and are deleted only on purge.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Engine

from ompire_daemon.db import review_iterations, reviews

REVIEW_STATUSES = ("open", "approved", "aborted", "error")
# `interrupted` is iteration-only: a daemon restart killed the reviewer. The
# review itself lands `aborted`, so the status vocabulary is unchanged.
ITERATION_OUTCOMES = ("approved", "comments", "aborted", "error", "interrupted")


@dataclass(frozen=True)
class ReviewIterationRecord:
    task_id: int
    seq: int
    outcome: str
    comment_count: int | None
    stderr: str | None
    recorded_at: str


@dataclass(frozen=True)
class ReviewRecord:
    task_id: int
    status: str
    process_started_at: str | None
    created_at: str
    updated_at: str
    iterations: list[ReviewIterationRecord]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_iteration(row) -> ReviewIterationRecord:
    return ReviewIterationRecord(
        task_id=row.task_id,
        seq=row.seq,
        outcome=row.outcome,
        comment_count=row.comment_count,
        stderr=row.stderr,
        recorded_at=row.recorded_at,
    )


def _row_to_review(row, iterations: list[ReviewIterationRecord]) -> ReviewRecord:
    return ReviewRecord(
        task_id=row.task_id,
        status=row.status,
        process_started_at=row.process_started_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        iterations=iterations,
    )


def open_review(engine: Engine, task_id: int) -> ReviewRecord:
    """Mark the task's review `open` and stamp the write-ahead process
    marker. Upsert: a re-review keeps the existing row and its iterations."""
    now = _now_iso()
    with engine.begin() as conn:
        existing = conn.execute(
            reviews.select().where(reviews.c.task_id == task_id)
        ).first()
        if existing is None:
            conn.execute(
                reviews.insert().values(
                    task_id=task_id,
                    status="open",
                    process_started_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            conn.execute(
                reviews.update()
                .where(reviews.c.task_id == task_id)
                .values(status="open", process_started_at=now, updated_at=now)
            )
    record = get_review(engine, task_id)
    assert record is not None
    return record


def clear_process_marker(engine: Engine, task_id: int) -> None:
    """Clear the write-ahead marker: the reviewer process was observed
    exiting, so a later startup must not treat this review as interrupted.
    A no-op when the task has no review row."""
    with engine.begin() as conn:
        conn.execute(
            reviews.update()
            .where(reviews.c.task_id == task_id)
            .values(process_started_at=None, updated_at=_now_iso())
        )


def set_status(engine: Engine, task_id: int, status: str) -> ReviewRecord | None:
    with engine.begin() as conn:
        conn.execute(
            reviews.update()
            .where(reviews.c.task_id == task_id)
            .values(status=status, updated_at=_now_iso())
        )
    return get_review(engine, task_id)


def append_iteration(
    engine: Engine,
    task_id: int,
    *,
    outcome: str,
    comment_count: int | None = None,
    stderr: str | None = None,
    status: str | None = None,
) -> ReviewIterationRecord:
    """Append the next iteration (seq = max+1), optionally landing the
    review's status in the same transaction so a crash can never separate a
    terminal iteration from the status it produced."""
    now = _now_iso()
    with engine.begin() as conn:
        row = conn.execute(
            review_iterations.select()
            .where(review_iterations.c.task_id == task_id)
            .order_by(review_iterations.c.seq.desc())
            .limit(1)
        ).first()
        seq = (row.seq + 1) if row is not None else 1
        conn.execute(
            review_iterations.insert().values(
                task_id=task_id,
                seq=seq,
                outcome=outcome,
                comment_count=comment_count,
                stderr=stderr,
                recorded_at=now,
            )
        )
        if status is not None:
            conn.execute(
                reviews.update()
                .where(reviews.c.task_id == task_id)
                .values(status=status, updated_at=now)
            )
    return ReviewIterationRecord(
        task_id=task_id,
        seq=seq,
        outcome=outcome,
        comment_count=comment_count,
        stderr=stderr,
        recorded_at=now,
    )


def list_iterations(engine: Engine, task_id: int) -> list[ReviewIterationRecord]:
    with engine.connect() as conn:
        rows = conn.execute(
            review_iterations.select()
            .where(review_iterations.c.task_id == task_id)
            .order_by(review_iterations.c.seq)
        ).all()
    return [_row_to_iteration(row) for row in rows]


def get_review(engine: Engine, task_id: int) -> ReviewRecord | None:
    with engine.connect() as conn:
        row = conn.execute(
            reviews.select().where(reviews.c.task_id == task_id)
        ).first()
        if row is None:
            return None
        iteration_rows = conn.execute(
            review_iterations.select()
            .where(review_iterations.c.task_id == task_id)
            .order_by(review_iterations.c.seq)
        ).all()
    return _row_to_review(row, [_row_to_iteration(r) for r in iteration_rows])


def list_reviews(engine: Engine) -> list[ReviewRecord]:
    """Every task's review, newest iterations in order. Used to compose the
    WebSocket snapshot, so tasks with no review are simply absent."""
    with engine.connect() as conn:
        review_rows = conn.execute(reviews.select()).all()
        iteration_rows = conn.execute(
            review_iterations.select().order_by(
                review_iterations.c.task_id, review_iterations.c.seq
            )
        ).all()
    by_task: dict[int, list[ReviewIterationRecord]] = {}
    for row in iteration_rows:
        by_task.setdefault(row.task_id, []).append(_row_to_iteration(row))
    return [_row_to_review(row, by_task.get(row.task_id, [])) for row in review_rows]


def list_interrupted_candidates(engine: Engine) -> list[ReviewRecord]:
    """Reviews persisted `open` with an uncleared process marker: their
    llmvet process died with the daemon. A review left `open` because its
    comments went back to the agent has a cleared marker and is not a
    candidate."""
    with engine.connect() as conn:
        rows = conn.execute(
            reviews.select()
            .where(reviews.c.status == "open")
            .where(reviews.c.process_started_at.isnot(None))
            .order_by(reviews.c.task_id)
        ).all()
    return [_row_to_review(row, list_iterations(engine, row.task_id)) for row in rows]


def delete_review(engine: Engine, task_id: int) -> None:
    """Drop a task's review row and iterations (purge path only). Cleanup
    deliberately retains them — see ADR-0016 and `VISION.md` principle 4."""
    with engine.begin() as conn:
        conn.execute(
            review_iterations.delete().where(review_iterations.c.task_id == task_id)
        )
        conn.execute(reviews.delete().where(reviews.c.task_id == task_id))
