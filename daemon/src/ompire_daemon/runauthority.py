"""What a task's pinned procedure currently permits, resolved in one place.

Architecture: ADR-0033
(docs/adr/0033-scope-trusted-delivery-authority-to-the-workflow-run.md)

Every privileged operation on a task — review, a local signed commit, a push,
a pull request — is asked for by *someone*: the run itself, a REST request, a
direct call to a manager. This module answers, for all of them, the same
question in the same way: given the revision this task accepted, where the run
actually is, what was answered, and what the delivery journal already records,
which operations may happen right now, and why not the others.

Three things make it a resolver rather than a route check.

It reads only durable state. The pinned definition, the current attempt, the
persisted gate question and its committed decision, the bound review iteration,
the delivery rows. A caller supplies identities and expected versions; it never
supplies a verdict, and nothing here trusts a flag that says "this is allowed".

It refuses by default. A definition with no delivery vocabulary grants no
publication, whatever the workflow is called, whatever an agent reported, and
however an old approval reads. The one exception is narrow and dated: an
authorization genuinely made before this format existed may still finish the
prefix it was actually granted, and `registry.ships.is_pre_upgrade_grant` is
the only thing that can say so.

And its refusals are the projection. The same reasons that stop an operation
are what Task detail and Ship flow show, so the UI cannot offer a button the
service would decline — or hide one it would accept.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import Engine

from ompire_daemon.registry.reviews import ReviewIterationRecord, get_review
from ompire_daemon.registry.ships import (
    DeliveryRecord,
    authority_boundary,
    get_latest_delivery,
    is_pre_upgrade_grant,
)
from ompire_daemon.registry.workflows import (
    GATE_SNAPSHOT_VERSION,
    StepRecord,
    latest_step_record,
    list_step_records,
)
from ompire_daemon.taskdefinition import (
    TaskDefinitionUnavailableError,
    resolve_task_definition,
)
from ompire_daemon.workflow_definitions import (
    DeliveryGrant,
    DeliveryStep,
    GateStep,
    ReviewStep,
    WorkflowRevision,
)

if TYPE_CHECKING:
    from ompire_daemon.work.tasks import Task

# How authority for a privileged operation was established.
SOURCE_WORKFLOW_GATE = "workflow-gate"
SOURCE_WORKFLOW_ACTION = "workflow-action"
SOURCE_LEGACY_CONTINUATION = "legacy-continuation"

# Why an operation is refused. These are codes a UI switches on, and each one
# names a *different* thing an operator would do about it.
REFUSE_DEFINITION_UNAVAILABLE = "definition-unavailable"
REFUSE_NO_DELIVERY_VOCABULARY = "no-delivery-vocabulary"
REFUSE_NOT_AT_GATE = "not-at-gate"
REFUSE_UNKNOWN_CHOICE = "unknown-choice"
REFUSE_CHOICE_GRANTS_NOTHING = "choice-grants-nothing"
REFUSE_STALE_GATE = "stale-gate"
REFUSE_REVIEW_MISSING = "review-missing"
REFUSE_REVIEW_NOT_APPROVED = "review-not-approved"
REFUSE_NOT_AT_ACTION = "not-at-action"
REFUSE_ACTION_NOT_GRANTED = "action-not-granted"
REFUSE_RUN_FINISHED = "run-finished"
REFUSE_REVIEW_NOT_DECLARED = "review-not-declared"
REFUSE_NOT_AT_REVIEW = "not-at-review"


class AuthorityError(Exception):
    """A privileged operation is not permitted by this run's procedure."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class PendingApproval:
    """A delivery gate this run is waiting at, and what it is asking about."""

    seq: int
    step: GateStep
    snapshot: dict[str, Any]
    review: ReviewIterationRecord | None
    review_step_seq: int | None

    def grant(self, choice_id: str) -> DeliveryGrant | None:
        choice = self.step.choice_named(choice_id)
        return choice.authorize if choice is not None else None


@dataclass(frozen=True)
class PendingAction:
    """A delivery step this run is currently at, and the chain it belongs to."""

    seq: int
    step: DeliveryStep
    delivery: DeliveryRecord
    # True when the effect was interrupted and the run is holding the same
    # attempt open for an explicit continuation.
    awaiting_continuation: bool = False


@dataclass(frozen=True)
class RunAuthority:
    """One task's current publication authority, and every reason it is not.

    Deliberately a value with no methods that act. Whatever a caller decides
    from it, the trusted managers still re-resolve their own content, target,
    and credential checks before an effect — this says *whether the procedure
    permits asking*, never that the operation is safe.
    """

    task_id: int
    revision: WorkflowRevision | None
    unavailable: str | None = None
    # A format-3 run parked at a gate whose answer could authorize publication.
    approval: PendingApproval | None = None
    # A format-3 run at an authorized delivery step, waiting to perform it.
    action: PendingAction | None = None
    # A genuine pre-upgrade authorization that may still finish its prefix.
    legacy: DeliveryRecord | None = None
    # A format-3 run parked at a `review` step.
    review_seq: int | None = None

    @property
    def format(self) -> int | None:
        return self.revision.format if self.revision is not None else None

    @property
    def declares_delivery(self) -> bool:
        """Whether the pinned procedure can publish at all."""
        return (
            self.revision is not None
            and bool(self.revision.definition.delivery_steps())
        )

    @property
    def declares_review(self) -> bool:
        return (
            self.revision is not None
            and bool(self.revision.definition.review_steps())
        )

    @property
    def declared_actions(self) -> tuple[str, ...]:
        if self.revision is None:
            return ()
        return self.revision.definition.declared_actions

    @property
    def source(self) -> str | None:
        """How authority would be established right now, if at all."""
        if self.approval is not None:
            return SOURCE_WORKFLOW_GATE
        if self.action is not None:
            return SOURCE_WORKFLOW_ACTION
        if self.legacy is not None:
            return SOURCE_LEGACY_CONTINUATION
        return None

    def refusal(self) -> tuple[str, str] | None:
        """Why no privileged action can be authorized now, or None.

        Ordered from the most specific fact to the most general, so an
        operator is told the thing they can act on: "this run has no
        publication in it at all" is a different problem from "you are looking
        at a question that has already been answered".
        """
        if self.unavailable is not None:
            return (REFUSE_DEFINITION_UNAVAILABLE, self.unavailable)
        if self.source is not None:
            return None
        if not self.declares_delivery:
            return (
                REFUSE_NO_DELIVERY_VOCABULARY,
                (
                    "this task's pinned workflow declares no publication "
                    "steps, so nothing can authorize signing, pushing, or "
                    "opening a pull request for it. Launch a new task from a "
                    "workflow that declares the delivery it should perform."
                ),
            )
        return (
            REFUSE_NOT_AT_GATE,
            (
                "this run is not waiting at an approval that authorizes "
                "publication, and has no authorized action outstanding."
            ),
        )


def resolve_authority(engine: Engine, task: Task) -> RunAuthority:
    """Where this task's run stands with respect to privileged operations.

    The task row is re-read rather than taken from the caller. A manager that
    has held a `Task` since before the run moved would otherwise be told about
    a question that has since been answered — and authority is a property of
    where the run *is*, never of how fresh someone's copy of it happens to be.
    """
    from ompire_daemon.work.tasks import TaskNotFoundError, get_task

    try:
        task = get_task(engine, task.id)
    except TaskNotFoundError:
        return RunAuthority(
            task_id=task.id, revision=None, unavailable="this task no longer exists"
        )
    try:
        revision = resolve_task_definition(engine, task)
    except TaskDefinitionUnavailableError as exc:
        return RunAuthority(task_id=task.id, revision=None, unavailable=exc.detail)

    definition = revision.definition
    latest = latest_step_record(engine, task.id)
    boundary = authority_boundary(engine)
    legacy: DeliveryRecord | None = None
    if not definition.delivery_steps():
        # An older procedure. It runs exactly as it always did, and it grants
        # no new publication — but an authorization a person genuinely gave
        # before this format existed is still theirs to finish.
        delivery = get_latest_delivery(engine, task.id)
        if (
            delivery is not None
            and is_pre_upgrade_grant(boundary, delivery)
            and task.workflow_status in ("complete", "failed")
            and delivery.remaining_actions
        ):
            legacy = delivery
        return RunAuthority(task_id=task.id, revision=revision, legacy=legacy)

    if latest is None:
        return RunAuthority(task_id=task.id, revision=revision)

    step = definition.step_named(latest.step)
    if isinstance(step, ReviewStep) and latest.status == "running":
        return RunAuthority(
            task_id=task.id, revision=revision, review_seq=latest.seq
        )
    if isinstance(step, GateStep) and step.delivery is not None:
        approval = _pending_approval(engine, task, latest, step)
        if approval is not None:
            return RunAuthority(
                task_id=task.id, revision=revision, approval=approval
            )
    if isinstance(step, DeliveryStep) and latest.status in ("running", "waiting"):
        # `waiting` here is the continuation pause: a privileged action whose
        # effect was interrupted, keeping its grant and its journal link while
        # it waits for a person to confirm the rest. It is deliberately still
        # an authorized action — the grant did not evaporate — but nothing
        # performs it without that confirmation.
        delivery = get_latest_delivery(engine, task.id)
        if delivery is not None and delivery.workflow_authorized:
            return RunAuthority(
                task_id=task.id,
                revision=revision,
                action=PendingAction(
                    seq=latest.seq,
                    step=step,
                    delivery=delivery,
                    awaiting_continuation=latest.status == "waiting",
                ),
            )
    return RunAuthority(task_id=task.id, revision=revision)


def _pending_approval(
    engine: Engine, task: Task, record: StepRecord, step: GateStep
) -> PendingApproval | None:
    """The unanswered question at a delivery gate, with its bound review.

    An answered gate is not pending, however recently: its decision and its
    successor were committed together, and offering it again is how a stale
    tab authorizes a second chain.
    """
    if record.status != "waiting" or record.pause is not None:
        return None
    if task.workflow_status != "waiting":
        return None
    snapshot = record.outcome or {}
    if snapshot.get("version") != GATE_SNAPSHOT_VERSION:
        return None
    if snapshot.get("decision") is not None:
        return None
    assert step.delivery is not None
    review, review_seq = bound_review(engine, task.id, record, step.delivery.review)
    return PendingApproval(
        seq=record.seq,
        step=step,
        snapshot=snapshot,
        review=review,
        review_step_seq=review_seq,
    )


def bound_review(
    engine: Engine, task_id: int, record: StepRecord, alias: str
) -> tuple[ReviewIterationRecord | None, int | None]:
    """The exact review attempt this question froze, not the latest one.

    A gate's evidence binding names a step and a sequence. Reading "the task's
    review" instead would let a review that finished *after* the question was
    asked authorize an answer to it — which is precisely the substitution the
    frozen binding exists to prevent.
    """
    from ompire_daemon.registry.reviews import iteration_for_step

    bindings = (record.evidence or {}).get("bindings") or {}
    binding = bindings.get(alias)
    if not isinstance(binding, dict):
        return None, None
    seq = binding.get("seq")
    if not isinstance(seq, int):
        return None, None
    return iteration_for_step(engine, task_id, seq), seq


def review_admission(
    authority: RunAuthority, task: Task
) -> tuple[str, str] | None:
    """Why a review cannot be started for this task now, or None.

    A format-3 run reviews when its `review` step says to; starting one by
    hand at an arbitrary moment would grade content the run is still writing
    and produce an iteration no step is waiting for. Older runs keep the
    operator-driven review they were written against.
    """
    if authority.unavailable is not None:
        return (REFUSE_DEFINITION_UNAVAILABLE, authority.unavailable)
    if authority.revision is None or not authority.declares_review:
        return None  # legacy: the operator drives review
    if authority.review_seq is None:
        return (
            REFUSE_NOT_AT_REVIEW,
            (
                "this task's workflow declares its own review step; the run "
                "starts the review when it reaches it, and reviewing at "
                "another moment would grade content the run is still changing."
            ),
        )
    return None


# Why a daemon-managed writer is refused right now.
REFUSE_AWAITING_APPROVAL = "awaiting-approval"
REFUSE_DELIVERING = "delivering"


def writer_refusal(engine: Engine, task: Task) -> tuple[str, str] | None:
    """Why a new daemon-managed workspace writer is refused, or None.

    The workspace guard answers "is someone writing right now"; this answers
    the question the guard structurally cannot. A run parked at an approval
    holds nothing — it is waiting for a person — and a turn started in that
    window would change the very content the approval is about, quietly
    invalidating a decision somebody is in the middle of making.

    So the refusal is about the run's *position*, and it points at the way
    forward the author actually declared: the gate's own correction choice.
    A delivery step is refused for the plainer reason that its effect is
    either running or on record, and nothing may write across it.
    """
    authority = resolve_authority(engine, task)
    if authority.approval is not None:
        return (
            REFUSE_AWAITING_APPROVAL,
            (
                f"task {task.id} is waiting for a publication decision; "
                "changing the workspace now would invalidate the content that "
                "decision is about. Answer the question — its 'request "
                "changes' choice is how work continues — or finish without "
                "publishing."
            ),
        )
    if authority.action is not None:
        return (
            REFUSE_DELIVERING,
            (
                f"task {task.id} is performing an authorized "
                f"{authority.action.step.action} action; nothing may write to "
                "its workspace until that is on record."
            ),
        )
    return None


def step_records(engine: Engine, task_id: int) -> list[StepRecord]:
    return list_step_records(engine, task_id)


def review_for_delivery(
    engine: Engine, task_id: int
) -> ReviewIterationRecord | None:
    """The task's own latest review record, for legacy admission only."""
    record = get_review(engine, task_id)
    if record is None or not record.iterations:
        return None
    return record.iterations[-1]
