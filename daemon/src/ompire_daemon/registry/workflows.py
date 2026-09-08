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
STEP_KINDS = ("agent", "command", "decision", "gate", "review", "delivery")

# Why the engine stopped rather than choosing. Each one names evidence that is
# absent or unreadable, never a declared negative result: a `failed` outcome
# and a nonzero exit code are data and follow the definition's own routes.
PAUSE_MISSING_OUTCOME = "missing_outcome"
PAUSE_UNRESOLVED_DECISION = "unresolved_decision"
PAUSE_PROMPT_UNRENDERABLE = "prompt_unrenderable"
PAUSE_CONDITION_UNRESOLVED = "condition_unresolved"
# A required format-2 evidence selector matched nothing. The attempt is
# open and recorded, and it stops here rather than prompting an agent with
# a handoff its author said it must have.
PAUSE_MISSING_EVIDENCE = "missing_evidence"
# The task's workspace is owned by another daemon-managed writer — a review, a
# delivery — or an unresolved privileged effect makes writing to it unsafe
# (ADR-0032). The step is not started, its attempt keeps its own evidence, and
# an operator retry re-enters it once the workspace is free.
PAUSE_WORKSPACE_UNAVAILABLE = "workspace_unavailable"
# A review step could not start: there is nothing to review, the candidate
# could not be resolved, or the reviewer could not be launched. The engine
# does not invent a verdict for a review that never ran — an approval nobody
# gave is the one thing this pause exists to prevent.
PAUSE_REVIEW_UNAVAILABLE = "review_unavailable"
# A delivery step is authorized but its effect cannot begin, or a previous
# effect's outcome is unknown. Nothing privileged is retried automatically;
# the operator sees why and what would resolve it.
PAUSE_DELIVERY_BLOCKED = "delivery_blocked"
# An authorized chain was interrupted between its grant and its effect, or
# mid-effect. The remaining work resumes only after a fresh confirmation
# against the same journal, never from a generic retry.
PAUSE_DELIVERY_CONTINUATION = "delivery_continuation"
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
    # What this attempt bound when it opened (ADR-0029): alias -> {step, seq}
    # or null. None means the attempt recorded none — a format-1 attempt, or a
    # step declaring no evidence — which is different from "bound nothing".
    evidence: dict[str, Any] | None
    prompted_at: str | None
    started_at: str
    finished_at: str | None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_record(row) -> StepRecord:
    outcome = json.loads(row.outcome_json) if row.outcome_json is not None else None
    pause = json.loads(row.pause_json) if row.pause_json is not None else None
    evidence = (
        json.loads(row.evidence_json) if row.evidence_json is not None else None
    )
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
        evidence=evidence,
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
    evidence: dict[str, Any] | None = None,
) -> StepRecord:
    """Append the next step record (seq = max+1) and return it.

    `evidence` is written once, here, with the attempt: freezing it at entry is
    what makes "the evidence this attempt was given" a recorded fact rather
    than a query re-run against whatever history looks like later.
    """
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
                evidence_json=json.dumps(evidence) if evidence is not None else None,
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
    engine: Engine,
    task_id: int,
    seq: int,
    *,
    step: str,
    message: str,
    snapshot: dict[str, Any] | None = None,
) -> tuple[StepRecord, Task]:
    """Park a declared gate, persisting the question before anyone can answer.

    Format 1 records only its message, which is all it has. Format 2 records
    the whole snapshot — message, offered choices, bound evidence — because an
    answer is only meaningful against the question that was actually shown,
    and that question must survive a restart and a later definition edit.
    """
    return _set_waiting(
        engine,
        task_id,
        seq,
        step=step,
        values={
            "outcome_json": json.dumps(
                snapshot if snapshot is not None else {"message": message}
            )
        },
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


GATE_SNAPSHOT_VERSION = 2

# The one actor a single-user authenticated daemon can honestly name. It is not
# a person's identity: the bearer token says "the operator", and recording a
# guessed name beside a durable decision would be worse than recording none.
GATE_ACTOR = "operator"

MAX_FEEDBACK_BYTES = 16 * 1024


class WorkflowGateChoiceError(ValueError):
    """The submitted answer is not one this gate offers, or is incomplete.

    Distinct from a stale-attempt conflict: the operator is looking at the
    right question and gave an answer it does not accept, so the useful reply
    names the field rather than telling them to reload.
    """

    def __init__(self, detail: str, *, field: str = "choice_id") -> None:
        super().__init__(detail)
        self.detail = detail
        self.field = field


@dataclass(frozen=True)
class DeliveryAuthorization:
    """One approved delivery grant, ready to be committed with its decision.

    A value, not a command: everything here was already resolved and checked
    by the delivery service against the current candidate, review, and policy.
    What the workflow registry adds is atomicity — this becomes durable in the
    same transaction as the answer that permitted it, or neither does.
    """

    delivery_id: int
    expected_version: int
    candidate_id: str
    review_candidate_id: str | None
    review_seq: int | None
    mode: str
    ending: str
    actions: tuple[str, ...]
    commit_message: str | None
    pr_title: str | None
    pr_body: str | None
    routing: dict[str, Any]
    identity: dict[str, Any]
    request_key: str
    input_fingerprint: str


def build_gate_snapshot(
    *,
    message: str,
    choices: list[dict[str, Any]],
    evidence: dict[str, Any],
    delivery: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The question, exactly as it was put to a person.

    Persisted before anyone can answer, and never rewritten afterwards. A gate
    answered last week must still show the message, the options, and the
    records it was asking about, even if the definition has since changed —
    otherwise the recorded choice is an answer to a question nobody can read.
    """
    snapshot = {
        "version": GATE_SNAPSHOT_VERSION,
        "message": message,
        "choices": choices,
        "evidence": evidence,
    }
    if delivery is not None:
        # A delivery gate also records the review its grant is bound to and
        # the publication text the definition suggested. Both belong to the
        # question: an operator reading it later must see what was proposed,
        # not only what was finally published.
        snapshot["delivery"] = delivery
    return snapshot


def gate_decision_document(
    *,
    choice_id: str,
    label: str,
    feedback: str | None,
    destination: dict[str, Any],
    decided_at: str | None = None,
) -> dict[str, Any]:
    return {
        "choice_id": choice_id,
        "label": label,
        "feedback": feedback,
        "destination": destination,
        "actor": GATE_ACTOR,
        "decided_at": decided_at or _now_iso(),
    }


def resolve_gate(
    engine: Engine,
    task_id: int,
    seq: int,
    *,
    choice_id: str,
    feedback: str | None,
    successor: tuple[str, str, str | None, dict[str, Any] | None] | None,
    terminal_result: str | None,
    authorization: DeliveryAuthorization | None = None,
) -> tuple[StepRecord, Task]:
    """Record a human's answer and advance the run — in one transaction.

    This is the write the old in-memory gate future could not make. There, the
    daemon acknowledged an answer and *then* advanced, so a crash in between
    lost a decision a person had already made, and a second submission could
    be applied to whatever the run was waiting on by then.

    Everything that must agree lands together: the answer on the waiting
    attempt, that attempt's completion, and either the successor attempt with
    its own frozen evidence or the run's terminal status and named result. The
    caller schedules execution only after this returns, so a crash before the
    commit leaves the original unanswered gate — the question is still open,
    which is true — and a crash after it leaves an already-opened successor for
    recovery to pick up, never a gate to answer twice.

    The choice is checked against the *persisted snapshot*, not against
    today's definition: the snapshot is the question that was actually asked.

    Refusals: `WorkflowWaitConflictError` when this is not the attempt the run
    is waiting on (a stale tab, a duplicate submit, an already-answered gate),
    and `WorkflowGateChoiceError` when the answer names something this gate
    does not offer.
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
        task_row = conn.execute(
            tasks_table.select().where(tasks_table.c.id == task_id)
        ).one()
        if (
            row is None
            or row.seq != seq
            or row.status != "waiting"
            or row.kind != "gate"
            or row.pause_json is not None
            or task_row.workflow_status != "waiting"
        ):
            raise WorkflowWaitConflictError(
                task_id, seq, row.seq if row is not None else None
            )
        snapshot = json.loads(row.outcome_json) if row.outcome_json is not None else {}
        if snapshot.get("version") != GATE_SNAPSHOT_VERSION:
            raise WorkflowGateChoiceError(
                "this gate does not offer named choices", field="choice_id"
            )
        if snapshot.get("decision") is not None:
            # Answered already. The successor is committed; re-applying would
            # open a second one for a decision made once.
            raise WorkflowWaitConflictError(task_id, seq, row.seq)
        offered = {
            choice["id"]: choice
            for choice in snapshot.get("choices", [])
            if isinstance(choice, dict) and isinstance(choice.get("id"), str)
        }
        choice = offered.get(choice_id)
        if choice is None:
            raise WorkflowGateChoiceError(
                f"{choice_id!r} is not one of this gate's choices: "
                f"{', '.join(sorted(offered)) or 'none'}"
            )
        if choice.get("feedback_required") and not (feedback or "").strip():
            raise WorkflowGateChoiceError(
                f"the choice {choice_id!r} requires feedback", field="note"
            )
        decision = gate_decision_document(
            choice_id=choice_id,
            label=str(choice.get("label", choice_id)),
            feedback=feedback,
            destination=choice.get("next", {}),
            decided_at=now,
        )
        if authorization is not None:
            # The grant lands here, on this connection, or not at all. A
            # decision committed without its authorization would leave a person
            # having approved publication and nothing on record permitting it;
            # an authorization committed without its decision would be worse.
            from ompire_daemon.registry.ships import authorize_delivery_in

            authorize_delivery_in(
                conn,
                authorization.delivery_id,
                expected_version=authorization.expected_version,
                candidate_id=authorization.candidate_id,
                review_candidate_id=authorization.review_candidate_id,
                mode=authorization.mode,
                ending=authorization.ending,
                commit_message=authorization.commit_message,
                pr_title=authorization.pr_title,
                pr_body=authorization.pr_body,
                routing=authorization.routing,
                identity=authorization.identity,
                request_key=authorization.request_key,
                input_fingerprint=authorization.input_fingerprint,
                workflow_gate_seq=seq,
                workflow_choice_id=choice_id,
                review_seq=authorization.review_seq,
            )
            # Recorded on the answer itself, so the question, the choice, and
            # what it permitted are one readable record rather than two rows a
            # reader has to correlate by timestamp.
            decision["authorization"] = {
                "delivery_id": authorization.delivery_id,
                "ending": authorization.ending,
                "mode": authorization.mode,
                "actions": list(authorization.actions),
                "candidate_id": authorization.candidate_id,
                "review_seq": authorization.review_seq,
            }
        snapshot["decision"] = decision
        record_row, task_row = _advance_in(
            conn,
            task_id,
            seq,
            now,
            outcome=snapshot,
            successor=successor,
            terminal_result=terminal_result,
        )
    return _row_to_record(record_row), _row_to_task(task_row)


def _advance_in(
    conn,
    task_id: int,
    seq: int,
    now: str,
    *,
    outcome: dict[str, Any] | None,
    successor: tuple[str, str, str | None, dict[str, Any] | None] | None,
    terminal_result: str | None,
):
    """Finish one attempt and open its successor, on a caller's connection.

    Shared by every transition that has to be atomic with something else — a
    human decision and the authority it grants, a privileged effect and the
    step that consumed it. Splitting either pair across two commits creates a
    window where the run and the record of what happened disagree, and that
    window is exactly where a duplicate signature or a lost approval lives.
    """
    conn.execute(
        workflow_step_records.update()
        .where(workflow_step_records.c.task_id == task_id)
        .where(workflow_step_records.c.seq == seq)
        .values(
            status="ok",
            outcome_json=json.dumps(outcome) if outcome is not None else None,
            pause_json=None,
            finished_at=now,
        )
    )
    if successor is not None:
        step, kind, session, evidence = successor
        conn.execute(
            workflow_step_records.insert().values(
                task_id=task_id,
                seq=seq + 1,
                step=step,
                kind=kind,
                session=session,
                status="running",
                outcome_json=None,
                error=None,
                pause_json=None,
                evidence_json=(json.dumps(evidence) if evidence is not None else None),
                prompted_at=None,
                started_at=now,
                finished_at=None,
            )
        )
        conn.execute(
            tasks_table.update()
            .where(tasks_table.c.id == task_id)
            .values(workflow_status="running", workflow_step=step, updated_at=now)
        )
    else:
        conn.execute(
            tasks_table.update()
            .where(tasks_table.c.id == task_id)
            .values(
                workflow_status="complete",
                workflow_step=None,
                workflow_result=terminal_result,
                updated_at=now,
            )
        )
    record_row = conn.execute(
        workflow_step_records.select()
        .where(workflow_step_records.c.task_id == task_id)
        .where(workflow_step_records.c.seq == (seq + 1 if successor else seq))
    ).one()
    task_row = conn.execute(
        tasks_table.select().where(tasks_table.c.id == task_id)
    ).one()
    return record_row, task_row


def settle_delivery_step(
    engine: Engine,
    task_id: int,
    seq: int,
    *,
    action_id: int | None,
    result: dict[str, Any] | None = None,
    identity: dict[str, Any] | None = None,
    disposition: str | None = None,
    outcome: dict[str, Any],
    successor: tuple[str, str, str | None, dict[str, Any] | None] | None,
    terminal_result: str | None,
) -> tuple[StepRecord, Task]:
    """Land one privileged effect's journal result and the run's next state.

    The mirror of `resolve_gate` on the other side of the authorization: the
    action succeeded, and the step that asked for it finishes with the same
    write. If this ever *does* get interrupted before it runs, recovery finds a
    succeeded action whose step is still open and attaches that same result —
    it never dispatches a second effect to fill the gap, because the effect is
    already on record as having happened.
    """
    from ompire_daemon.registry.model_profiles import reserved_write
    from ompire_daemon.registry.ships import complete_action_in

    now = _now_iso()
    with reserved_write(engine) as conn:
        if action_id is not None and result is not None:
            complete_action_in(
                conn,
                action_id,
                result=result,
                identity=identity,
                disposition=disposition,
            )
        row = conn.execute(
            workflow_step_records.select()
            .where(workflow_step_records.c.task_id == task_id)
            .where(workflow_step_records.c.seq == seq)
        ).first()
        if row is None or row.status != "running" or row.kind != "delivery":
            raise WorkflowWaitConflictError(
                task_id, seq, row.seq if row is not None else None
            )
        record_row, task_row = _advance_in(
            conn,
            task_id,
            seq,
            now,
            outcome=outcome,
            successor=successor,
            terminal_result=terminal_result,
        )
    return _row_to_record(record_row), _row_to_task(task_row)


def resume_paused_attempt(
    engine: Engine, task_id: int, seq: int
) -> tuple[StepRecord, Task]:
    """Return one paused attempt to `running`, on the same row.

    The delivery counterpart of `retry_paused_step`, and deliberately not the
    same thing. A retry opens a *new* attempt, which is right for work that
    can simply be done again. A privileged action cannot: its attempt owns a
    write-ahead intent in the delivery journal and, possibly, a partially
    observed effect. Resuming the same row keeps that link, so the uniqueness
    that stops a second signature or a second push still applies.

    The pause's reason is cleared; the error it recorded stays, so history
    still says the attempt was interrupted and continued rather than having
    run cleanly.
    """
    from ompire_daemon.registry.model_profiles import reserved_write

    now = _now_iso()
    with reserved_write(engine) as conn:
        row = conn.execute(
            workflow_step_records.select()
            .where(workflow_step_records.c.task_id == task_id)
            .where(workflow_step_records.c.seq == seq)
        ).first()
        if row is None or row.status != "waiting" or row.pause_json is None:
            raise WorkflowWaitConflictError(
                task_id, seq, row.seq if row is not None else None
            )
        conn.execute(
            workflow_step_records.update()
            .where(workflow_step_records.c.task_id == task_id)
            .where(workflow_step_records.c.seq == seq)
            .values(status="running", pause_json=None, finished_at=None)
        )
        conn.execute(
            tasks_table.update()
            .where(tasks_table.c.id == task_id)
            .values(workflow_status="running", workflow_step=row.step, updated_at=now)
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


def retry_paused_step(
    engine: Engine,
    task_id: int,
    seq: int,
    *,
    target: tuple[str, str] | None = None,
    evidence: dict[str, Any] | None = None,
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
                evidence_json=(
                    json.dumps(evidence) if evidence is not None else None
                ),
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


def set_run_complete(engine: Engine, task_id: int, result: str | None) -> Task:
    """Land the run `complete` with the declared ending it reached.

    `result` is None for a format-1 run: those have no name for their ending,
    and inventing one would put a claim in the record the definition never
    made.
    """
    return _update(
        engine,
        task_id,
        workflow_status="complete",
        workflow_step=None,
        workflow_result=result,
    )


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
