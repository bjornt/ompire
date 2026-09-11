"""Coordinated task cleanup: admission, guarded teardown, finalization.

The application algorithm behind the cleanup route, using its existing
collaborators rather than a new universal lifecycle service. Authority stays
where it already lived: active-delivery and run-position refusals come from
the delivery journal and the run-authority evaluator; the workspace hold and
the confined, container-first resource destruction come from the isolation
boundary; review/ship runtime finalization, candidate-storage release,
work-owner archival, and observation cleanup stay with their owners.

Cleanup destroys resources, never durable meaning: the delivery journal is
deliberately retained — a cleaned-up task keeps the record of what it
published and under whose authorization — and retained results, consumer
references, and journals are untouched (ADR-0034). Only the candidate staging
repositories go, and only the ones no unresolved work still needs as
evidence.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from sqlalchemy import Engine

from ompire_daemon.advisories import AdvisorySampler
from ompire_daemon.config import Config
from ompire_daemon.events import EventHub
from ompire_daemon.isolation import (
    WorkshopRemoveError,
    WorkspaceBlockedError,
    WorkspaceBusyError,
    destroy_workspace,
)
from ompire_daemon.notifications import AttentionNotifier
from ompire_daemon.oversight.tasks import task_payload
from ompire_daemon.registry.ships import get_active_delivery
from ompire_daemon.review import ReviewManager
from ompire_daemon.runauthority import writer_refusal
from ompire_daemon.sessions import SessionTracker
from ompire_daemon.ship import ShipManager
from ompire_daemon.work.tasks import (
    get_task,
    mark_archived,
)

logger = logging.getLogger(__name__)


class CleanupConflictError(Exception):
    """Cleanup is refused by a state the operator must resolve first.

    `detail` is the wire body of the refusal — a plain message, or the
    code-plus-message pair a run-position refusal produces.
    """

    def __init__(self, detail: str | dict[str, str]) -> None:
        super().__init__(str(detail))
        self.detail = detail


class WorkshopTeardownError(Exception):
    """The container could not be torn down; the clone is retained."""

    def __init__(self, stderr: str) -> None:
        super().__init__("workshop remove failed; clone retained")
        self.stderr = stderr


class CleanupService:
    """One cleanup operation, admitted and finalized across its owners."""

    def __init__(
        self,
        engine: Engine,
        config: Config,
        events: EventHub,
        *,
        sessions: SessionTracker,
        advisories: AdvisorySampler,
        reviews: ReviewManager,
        ships: ShipManager,
        guard: Any,
        notifications: AttentionNotifier,
    ) -> None:
        self._engine = engine
        self._config = config
        self._events = events
        self._sessions = sessions
        self._advisories = advisories
        self._reviews = reviews
        self._ships = ships
        self._guard = guard
        self._notifications = notifications

    async def cleanup_task(self, task_id: int) -> dict:
        """Tear the task's workspace down and archive it, or refuse.

        Returns the archived task's canonical projection, published as a
        `task_updated` event. Raises `TaskNotFoundError`,
        `CleanupConflictError`, or `WorkshopTeardownError` for the route to
        map; nothing is archived or discarded unless teardown completed.
        """
        task = get_task(self._engine, task_id)

        active = get_active_delivery(self._engine, task_id)
        if active is not None and active.disposition in ("authorized", "unresolved"):
            raise CleanupConflictError(
                f"task {task_id} has a delivery that is still {active.disposition}; "
                "finish or reconcile it before cleaning up"
            )
        # And refused while the run is at a decision or an action: destroying the
        # workspace mid-publication would leave the effect on record with nothing
        # to reconcile it against (ADR-0033).
        refusal = writer_refusal(self._engine, task)
        if refusal is not None:
            raise CleanupConflictError({"code": refusal[0], "message": refusal[1]})

        # Cleanup deletes the clone, so it is refused while another host
        # operation owns the workspace and while a privileged effect's outcome
        # is unknown (ADR-0032). Abandoning remaining agent work does not make
        # an unresolved effect safe to destroy the evidence for; it has to be
        # reconciled first.
        #
        # The hold is retained through teardown rather than only checked here:
        # a result capture admitted between this check and the deletion would
        # be reading files out of a clone that is being deleted (ADR-0034). A
        # busy capture therefore refuses cleanup, and a started cleanup refuses
        # a new capture.
        try:
            async with self._guard.cleanup_hold(task_id, "cleanup"):
                # The resource operation re-validates confinement on its own;
                # checking it here as well keeps the outside-root refusal
                # ahead of any writer-contention answer, as it always was.
                clone_path = Path(task.clone_path).resolve()
                task_root = self._config.task_dir_root.expanduser().resolve()
                if task_root not in clone_path.parents:
                    raise CleanupConflictError(
                        f"refusing to delete {clone_path}: outside task root {task_root}"
                    )
                try:
                    # Container first, then clone (design D-4); an already-gone
                    # container is success, any other failure aborts
                    # un-archived with the clone retained.
                    await destroy_workspace(
                        task.clone_path,
                        self._config.task_dir_root,
                        workshop_id=task.workshop_id,
                        workshop_timeout=self._config.workshop_step_timeout,
                    )
                except WorkshopRemoveError as exc:
                    raise WorkshopTeardownError(exc.stderr) from exc

                await self._reviews.cancel_and_drop(task_id)
                await self._ships.cancel_and_drop(task_id)
        except (WorkspaceBusyError, WorkspaceBlockedError) as exc:
            raise CleanupConflictError(str(exc)) from exc

        self._ships.release_candidate_storage(task_id)
        self._guard.discard(task_id)
        archived = mark_archived(self._engine, task_id)
        self._sessions.discard(task_id)
        self._advisories.clear_task(task_id)
        self._notifications.clear_task(task_id)
        payload = task_payload(archived, engine=self._engine)
        self._events.publish("task_updated", payload)
        return payload
