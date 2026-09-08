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

# What an iteration's retained `findings` actually is. A correcting agent may
# only be handed a `complete` report: a count is not a report, and a partial
# one presented as the reviewer's whole opinion is worse than none.
FINDINGS_COMPLETE = "complete"
FINDINGS_EMPTY = "empty"
FINDINGS_TRUNCATED = "truncated"
FINDINGS_UNAVAILABLE = "unavailable"
FINDINGS_STATES = (
    FINDINGS_COMPLETE,
    FINDINGS_EMPTY,
    FINDINGS_TRUNCATED,
    FINDINGS_UNAVAILABLE,
)

# The retained report's ceiling. Beyond it the capture is recorded
# `truncated`, which is a refusal to correct automatically, not a smaller
# report.
MAX_FINDINGS_BYTES = 256 * 1024


@dataclass(frozen=True)
class ReviewIterationRecord:
    task_id: int
    seq: int
    outcome: str
    comment_count: int | None
    stderr: str | None
    recorded_at: str
    # The protected candidate this iteration actually graded (ADR-0032), or
    # None for an iteration recorded before content binding existed.
    candidate_id: str | None = None
    # The workflow attempt that asked for this review, or None for one an
    # operator started by hand. Never backfilled.
    workflow_seq: int | None = None
    # The reviewer's actual report, and what was retained of it.
    findings: str | None = None
    findings_state: str | None = None

    @property
    def actionable_findings(self) -> str | None:
        """The report a correction may be run against, or None.

        Only a `complete` capture qualifies. An empty, truncated, or missing
        one returns None so a caller has to say what it is doing rather than
        silently sending an agent a fragment.
        """
        if self.findings_state != FINDINGS_COMPLETE:
            return None
        return self.findings


@dataclass(frozen=True)
class ReviewRecord:
    task_id: int
    status: str
    process_started_at: str | None
    created_at: str
    updated_at: str
    iterations: list[ReviewIterationRecord]
    # The candidate the current (or last) round is bound to.
    candidate_id: str | None = None
    # The workflow attempt the current (or last) round was launched for.
    workflow_seq: int | None = None

    @property
    def approved_candidate_id(self) -> str | None:
        """What this review's approval is usable for, if anything.

        An approval names a candidate only when the terminal `approved`
        iteration recorded one. A legacy approval — recorded before reviews
        were content-bound — returns None: it stays visible as historical
        evidence and is never treated as authorization for current work.
        """
        if self.status != "approved":
            return None
        for iteration in reversed(self.iterations):
            if iteration.outcome == "approved":
                return iteration.candidate_id
        return None


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
        candidate_id=row.candidate_id,
        workflow_seq=row.workflow_seq,
        findings=row.findings,
        findings_state=row.findings_state,
    )


def _row_to_review(row, iterations: list[ReviewIterationRecord]) -> ReviewRecord:
    return ReviewRecord(
        task_id=row.task_id,
        status=row.status,
        process_started_at=row.process_started_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        iterations=iterations,
        candidate_id=row.candidate_id,
        workflow_seq=row.workflow_seq,
    )


def open_review(
    engine: Engine,
    task_id: int,
    *,
    candidate_id: str | None = None,
    workflow_seq: int | None = None,
) -> ReviewRecord:
    """Mark the task's review `open`, bind it to the candidate being graded,
    and stamp the write-ahead process marker. Upsert: a re-review keeps the
    existing row and its iterations, and rebinds to the new candidate — a
    second round grades the corrected content, not the content that produced
    the comments.

    `workflow_seq` binds the round to the run attempt that asked for it, and
    is written *with* the process marker rather than after the reviewer
    exits: a restart in between must be able to resolve the interrupted
    reviewer against the step still waiting on it. None is an operator-started
    review, which is a different fact, not a missing one."""
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
                    candidate_id=candidate_id,
                    workflow_seq=workflow_seq,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            conn.execute(
                reviews.update()
                .where(reviews.c.task_id == task_id)
                .values(
                    status="open",
                    process_started_at=now,
                    candidate_id=candidate_id,
                    workflow_seq=workflow_seq,
                    updated_at=now,
                )
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
    candidate_id: str | None = None,
    workflow_seq: int | None = None,
    findings: str | None = None,
) -> ReviewIterationRecord:
    """Append the next iteration (seq = max+1), optionally landing the
    review's status in the same transaction so a crash can never separate a
    terminal iteration from the status it produced.

    `findings` is the reviewer's own output. It is classified here rather than
    by the caller, so "there was no report" and "the report was too large to
    keep" are recorded as themselves instead of both arriving as an empty
    string a later correction would treat as an approval with no comments.
    """
    now = _now_iso()
    findings_state = _classify_findings(findings)
    if findings_state == FINDINGS_TRUNCATED:
        findings = findings[:MAX_FINDINGS_BYTES] if findings else findings
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
                candidate_id=candidate_id,
                workflow_seq=workflow_seq,
                findings=findings,
                findings_state=findings_state,
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
        candidate_id=candidate_id,
        workflow_seq=workflow_seq,
        findings=findings,
        findings_state=findings_state,
        recorded_at=now,
    )


def _classify_findings(findings: str | None) -> str | None:
    """What was retained of the reviewer's report.

    None means the outcome carries no report at all — an abort, an error, an
    interruption — and is recorded as no state rather than as an empty one.
    """
    if findings is None:
        return None
    if not findings.strip():
        return FINDINGS_EMPTY
    if len(findings.encode("utf-8")) > MAX_FINDINGS_BYTES:
        return FINDINGS_TRUNCATED
    return FINDINGS_COMPLETE


def iteration_for_step(
    engine: Engine, task_id: int, workflow_seq: int
) -> ReviewIterationRecord | None:
    """The newest iteration this workflow attempt produced, if any.

    What the runner reads after it is woken. The wake-up is a notification;
    this row is the authority, so a restart between the two reaches the same
    verdict the first pass would have.
    """
    with engine.connect() as conn:
        row = conn.execute(
            review_iterations.select()
            .where(review_iterations.c.task_id == task_id)
            .where(review_iterations.c.workflow_seq == workflow_seq)
            .order_by(review_iterations.c.seq.desc())
            .limit(1)
        ).first()
    return None if row is None else _row_to_iteration(row)


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
