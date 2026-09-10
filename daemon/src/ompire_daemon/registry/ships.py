"""Delivery registry: protected candidates, operator authorizations, write-ahead
action attempts, and the ordered decisions made about them.

Architecture: ADR-0032
(docs/adr/0032-bind-trusted-delivery-to-retained-candidates.md); durability
boundary: ADR-0016
(docs/adr/0016-persist-authority-bearing-task-history-and-provenance.md)

No ORM — Core only, mirroring `registry/reviews.py`. Three rules shape every
write here:

- **Journal before the effect.** An action row reaches `executing` and commits
  *before* anything signs, pushes, or calls the forge. A lost response is then
  a row that says what was attempted and against which exact refs, rather than
  silence that looks identical to "never started".
- **Result before the next effect.** An action's verified outcome and the
  eligibility it grants commit in one transaction, so a push can never be
  scheduled against a signing result nobody recorded.
- **Terminal prefixes are immutable.** A completed commit or push stays a
  successful fact when a later action fails. Extending a delivery to a further
  ending appends authority; it never rewrites what was already authorized.

Rows are history: they survive task archival — a cleaned-up task keeps the
evidence explaining what it published and under whose authorization — and are
deleted only by purge.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, Engine

from ompire_daemon.db import (
    deliveries,
    delivery_actions,
    delivery_authority_boundary,
    delivery_candidates,
    delivery_decisions,
)
from ompire_daemon.platform.transactions import reserved_write

# The three endings an operator can select. They name publishing effects, not
# workflow results: a workflow's own ending vocabulary (ADR-0029) is separate
# and deliberately not reused here.
ENDINGS = ("commit", "push", "pr")
# Which actions each ending authorizes, in order. A confirmation authorizes a
# prefix of this sequence and nothing beyond it.
ENDING_ACTIONS: dict[str, tuple[str, ...]] = {
    "commit": ("commit",),
    "push": ("commit", "push"),
    "pr": ("commit", "push", "pr"),
}
ACTION_KINDS = ("commit", "push", "pr")
MODES = ("squash", "retain")

# Delivery dispositions.
#
# `open` holds a draft and authorizes nothing. `authorized` has a confirmation
# and remaining work. `completed` reached its selected ending — and can still be
# *extended* to a further one, which appends authority and returns it to
# `authorized`. `blocked` is a safe stop: nothing is in an unknown state, and a
# fresh preview and confirmation may retry. `unresolved` means an effect's
# outcome could not be established; nothing dependent may run and cleanup is
# refused until an operator decision resolves it. `abandoned` records that the
# operator asked for no further authority.
DISPOSITIONS = (
    "open",
    "authorized",
    "completed",
    "blocked",
    "unresolved",
    "abandoned",
)
# A task may hold only one delivery in these states at a time.
NON_TERMINAL_DISPOSITIONS = ("open", "authorized", "blocked", "unresolved")

# Action phases. `failed` is only reachable when non-execution or a verified
# rollback was established; everything else uncertain lands in
# `needs_reconciliation`.
ACTION_PHASES = (
    "prepared",
    "executing",
    "succeeded",
    "failed",
    "needs_reconciliation",
)

DECISION_KINDS = (
    "authorize",
    "extend",
    "recheck",
    "adopt",
    "retry",
    "abandon",
    "block",
)


class DeliveryConflictError(Exception):
    """A request cannot be admitted against the delivery's current state."""

    def __init__(self, detail: str, *, task_id: int | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.task_id = task_id


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _dumps(value: Any) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True)


def _loads(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


# --- records ---------------------------------------------------------------


@dataclass(frozen=True)
class SourceCommit:
    """One commit in a retain range, as captured."""

    commit_id: str
    tree_id: str
    message: str
    parent_ids: tuple[str, ...]


@dataclass(frozen=True)
class CandidateRecord:
    """The exact publishable content a review graded and a signature covers."""

    candidate_id: str
    task_id: int
    base_branch: str
    base_commit: str
    original_head: str
    tree_id: str
    source_commits: tuple[SourceCommit, ...]
    dirty: bool
    storage_path: str | None
    created_at: str

    @property
    def commit_count(self) -> int:
        return len(self.source_commits)


@dataclass(frozen=True)
class ActionRecord:
    id: int
    delivery_id: int
    seq: int
    kind: str
    attempt: int
    request_key: str
    input_fingerprint: str
    phase: str
    expected: dict[str, Any] | None
    progress: dict[str, Any] | None
    identity: dict[str, Any] | None
    result: dict[str, Any] | None
    error: str | None
    created_at: str
    updated_at: str
    # The workflow delivery-step attempt this action belongs to, written with
    # the intent and before the effect. None is an operator-driven action,
    # never "some step, unknown".
    workflow_seq: int | None = None


@dataclass(frozen=True)
class DecisionRecord:
    id: int
    delivery_id: int
    action_id: int | None
    kind: str
    detail: dict[str, Any] | None
    note: str | None
    decided_at: str


@dataclass(frozen=True)
class DeliveryRecord:
    id: int
    task_id: int
    version: int
    workflow_revision: str | None
    candidate_id: str | None
    review_candidate_id: str | None
    mode: str | None
    ending: str | None
    commit_message: str | None
    pr_title: str | None
    pr_body: str | None
    routing: dict[str, Any] | None
    identity: dict[str, Any] | None
    authorized_at: str | None
    authorized_by: str | None
    request_key: str | None
    input_fingerprint: str | None
    draft: dict[str, Any] | None
    disposition: str
    blocked_reason: str | None
    created_at: str
    updated_at: str
    # Which workflow decision granted this authorization: the answered gate
    # attempt, the choice, and the review iteration the grant is bound to.
    # All None for an authorization made outside a workflow decision — which
    # is a fact about how it was granted, not a gap to be filled in.
    workflow_gate_seq: int | None = None
    workflow_choice_id: str | None = None
    review_seq: int | None = None
    actions: list[ActionRecord] = field(default_factory=list)
    decisions: list[DecisionRecord] = field(default_factory=list)

    @property
    def workflow_authorized(self) -> bool:
        """Whether a workflow decision is what granted this delivery."""
        return self.workflow_gate_seq is not None and self.workflow_choice_id is not None

    def action(self, kind: str) -> ActionRecord | None:
        """The latest attempt at `kind`, or None."""
        matches = [a for a in self.actions if a.kind == kind]
        return matches[-1] if matches else None

    def succeeded(self, kind: str) -> ActionRecord | None:
        matches = [a for a in self.actions if a.kind == kind and a.phase == "succeeded"]
        return matches[-1] if matches else None

    @property
    def unresolved_actions(self) -> list[ActionRecord]:
        return [
            a
            for a in self.actions
            if a.phase in ("executing", "needs_reconciliation")
        ]

    @property
    def authorized_actions(self) -> tuple[str, ...]:
        if self.ending is None:
            return ()
        return ENDING_ACTIONS[self.ending]

    @property
    def remaining_actions(self) -> tuple[str, ...]:
        return tuple(
            kind
            for kind in self.authorized_actions
            if self.succeeded(kind) is None
        )


def _row_to_candidate(row) -> CandidateRecord:
    raw = _loads(row.source_commits_json) or []
    commits = tuple(
        SourceCommit(
            commit_id=entry["commit_id"],
            tree_id=entry["tree_id"],
            message=entry["message"],
            parent_ids=tuple(entry.get("parent_ids", ())),
        )
        for entry in raw
    )
    return CandidateRecord(
        candidate_id=row.candidate_id,
        task_id=row.task_id,
        base_branch=row.base_branch,
        base_commit=row.base_commit,
        original_head=row.original_head,
        tree_id=row.tree_id,
        source_commits=commits,
        dirty=bool(row.dirty),
        storage_path=row.storage_path,
        created_at=row.created_at,
    )


def _row_to_action(row) -> ActionRecord:
    return ActionRecord(
        id=row.id,
        delivery_id=row.delivery_id,
        seq=row.seq,
        kind=row.kind,
        attempt=row.attempt,
        request_key=row.request_key,
        input_fingerprint=row.input_fingerprint,
        phase=row.phase,
        expected=_loads(row.expected_json),
        progress=_loads(row.progress_json),
        identity=_loads(row.identity_json),
        result=_loads(row.result_json),
        error=row.error,
        created_at=row.created_at,
        updated_at=row.updated_at,
        workflow_seq=row.workflow_seq,
    )


def _row_to_decision(row) -> DecisionRecord:
    return DecisionRecord(
        id=row.id,
        delivery_id=row.delivery_id,
        action_id=row.action_id,
        kind=row.kind,
        detail=_loads(row.detail_json),
        note=row.note,
        decided_at=row.decided_at,
    )


def _row_to_delivery(
    row, actions: list[ActionRecord], decisions: list[DecisionRecord]
) -> DeliveryRecord:
    return DeliveryRecord(
        id=row.id,
        task_id=row.task_id,
        version=row.version,
        workflow_revision=row.workflow_revision,
        candidate_id=row.candidate_id,
        review_candidate_id=row.review_candidate_id,
        mode=row.mode,
        ending=row.ending,
        commit_message=row.commit_message,
        pr_title=row.pr_title,
        pr_body=row.pr_body,
        routing=_loads(row.routing_json),
        identity=_loads(row.identity_json),
        authorized_at=row.authorized_at,
        authorized_by=row.authorized_by,
        request_key=row.request_key,
        input_fingerprint=row.input_fingerprint,
        draft=_loads(row.draft_json),
        disposition=row.disposition,
        blocked_reason=row.blocked_reason,
        created_at=row.created_at,
        updated_at=row.updated_at,
        workflow_gate_seq=row.workflow_gate_seq,
        workflow_choice_id=row.workflow_choice_id,
        review_seq=row.review_seq,
        actions=actions,
        decisions=decisions,
    )


# --- candidates ------------------------------------------------------------


def record_candidate(
    engine: Engine,
    *,
    candidate_id: str,
    task_id: int,
    base_branch: str,
    base_commit: str,
    original_head: str,
    tree_id: str,
    source_commits: tuple[SourceCommit, ...] | list[SourceCommit],
    dirty: bool,
    storage_path: str | None,
) -> CandidateRecord:
    """Persist the candidate manifest. Idempotent by content identity: the same
    workspace captured twice is the same candidate, which is exactly what makes
    "unchanged since review" checkable rather than guessed."""
    payload = _dumps(
        [
            {
                "commit_id": c.commit_id,
                "tree_id": c.tree_id,
                "message": c.message,
                "parent_ids": list(c.parent_ids),
            }
            for c in source_commits
        ]
    )
    now = _now_iso()
    with reserved_write(engine) as conn:
        existing = conn.execute(
            delivery_candidates.select().where(
                delivery_candidates.c.candidate_id == candidate_id
            )
        ).first()
        if existing is None:
            conn.execute(
                delivery_candidates.insert().values(
                    candidate_id=candidate_id,
                    task_id=task_id,
                    base_branch=base_branch,
                    base_commit=base_commit,
                    original_head=original_head,
                    tree_id=tree_id,
                    source_commits_json=payload,
                    dirty=1 if dirty else 0,
                    storage_path=storage_path,
                    created_at=now,
                )
            )
        elif storage_path is not None and existing.storage_path != storage_path:
            # Re-capture after the previous staging repository was removed:
            # the identity is unchanged, only where its objects now live.
            conn.execute(
                delivery_candidates.update()
                .where(delivery_candidates.c.candidate_id == candidate_id)
                .values(storage_path=storage_path, original_head=original_head)
            )
    record = get_candidate(engine, candidate_id)
    assert record is not None
    return record


def get_candidate(engine: Engine, candidate_id: str) -> CandidateRecord | None:
    with engine.connect() as conn:
        row = conn.execute(
            delivery_candidates.select().where(
                delivery_candidates.c.candidate_id == candidate_id
            )
        ).first()
    return None if row is None else _row_to_candidate(row)


def list_task_candidates(engine: Engine, task_id: int) -> list[CandidateRecord]:
    with engine.connect() as conn:
        rows = conn.execute(
            delivery_candidates.select()
            .where(delivery_candidates.c.task_id == task_id)
            .order_by(delivery_candidates.c.created_at)
        ).all()
    return [_row_to_candidate(row) for row in rows]


def clear_candidate_storage(engine: Engine, candidate_id: str) -> None:
    """Forget where a candidate's objects lived; the manifest stays as
    evidence of what was reviewed and signed."""
    with engine.begin() as conn:
        conn.execute(
            delivery_candidates.update()
            .where(delivery_candidates.c.candidate_id == candidate_id)
            .values(storage_path=None)
        )


# --- deliveries ------------------------------------------------------------


def _next_version(conn: Connection, task_id: int) -> int:
    row = conn.execute(
        deliveries.select()
        .where(deliveries.c.task_id == task_id)
        .order_by(deliveries.c.version.desc())
        .limit(1)
    ).first()
    return (row.version + 1) if row is not None else 1


def task_version(engine: Engine, task_id: int) -> int:
    """The task's current delivery projection version; 0 when it has none."""
    with engine.connect() as conn:
        row = conn.execute(
            deliveries.select()
            .where(deliveries.c.task_id == task_id)
            .order_by(deliveries.c.version.desc())
            .limit(1)
        ).first()
    return row.version if row is not None else 0


def open_delivery(
    engine: Engine, task_id: int, *, workflow_revision: str | None = None
) -> DeliveryRecord:
    """Return the task's non-terminal delivery, creating an `open` one if it has
    none. The reservation is what makes "one delivery at a time" true for
    concurrent callers rather than merely usually true."""
    now = _now_iso()
    with reserved_write(engine) as conn:
        row = conn.execute(
            deliveries.select()
            .where(deliveries.c.task_id == task_id)
            .where(deliveries.c.disposition.in_(NON_TERMINAL_DISPOSITIONS))
            .order_by(deliveries.c.id.desc())
            .limit(1)
        ).first()
        if row is not None:
            delivery_id = row.id
        else:
            result = conn.execute(
                deliveries.insert().values(
                    task_id=task_id,
                    version=_next_version(conn, task_id),
                    workflow_revision=workflow_revision,
                    disposition="open",
                    created_at=now,
                    updated_at=now,
                )
            )
            inserted = result.inserted_primary_key
            assert inserted is not None
            delivery_id = int(inserted[0])
    record = get_delivery(engine, delivery_id)
    assert record is not None
    return record


def get_delivery(engine: Engine, delivery_id: int) -> DeliveryRecord | None:
    with engine.connect() as conn:
        row = conn.execute(
            deliveries.select().where(deliveries.c.id == delivery_id)
        ).first()
        if row is None:
            return None
        return _row_to_delivery(row, *_children(conn, delivery_id))


def _children(
    conn: Connection, delivery_id: int
) -> tuple[list[ActionRecord], list[DecisionRecord]]:
    action_rows = conn.execute(
        delivery_actions.select()
        .where(delivery_actions.c.delivery_id == delivery_id)
        .order_by(delivery_actions.c.seq)
    ).all()
    decision_rows = conn.execute(
        delivery_decisions.select()
        .where(delivery_decisions.c.delivery_id == delivery_id)
        .order_by(delivery_decisions.c.id)
    ).all()
    return (
        [_row_to_action(r) for r in action_rows],
        [_row_to_decision(r) for r in decision_rows],
    )


def get_active_delivery(engine: Engine, task_id: int) -> DeliveryRecord | None:
    """The task's non-terminal delivery, if it has one."""
    with engine.connect() as conn:
        row = conn.execute(
            deliveries.select()
            .where(deliveries.c.task_id == task_id)
            .where(deliveries.c.disposition.in_(NON_TERMINAL_DISPOSITIONS))
            .order_by(deliveries.c.id.desc())
            .limit(1)
        ).first()
        if row is None:
            return None
        return _row_to_delivery(row, *_children(conn, row.id))


def get_latest_delivery(engine: Engine, task_id: int) -> DeliveryRecord | None:
    """The task's most recent delivery in any disposition — including a
    completed one, which a later push or PR can still extend."""
    with engine.connect() as conn:
        row = conn.execute(
            deliveries.select()
            .where(deliveries.c.task_id == task_id)
            .order_by(deliveries.c.id.desc())
            .limit(1)
        ).first()
        if row is None:
            return None
        return _row_to_delivery(row, *_children(conn, row.id))


def list_deliveries(engine: Engine, task_id: int) -> list[DeliveryRecord]:
    with engine.connect() as conn:
        rows = conn.execute(
            deliveries.select()
            .where(deliveries.c.task_id == task_id)
            .order_by(deliveries.c.id)
        ).all()
        return [_row_to_delivery(row, *_children(conn, row.id)) for row in rows]


def list_unresolved_deliveries(engine: Engine) -> list[DeliveryRecord]:
    """Every delivery a restart must inspect before the task is writable: one
    whose action was executing when the daemon stopped, or which already
    recorded an unresolved effect."""
    with engine.connect() as conn:
        rows = conn.execute(
            deliveries.select()
            .where(deliveries.c.disposition.in_(("authorized", "unresolved")))
            .order_by(deliveries.c.id)
        ).all()
        return [_row_to_delivery(row, *_children(conn, row.id)) for row in rows]


def _bump(conn: Connection, delivery_id: int, **values: Any) -> None:
    row = conn.execute(
        deliveries.select().where(deliveries.c.id == delivery_id)
    ).first()
    assert row is not None
    values.setdefault("version", _next_version(conn, row.task_id))
    conn.execute(
        deliveries.update()
        .where(deliveries.c.id == delivery_id)
        .values(updated_at=_now_iso(), **values)
    )


def save_draft(
    engine: Engine, delivery_id: int, draft: dict[str, Any] | None
) -> DeliveryRecord:
    """Persist the publication draft. Inert text: it neither authorizes nor
    selects anything, and an operator may replace every field by hand."""
    with reserved_write(engine) as conn:
        _bump(conn, delivery_id, draft_json=_dumps(draft))
    record = get_delivery(engine, delivery_id)
    assert record is not None
    return record


def authorize_delivery(
    engine: Engine,
    delivery_id: int,
    *,
    expected_version: int | None,
    candidate_id: str,
    review_candidate_id: str | None,
    mode: str,
    ending: str,
    commit_message: str | None,
    pr_title: str | None,
    pr_body: str | None,
    routing: dict[str, Any],
    identity: dict[str, Any],
    request_key: str,
    input_fingerprint: str,
    authorized_by: str = "operator",
    workflow_gate_seq: int | None = None,
    workflow_choice_id: str | None = None,
    review_seq: int | None = None,
) -> DeliveryRecord:
    """Accept one operator authorization.

    The version check and the write share a reservation, so a confirmation that
    raced another change is refused rather than applied to a delivery the
    operator was not looking at. An exact replay — same request key and the same
    normalized inputs — returns the existing record instead of authorizing a
    second time; a *conflicting* reuse of the key is a refusal.
    """
    with reserved_write(engine) as conn:
        authorize_delivery_in(
            conn,
            delivery_id,
            expected_version=expected_version,
            candidate_id=candidate_id,
            review_candidate_id=review_candidate_id,
            mode=mode,
            ending=ending,
            commit_message=commit_message,
            pr_title=pr_title,
            pr_body=pr_body,
            routing=routing,
            identity=identity,
            request_key=request_key,
            input_fingerprint=input_fingerprint,
            authorized_by=authorized_by,
            workflow_gate_seq=workflow_gate_seq,
            workflow_choice_id=workflow_choice_id,
            review_seq=review_seq,
        )
    record = get_delivery(engine, delivery_id)
    assert record is not None
    return record


def authorize_delivery_in(
    conn: Connection,
    delivery_id: int,
    *,
    expected_version: int | None,
    candidate_id: str,
    review_candidate_id: str | None,
    mode: str,
    ending: str,
    commit_message: str | None,
    pr_title: str | None,
    pr_body: str | None,
    routing: dict[str, Any],
    identity: dict[str, Any],
    request_key: str,
    input_fingerprint: str,
    authorized_by: str = "operator",
    workflow_gate_seq: int | None = None,
    workflow_choice_id: str | None = None,
    review_seq: int | None = None,
) -> DeliveryRecord:
    """The authorization write, on a caller's reserved connection.

    Connection-scoped so a workflow decision and the grant it produces land
    in *one* transaction. Committing the answer first and the grant afterwards
    would leave a crash window in which a person has approved publication and
    nothing on record permits it — or worse, the reverse.
    """
    if mode not in MODES:
        raise DeliveryConflictError(f"delivery mode {mode!r} is not supported")
    if ending not in ENDINGS:
        raise DeliveryConflictError(f"delivery ending {ending!r} is not supported")
    now = _now_iso()
    row = conn.execute(
        deliveries.select().where(deliveries.c.id == delivery_id)
    ).first()
    if row is None:
        raise DeliveryConflictError(f"delivery {delivery_id} does not exist")
    if row.authorized_at is not None:
        if (
            row.request_key == request_key
            and row.input_fingerprint == input_fingerprint
        ):
            return _row_to_delivery(row, *_children(conn, delivery_id))
        raise DeliveryConflictError(
            "this delivery is already authorized; changing the ending, mode, "
            "metadata, or content needs a new preview and confirmation"
        )
    if expected_version is not None and row.version != expected_version:
        raise DeliveryConflictError(
            f"delivery changed since it was previewed "
            f"(version {row.version}, expected {expected_version})"
        )
    conflicting = conn.execute(
        deliveries.select()
        .where(deliveries.c.task_id == row.task_id)
        .where(deliveries.c.request_key == request_key)
        .where(deliveries.c.id != delivery_id)
    ).first()
    if conflicting is not None:
        raise DeliveryConflictError(
            "that request identifier already authorized a different delivery"
        )
    _bump(
        conn,
        delivery_id,
        candidate_id=candidate_id,
        review_candidate_id=review_candidate_id,
        mode=mode,
        ending=ending,
        commit_message=commit_message,
        pr_title=pr_title,
        pr_body=pr_body,
        routing_json=_dumps(routing),
        identity_json=_dumps(identity),
        authorized_at=now,
        authorized_by=authorized_by,
        request_key=request_key,
        input_fingerprint=input_fingerprint,
        workflow_gate_seq=workflow_gate_seq,
        workflow_choice_id=workflow_choice_id,
        review_seq=review_seq,
        disposition="authorized",
        blocked_reason=None,
    )
    conn.execute(
        delivery_decisions.insert().values(
            delivery_id=delivery_id,
            action_id=None,
            kind="authorize",
            detail_json=_dumps(
                {
                    "ending": ending,
                    "mode": mode,
                    "candidate_id": candidate_id,
                    "review_candidate_id": review_candidate_id,
                    "actions": list(ENDING_ACTIONS[ending]),
                    "workflow_gate_seq": workflow_gate_seq,
                    "workflow_choice_id": workflow_choice_id,
                    "review_seq": review_seq,
                }
            ),
            note=None,
            decided_at=now,
        )
    )
    record = _row_to_delivery(
        conn.execute(
            deliveries.select().where(deliveries.c.id == delivery_id)
        ).one(),
        *_children(conn, delivery_id),
    )
    return record


def reauthorize_delivery(
    engine: Engine,
    delivery_id: int,
    *,
    expected_version: int,
    candidate_id: str,
    review_candidate_id: str | None,
    mode: str,
    ending: str,
    commit_message: str | None,
    pr_title: str | None,
    pr_body: str | None,
    routing: dict[str, Any],
    identity: dict[str, Any],
    request_key: str,
    input_fingerprint: str,
    authorized_by: str = "operator",
) -> DeliveryRecord:
    """Re-authorize a delivery that stopped before anything succeeded.

    The immutability rule protects *terminal prefixes* — a completed commit or
    push stays exactly as recorded. A delivery whose first action was refused
    has no such prefix, so a fresh confirmation may correct the ending, the
    mode, the metadata, or the content it names rather than stranding the
    operator on an authorization that can no longer run. The new confirmation
    is appended as its own decision; the refused attempt keeps its own record.

    An attempt that is executing or unresolved blocks this outright. The
    recorded inputs are what a running effect and its recovery are read against,
    so they cannot change under either — and this refuses locally rather than
    relying on the workspace guard and startup reconciliation to have refused
    first.
    """
    if mode not in MODES:
        raise DeliveryConflictError(f"delivery mode {mode!r} is not supported")
    if ending not in ENDINGS:
        raise DeliveryConflictError(f"delivery ending {ending!r} is not supported")
    now = _now_iso()
    with reserved_write(engine) as conn:
        row = conn.execute(
            deliveries.select().where(deliveries.c.id == delivery_id)
        ).first()
        if row is None:
            raise DeliveryConflictError(f"delivery {delivery_id} does not exist")
        settled = conn.execute(
            delivery_actions.select()
            .where(delivery_actions.c.delivery_id == delivery_id)
            .where(
                delivery_actions.c.phase.in_(
                    ("succeeded", "executing", "needs_reconciliation")
                )
            )
        ).first()
        if settled is not None:
            raise DeliveryConflictError(
                f"this delivery has an action that is {settled.phase}; its "
                "authorization cannot be replaced"
            )
        if row.version != expected_version:
            raise DeliveryConflictError(
                f"delivery changed since it was previewed "
                f"(version {row.version}, expected {expected_version})"
            )
        _bump(
            conn,
            delivery_id,
            candidate_id=candidate_id,
            review_candidate_id=review_candidate_id,
            mode=mode,
            ending=ending,
            commit_message=commit_message,
            pr_title=pr_title,
            pr_body=pr_body,
            routing_json=_dumps(routing),
            identity_json=_dumps(identity),
            authorized_at=now,
            authorized_by=authorized_by,
            request_key=request_key,
            input_fingerprint=input_fingerprint,
            disposition="authorized",
            blocked_reason=None,
        )
        conn.execute(
            delivery_decisions.insert().values(
                delivery_id=delivery_id,
                action_id=None,
                kind="authorize",
                detail_json=_dumps(
                    {
                        "ending": ending,
                        "mode": mode,
                        "candidate_id": candidate_id,
                        "review_candidate_id": review_candidate_id,
                        "actions": list(ENDING_ACTIONS[ending]),
                        "replaces_refused_attempt": True,
                    }
                ),
                note=None,
                decided_at=now,
            )
        )
    record = get_delivery(engine, delivery_id)
    assert record is not None
    return record


def resume_delivery(
    engine: Engine,
    delivery_id: int,
    *,
    expected_version: int,
    request_key: str,
    input_fingerprint: str,
) -> DeliveryRecord:
    """Re-admit the remaining actions of a delivery whose completed prefix
    stands and whose next action was safely refused.

    Nothing about the original authorization changes — the ending, the mode,
    the content, and the completed results are exactly as recorded. This says
    the operator looked at the refusal and asked for the rest again.
    """
    now = _now_iso()
    with reserved_write(engine) as conn:
        row = conn.execute(
            deliveries.select().where(deliveries.c.id == delivery_id)
        ).first()
        if row is None:
            raise DeliveryConflictError(f"delivery {delivery_id} does not exist")
        if row.version != expected_version:
            raise DeliveryConflictError(
                f"delivery changed since it was previewed "
                f"(version {row.version}, expected {expected_version})"
            )
        _bump(conn, delivery_id, disposition="authorized", blocked_reason=None)
        conn.execute(
            delivery_decisions.insert().values(
                delivery_id=delivery_id,
                action_id=None,
                kind="retry",
                detail_json=_dumps(
                    {
                        "request_key": request_key,
                        "input_fingerprint": input_fingerprint,
                        "ending": row.ending,
                    }
                ),
                note=None,
                decided_at=now,
            )
        )
    record = get_delivery(engine, delivery_id)
    assert record is not None
    return record


def extend_delivery(
    engine: Engine,
    delivery_id: int,
    *,
    expected_version: int,
    ending: str,
    pr_title: str | None,
    pr_body: str | None,
    request_key: str,
    input_fingerprint: str,
    identity: dict[str, Any],
    authorized_by: str = "operator",
) -> DeliveryRecord:
    """Authorize a further ending for a delivery whose earlier prefix completed.

    The previous authorization is not rewritten: its record of what the operator
    confirmed the first time stays exactly as it was, and this appends the new
    authority as its own decision. Only PR metadata may be supplied here,
    because that is the only input a later action needs and did not have.
    """
    if ending not in ENDINGS:
        raise DeliveryConflictError(f"delivery ending {ending!r} is not supported")
    now = _now_iso()
    with reserved_write(engine) as conn:
        row = conn.execute(
            deliveries.select().where(deliveries.c.id == delivery_id)
        ).first()
        if row is None:
            raise DeliveryConflictError(f"delivery {delivery_id} does not exist")
        if row.ending is None:
            raise DeliveryConflictError("this delivery was never authorized")
        # Replay is checked before the version, deliberately: a double submit
        # carries the version it was sent with, and re-answering it identically
        # is the whole point of a request key.
        existing = conn.execute(
            delivery_decisions.select()
            .where(delivery_decisions.c.delivery_id == delivery_id)
            .where(delivery_decisions.c.kind == "extend")
        ).all()
        for decision in existing:
            detail = _loads(decision.detail_json) or {}
            if detail.get("request_key") == request_key:
                if detail.get("input_fingerprint") == input_fingerprint:
                    return _row_to_delivery(row, *_children(conn, delivery_id))
                raise DeliveryConflictError(
                    "that request identifier already extended this delivery "
                    "with different inputs"
                )
        if row.version != expected_version:
            raise DeliveryConflictError(
                f"delivery changed since it was previewed "
                f"(version {row.version}, expected {expected_version})"
            )
        if len(ENDING_ACTIONS[ending]) <= len(ENDING_ACTIONS[row.ending]):
            raise DeliveryConflictError(
                f"{ending!r} is not further than the authorized ending {row.ending!r}"
            )
        values: dict[str, Any] = {
            "ending": ending,
            "disposition": "authorized",
            "blocked_reason": None,
            "identity_json": _dumps(identity),
        }
        if pr_title is not None:
            values["pr_title"] = pr_title
        if pr_body is not None:
            values["pr_body"] = pr_body
        _bump(conn, delivery_id, **values)
        conn.execute(
            delivery_decisions.insert().values(
                delivery_id=delivery_id,
                action_id=None,
                kind="extend",
                detail_json=_dumps(
                    {
                        "ending": ending,
                        "request_key": request_key,
                        "input_fingerprint": input_fingerprint,
                        "actions": list(ENDING_ACTIONS[ending]),
                    }
                ),
                note=None,
                decided_at=now,
            )
        )
    record = get_delivery(engine, delivery_id)
    assert record is not None
    return record


def set_disposition(
    engine: Engine,
    delivery_id: int,
    disposition: str,
    *,
    blocked_reason: str | None = None,
) -> DeliveryRecord:
    if disposition not in DISPOSITIONS:
        raise DeliveryConflictError(f"unknown disposition {disposition!r}")
    with reserved_write(engine) as conn:
        _bump(
            conn,
            delivery_id,
            disposition=disposition,
            blocked_reason=blocked_reason,
        )
    record = get_delivery(engine, delivery_id)
    assert record is not None
    return record


# --- action attempts -------------------------------------------------------


def prepare_action(
    engine: Engine,
    delivery_id: int,
    *,
    kind: str,
    request_key: str,
    input_fingerprint: str,
    expected: dict[str, Any],
    identity: dict[str, Any] | None = None,
    workflow_seq: int | None = None,
) -> ActionRecord:
    """Write the attempt's intent before anything runs.

    An exact replay of the same request key and inputs returns the existing
    attempt rather than opening a second one; a conflicting reuse is refused.
    A new attempt is refused outright while a previous attempt at the same kind
    is still executing or unresolved — that is the rule that keeps a lost
    response from becoming permission to sign or push again.

    `workflow_seq` is the run attempt asking for the effect, and at most one
    *live* action may carry it. That is what makes a re-driven step adopt its
    own action instead of dispatching a second one: the second insert is
    refused rather than merely discouraged, and the check happens here, under
    the reservation, rather than being left to a constraint this database does
    not enforce for foreign keys. An attempt whose effect is proven not to
    have happened is excluded, because continuing after one is a new attempt
    at the same step, not a second effect.
    """
    if kind not in ACTION_KINDS:
        raise DeliveryConflictError(f"unknown delivery action {kind!r}")
    now = _now_iso()
    with reserved_write(engine) as conn:
        rows = conn.execute(
            delivery_actions.select()
            .where(delivery_actions.c.delivery_id == delivery_id)
            .order_by(delivery_actions.c.seq)
        ).all()
        for row in rows:
            if row.request_key != request_key:
                continue
            if (
                row.kind == kind
                and row.input_fingerprint == input_fingerprint
                and row.phase == "prepared"
            ):
                return _row_to_action(row)
            raise DeliveryConflictError(
                "that request identifier already started a delivery action"
            )
        if workflow_seq is not None:
            for row in rows:
                if row.workflow_seq != workflow_seq or row.phase == "failed":
                    # A `failed` attempt is one whose effect is *proven* not to
                    # have happened. Continuing after that is a fresh attempt
                    # for the same step, which is the whole point of a
                    # continuation; both stay on record.
                    continue
                if row.kind == kind and row.phase == "prepared":
                    return _row_to_action(row)
                raise DeliveryConflictError(
                    f"workflow step {workflow_seq} already has a delivery "
                    f"action ({row.kind}, {row.phase}); a step performs its "
                    "effect once"
                )
        same_kind = [r for r in rows if r.kind == kind]
        for row in same_kind:
            if row.phase in ("executing", "needs_reconciliation"):
                raise DeliveryConflictError(
                    f"the previous {kind} attempt has an unresolved outcome; "
                    "reconcile it before starting another"
                )
            if row.phase == "succeeded":
                raise DeliveryConflictError(
                    f"{kind} already completed for this delivery"
                )
        seq = (rows[-1].seq + 1) if rows else 1
        result = conn.execute(
            delivery_actions.insert().values(
                delivery_id=delivery_id,
                seq=seq,
                kind=kind,
                attempt=len(same_kind) + 1,
                request_key=request_key,
                input_fingerprint=input_fingerprint,
                phase="prepared",
                expected_json=_dumps(expected),
                identity_json=_dumps(identity),
                workflow_seq=workflow_seq,
                created_at=now,
                updated_at=now,
            )
        )
        inserted = result.inserted_primary_key
        assert inserted is not None
        action_id = int(inserted[0])
        _bump(conn, delivery_id)
    return _require_action(engine, action_id)


def _require_action(engine: Engine, action_id: int) -> ActionRecord:
    with engine.connect() as conn:
        row = conn.execute(
            delivery_actions.select().where(delivery_actions.c.id == action_id)
        ).first()
    assert row is not None
    return _row_to_action(row)


def get_action(engine: Engine, action_id: int) -> ActionRecord | None:
    with engine.connect() as conn:
        row = conn.execute(
            delivery_actions.select().where(delivery_actions.c.id == action_id)
        ).first()
    return None if row is None else _row_to_action(row)


def mark_action_executing(
    engine: Engine, action_id: int, *, expected: dict[str, Any] | None = None
) -> ActionRecord:
    """Commit the executing marker *before* spawning the effect."""
    values: dict[str, Any] = {"phase": "executing", "updated_at": _now_iso()}
    if expected is not None:
        values["expected_json"] = _dumps(expected)
    with reserved_write(engine) as conn:
        row = conn.execute(
            delivery_actions.select().where(delivery_actions.c.id == action_id)
        ).first()
        if row is None:
            raise DeliveryConflictError(f"delivery action {action_id} does not exist")
        if row.phase != "prepared":
            raise DeliveryConflictError(
                f"delivery action {action_id} is {row.phase}, not prepared"
            )
        conn.execute(
            delivery_actions.update()
            .where(delivery_actions.c.id == action_id)
            .values(**values)
        )
        _bump(conn, row.delivery_id)
    return _require_action(engine, action_id)


def record_action_progress(
    engine: Engine, action_id: int, progress: dict[str, Any]
) -> ActionRecord:
    """Record partial evidence — the signatures produced so far, the refs
    already written — so an interruption is inspectable rather than opaque."""
    with engine.begin() as conn:
        conn.execute(
            delivery_actions.update()
            .where(delivery_actions.c.id == action_id)
            .values(progress_json=_dumps(progress), updated_at=_now_iso())
        )
    return _require_action(engine, action_id)


def complete_action(
    engine: Engine,
    action_id: int,
    *,
    result: dict[str, Any],
    identity: dict[str, Any] | None = None,
    disposition: str | None = None,
) -> DeliveryRecord:
    """Land a verified outcome and the delivery state it produces in one
    transaction, so no dependent action can ever be scheduled against a result
    that was not committed first."""
    with reserved_write(engine) as conn:
        delivery_id = complete_action_in(
            conn,
            action_id,
            result=result,
            identity=identity,
            disposition=disposition,
        )
    record = get_delivery(engine, delivery_id)
    assert record is not None
    return record


def complete_action_in(
    conn: Connection,
    action_id: int,
    *,
    result: dict[str, Any],
    identity: dict[str, Any] | None = None,
    disposition: str | None = None,
) -> int:
    """The success write, on a caller's reserved connection.

    Connection-scoped so a workflow-owned action can land its journal result
    *and* the run's step transition together. A commit between the two is the
    window where an effect has happened and the run does not know it, which is
    exactly the state recovery would otherwise have to guess its way out of.
    """
    now = _now_iso()
    row = conn.execute(
        delivery_actions.select().where(delivery_actions.c.id == action_id)
    ).first()
    if row is None:
        raise DeliveryConflictError(f"delivery action {action_id} does not exist")
    values: dict[str, Any] = {
        "phase": "succeeded",
        "result_json": _dumps(result),
        "error": None,
        "updated_at": now,
    }
    if identity is not None:
        values["identity_json"] = _dumps(identity)
    conn.execute(
        delivery_actions.update()
        .where(delivery_actions.c.id == action_id)
        .values(**values)
    )
    updates: dict[str, Any] = {}
    if disposition is not None:
        updates["disposition"] = disposition
        updates["blocked_reason"] = None
    _bump(conn, row.delivery_id, **updates)
    return int(row.delivery_id)


def fail_action(
    engine: Engine,
    action_id: int,
    *,
    error: str,
    blocked_reason: str | None = None,
) -> DeliveryRecord:
    """Record a failure whose non-execution (or verified rollback) is
    established. Anything less certain belongs in `flag_action_unresolved`."""
    now = _now_iso()
    with reserved_write(engine) as conn:
        row = conn.execute(
            delivery_actions.select().where(delivery_actions.c.id == action_id)
        ).first()
        if row is None:
            raise DeliveryConflictError(f"delivery action {action_id} does not exist")
        conn.execute(
            delivery_actions.update()
            .where(delivery_actions.c.id == action_id)
            .values(phase="failed", error=error, updated_at=now)
        )
        _bump(
            conn,
            row.delivery_id,
            disposition="blocked",
            blocked_reason=blocked_reason or error,
        )
        conn.execute(
            delivery_decisions.insert().values(
                delivery_id=row.delivery_id,
                action_id=action_id,
                kind="block",
                detail_json=_dumps({"kind": row.kind, "error": error}),
                note=None,
                decided_at=now,
            )
        )
    record = get_delivery(engine, row.delivery_id)
    assert record is not None
    return record


def flag_action_unresolved(
    engine: Engine,
    action_id: int,
    *,
    error: str,
    evidence: dict[str, Any] | None = None,
) -> DeliveryRecord:
    """Record that an effect's outcome could not be established.

    This is never a failure and never a success. Nothing dependent may run,
    cleanup is refused, and the operator is offered a recheck, a verified
    adoption, a retry only once non-execution is proven, or an explicit
    abandonment that leaves the effect on record as still unknown.
    """
    now = _now_iso()
    with reserved_write(engine) as conn:
        row = conn.execute(
            delivery_actions.select().where(delivery_actions.c.id == action_id)
        ).first()
        if row is None:
            raise DeliveryConflictError(f"delivery action {action_id} does not exist")
        progress = _loads(row.progress_json) or {}
        if evidence is not None:
            progress = {**progress, "evidence": evidence}
        conn.execute(
            delivery_actions.update()
            .where(delivery_actions.c.id == action_id)
            .values(
                phase="needs_reconciliation",
                error=error,
                progress_json=_dumps(progress),
                updated_at=now,
            )
        )
        _bump(
            conn,
            row.delivery_id,
            disposition="unresolved",
            blocked_reason=error,
        )
    record = get_delivery(engine, row.delivery_id)
    assert record is not None
    return record


def append_decision(
    engine: Engine,
    delivery_id: int,
    *,
    kind: str,
    action_id: int | None = None,
    detail: dict[str, Any] | None = None,
    note: str | None = None,
) -> DeliveryRecord:
    if kind not in DECISION_KINDS:
        raise DeliveryConflictError(f"unknown delivery decision {kind!r}")
    with reserved_write(engine) as conn:
        conn.execute(
            delivery_decisions.insert().values(
                delivery_id=delivery_id,
                action_id=action_id,
                kind=kind,
                detail_json=_dumps(detail),
                note=note,
                decided_at=_now_iso(),
            )
        )
        _bump(conn, delivery_id)
    record = get_delivery(engine, delivery_id)
    assert record is not None
    return record


def resolve_action(
    engine: Engine,
    action_id: int,
    *,
    phase: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    disposition: str,
    blocked_reason: str | None = None,
    decision: str,
    note: str | None = None,
    detail: dict[str, Any] | None = None,
) -> DeliveryRecord:
    """Apply one reconciliation decision to one attempt, together with the
    delivery state it produces and the immutable record of who decided it."""
    if phase not in ACTION_PHASES:
        raise DeliveryConflictError(f"unknown action phase {phase!r}")
    now = _now_iso()
    with reserved_write(engine) as conn:
        row = conn.execute(
            delivery_actions.select().where(delivery_actions.c.id == action_id)
        ).first()
        if row is None:
            raise DeliveryConflictError(f"delivery action {action_id} does not exist")
        conn.execute(
            delivery_actions.update()
            .where(delivery_actions.c.id == action_id)
            .values(
                phase=phase,
                result_json=_dumps(result) if result is not None else row.result_json,
                error=error,
                updated_at=now,
            )
        )
        conn.execute(
            delivery_decisions.insert().values(
                delivery_id=row.delivery_id,
                action_id=action_id,
                kind=decision,
                detail_json=_dumps(detail),
                note=note,
                decided_at=now,
            )
        )
        _bump(
            conn,
            row.delivery_id,
            disposition=disposition,
            blocked_reason=blocked_reason,
        )
    record = get_delivery(engine, row.delivery_id)
    assert record is not None
    return record


# --- purge -----------------------------------------------------------------


# --- the pre-upgrade authority boundary ------------------------------------


@dataclass(frozen=True)
class AuthorityBoundary:
    """Where history ends: the last delivery and action ids that existed
    before workflows owned publication authority."""

    max_delivery_id: int
    max_action_id: int
    recorded_at: str


def authority_boundary(engine: Engine) -> AuthorityBoundary | None:
    """The recorded upgrade boundary, or None on a database without one."""
    with engine.connect() as conn:
        row = conn.execute(
            delivery_authority_boundary.select().where(
                delivery_authority_boundary.c.id == 1
            )
        ).first()
    if row is None:
        return None
    return AuthorityBoundary(
        max_delivery_id=row.max_delivery_id,
        max_action_id=row.max_action_id,
        recorded_at=row.recorded_at,
    )


def is_pre_upgrade_grant(
    boundary: AuthorityBoundary | None, delivery: DeliveryRecord
) -> bool:
    """Whether this really is authority granted before the upgrade.

    Three things must hold together, and none of them is sufficient alone: the
    row predates the boundary, it carries a genuine authorization, and it has
    no workflow links — because a delivery created afterwards has an id above
    the boundary no matter what its links say. This is the only reason a
    workflow with no delivery vocabulary may still finish an action.
    """
    if boundary is None:
        return False
    return (
        delivery.id <= boundary.max_delivery_id
        and delivery.authorized_at is not None
        and delivery.ending is not None
        and not delivery.workflow_authorized
    )


def delete_task_deliveries(engine: Engine, task_id: int) -> list[str]:
    """Delete a task's delivery history in its own transaction.

    Kept for callers that own no wider transaction. Task purge uses
    `delete_task_deliveries_on` instead, because its refusal checks and every
    one of its deletions have to share a single write reservation (ADR-0034).
    """
    with engine.begin() as conn:
        return delete_task_deliveries_on(conn, task_id)


def delete_task_deliveries_on(conn: Connection, task_id: int) -> list[str]:
    """Delete a task's delivery history on the caller's connection.

    Returns the candidate storage paths that are now unreferenced, so the
    caller can remove them from disk. Cleanup deliberately retains all of this
    — a cleaned-up task keeps the record of what it published and under whose
    authorization (ADR-0016). Child rows are deleted explicitly because this
    connection does not enable SQLite foreign-key enforcement, so a cascade
    cannot be assumed.
    """
    rows = conn.execute(
        deliveries.select().where(deliveries.c.task_id == task_id)
    ).all()
    ids = [row.id for row in rows]
    if ids:
        conn.execute(
            delivery_decisions.delete().where(
                delivery_decisions.c.delivery_id.in_(ids)
            )
        )
        conn.execute(
            delivery_actions.delete().where(
                delivery_actions.c.delivery_id.in_(ids)
            )
        )
        conn.execute(deliveries.delete().where(deliveries.c.task_id == task_id))
    candidate_rows = conn.execute(
        delivery_candidates.select().where(
            delivery_candidates.c.task_id == task_id
        )
    ).all()
    paths = [row.storage_path for row in candidate_rows if row.storage_path]
    conn.execute(
        delivery_candidates.delete().where(
            delivery_candidates.c.task_id == task_id
        )
    )
    return paths
