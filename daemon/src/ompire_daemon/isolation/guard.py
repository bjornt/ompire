"""The task-scoped workspace writer guard.

One mechanism, owned by the resource boundary: it says who may write a
workspace right now, and whether an unresolved privileged effect makes writing
unsafe. It is mechanical exclusion only — it never decides what a workflow may
publish, and it never reads the durable evidence that says why a workspace is
blocked. Delivery (and legacy-ref recovery) derive blocks from their own
records and install or clear them here.

Holder kinds distinguish two questions. `host` covers Ompire's own host-side
operations on the workspace — review, drafting, delivery — which exclude
everything, including each other. `agent` covers the task's own work: a
workflow step driving its agent. Agent work blocks a host operation from
*starting*, but it does not stop the operator from steering the very agent it
is running, or from abandoning the task; those are interactions with the
writer that already holds it, not a second writer.

Reentrancy is by execution context, not by an explicit token argument: a
holder's nested daemon calls — an authorized draft prompting the agent, a
review handing comments back — inherit the hold and pass their own admission
check, while an unrelated caller does not. `asyncio` copies the context into
every task it spawns, so a background job started under a hold stays inside
it. That inheritance is intentional convenience, not a security claim: the
guard coordinates daemon-managed writers, and nothing here restrains an
agent's native escape hatch or replaces the OS sandbox.

The guard never interrupts an existing writer. It refuses the new one.

Architecture: ADR-0032, ADR-0039.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Iterator
from contextvars import ContextVar


class WorkspaceBusyError(Exception):
    """Another daemon-managed writer owns this task's workspace."""

    def __init__(self, task_id: int, owner: str) -> None:
        super().__init__(
            f"task {task_id} workspace is in use by {owner}; wait for it to finish"
        )
        self.task_id = task_id
        self.owner = owner


class WorkspaceBlockedError(Exception):
    """An unresolved privileged effect blocks work on this task."""

    def __init__(self, task_id: int, reason: str) -> None:
        super().__init__(
            f"task {task_id} has an unresolved delivery effect: {reason}"
        )
        self.task_id = task_id
        self.reason = reason


_current_hold: ContextVar[tuple[int, str] | None] = ContextVar(
    "ompire_workspace_hold", default=None
)


class WorkspaceGuard:
    """Task-scoped exclusion over daemon-managed workspace writers.

    The owner identity is an opaque integer: the guard never consults a task
    registry, so the same mechanism admits writers for owners that have no
    task row at all.
    """

    HOST = "host"
    AGENT = "agent"

    def __init__(self) -> None:
        self._owners: dict[int, tuple[str, str]] = {}
        self._blocked: dict[int, str] = {}

    def owner(self, task_id: int) -> str | None:
        held = self._owners.get(task_id)
        return held[0] if held is not None else None

    def owner_kind(self, task_id: int) -> str | None:
        held = self._owners.get(task_id)
        return held[1] if held is not None else None

    def blocked_reason(self, task_id: int) -> str | None:
        return self._blocked.get(task_id)

    def block(self, task_id: int, reason: str) -> None:
        """Mark a task unsafe to write until an operator decision resolves it.

        Set from startup reconciliation and whenever an effect's outcome cannot
        be established. Deliberately independent of `hold`: the daemon that
        launched the effect may be long gone.
        """
        self._blocked[task_id] = reason

    def unblock(self, task_id: int) -> None:
        self._blocked.pop(task_id, None)

    def discard(self, task_id: int) -> None:
        self._owners.pop(task_id, None)
        self._blocked.pop(task_id, None)

    def held_by_current_context(self, task_id: int) -> bool:
        hold = _current_hold.get()
        return hold is not None and hold[0] == task_id

    def _assert_unblocked(self, task_id: int) -> None:
        blocked = self._blocked.get(task_id)
        if blocked is not None:
            raise WorkspaceBlockedError(task_id, blocked)

    def assert_available(self, task_id: int, *, allow_blocked: bool = False) -> None:
        """Refuse a new writer while any writer owns the workspace, or while an
        unresolved effect makes writing unsafe."""
        if not allow_blocked:
            self._assert_unblocked(task_id)
        held = self._owners.get(task_id)
        if held is not None and not self.held_by_current_context(task_id):
            raise WorkspaceBusyError(task_id, held[0])

    def assert_host_free(self, task_id: int) -> None:
        """Refuse only against a host-side operation and an unresolved effect.

        What operator agent interaction and cleanup ask: steering the agent a
        workflow step is already running is not a second writer, but doing
        either while a review is reading the workspace, or while a privileged
        effect's outcome is unknown, is.
        """
        self._assert_unblocked(task_id)
        held = self._owners.get(task_id)
        if (
            held is not None
            and held[1] == self.HOST
            and not self.held_by_current_context(task_id)
        ):
            raise WorkspaceBusyError(task_id, held[0])

    def acquire(
        self,
        task_id: int,
        owner: str,
        *,
        kind: str = HOST,
        allow_blocked: bool = False,
    ) -> None:
        """Take ownership for work that outlives the calling coroutine — a
        supervised reviewer process, above all. `release` is the caller's
        obligation."""
        self.assert_available(task_id, allow_blocked=allow_blocked)
        self._owners[task_id] = (owner, kind)

    def release(self, task_id: int, owner: str) -> None:
        held = self._owners.get(task_id)
        if held is not None and held[0] == owner:
            self._owners.pop(task_id, None)

    @contextlib.asynccontextmanager
    async def hold(
        self,
        task_id: int,
        owner: str,
        *,
        kind: str = HOST,
        allow_blocked: bool = False,
    ) -> AsyncIterator[None]:
        """Own the task's workspace for the duration of the block."""
        if self.held_by_current_context(task_id):
            # Nested call inside an admitted holder: it is already the owner.
            # The original ownership kind and lifetime stay intact — a nested
            # hold never upgrades an agent hold into a host one.
            yield
            return
        self.acquire(task_id, owner, kind=kind, allow_blocked=allow_blocked)
        token = _current_hold.set((task_id, owner))
        try:
            yield
        finally:
            _current_hold.reset(token)
            self.release(task_id, owner)

    @contextlib.asynccontextmanager
    async def cleanup_hold(self, task_id: int, owner: str) -> AsyncIterator[None]:
        """Own the workspace for the whole of cleanup, agent work included.

        Cleanup is the one operation that legitimately abandons the task's own
        agent: it tears the container down and deletes the clone. So it admits
        on `assert_host_free` — refusing another host operation and an
        unresolved effect, exactly as before — and then *takes* ownership for
        the duration, displacing any agent hold. It cannot displace a host
        hold or an unresolved-effect block: those keep refusing it.

        The reservation is what is new. `assert_host_free` alone left the
        workspace unowned across cleanup's awaits, so a capture admitted during
        the container teardown would have been reading files while the clone
        was being deleted underneath it (ADR-0034). Holding through teardown
        makes the two mutually exclusive in both orders: a busy capture refuses
        cleanup, and a started cleanup refuses a new capture.

        Ownership is released on failure as well as success — an aborted
        cleanup must not leave the task permanently unwritable.
        """
        self.assert_host_free(task_id)
        self._owners[task_id] = (owner, self.HOST)
        token = _current_hold.set((task_id, owner))
        try:
            yield
        finally:
            _current_hold.reset(token)
            self.release(task_id, owner)

    @contextlib.contextmanager
    def released(self, task_id: int, owner: str) -> Iterator[None]:
        """Temporarily give ownership back while `owner` still holds it.

        Used before handing review comments to the agent: the reviewer is done
        with the workspace, and the agent's turn has to be admitted on its own
        merits rather than inheriting — or deadlocking against — the reviewer's
        hold. Ownership is restored afterwards only if nothing else took it.
        """
        current = self._owners.get(task_id)
        held = current is not None and current[0] == owner
        if held:
            self._owners.pop(task_id, None)
        token = _current_hold.set(None)
        try:
            yield
        finally:
            _current_hold.reset(token)
            if held and task_id not in self._owners:
                assert current is not None
                self._owners[task_id] = current
