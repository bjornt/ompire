"""Per-session status state machine (SPEC Decision 4, core subset).

`SessionTracker` owns `{status, reason, since}` per task, fed by (a) lifecycle
calls from the supervisor and spawn pipeline (spawned, exited, step failures)
and (b) a subscriber queue on each agent's event fan-out for `agent_start` /
`agent_end` frames (design D-1). Every transition goes through one guarded
method that re-checks the current status, so races (exit during the idle
debounce, late frames after discard) resolve deterministically: exit wins.

Status is in-memory only and independent of live handles — `failed` outlives
the child's deregistration; entries are dropped on task cleanup/purge.
Persistence across daemon restarts is a later chunk.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ompire_daemon.events import EventHub

if TYPE_CHECKING:
    from ompire_daemon.agent import AgentHandle

logger = logging.getLogger(__name__)

SESSION_STATUSES = ("starting", "working", "idle", "failed")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class SessionInfo:
    status: str
    reason: str
    since: str


class SessionTracker:
    def __init__(self, hub: EventHub, idle_debounce: float) -> None:
        self._hub = hub
        self._idle_debounce = idle_debounce
        self._sessions: dict[int, SessionInfo] = {}
        self._watchers: dict[int, asyncio.Task] = {}
        self._debounces: dict[int, asyncio.Task] = {}
        self._operator_stops: set[int] = set()

    def get(self, task_id: int) -> SessionInfo | None:
        return self._sessions.get(task_id)

    def snapshot(self) -> dict[int, dict[str, str]]:
        """Current statuses for the WS snapshot's `sessions` map (design D-4)."""
        return {task_id: asdict(info) for task_id, info in self._sessions.items()}

    # --- lifecycle transitions (supervisor / spawn pipeline / stop path) ------

    def agent_spawning(self, task_id: int) -> None:
        """The agent child is being spawned; covers the ready handshake too."""
        self._transition(task_id, "starting", "agent spawned")

    def watch(self, task_id: int, handle: AgentHandle) -> None:
        """Subscribe to the agent's fan-out and drive frame transitions."""
        self.unwatch(task_id)
        queue = handle.subscribe()
        watcher = asyncio.create_task(self._watch_events(task_id, handle, queue))
        self._watchers[task_id] = watcher
        watcher.add_done_callback(lambda t: self._pop_if_current(self._watchers, task_id, t))

    def agent_exited(self, task_id: int, exit_code: int) -> None:
        """Any child exit lands `failed` (design D-2); the reason distinguishes
        an operator stop from a crash. Exit wins any pending idle debounce."""
        self._cancel_debounce(task_id)
        if task_id in self._operator_stops:
            self._operator_stops.discard(task_id)
            reason = "stopped by operator"
        else:
            reason = f"process exited with code {exit_code}"
        self._transition(task_id, "failed", reason)

    def expect_operator_stop(self, task_id: int) -> None:
        self._operator_stops.add(task_id)

    def clear_operator_stop(self, task_id: int) -> None:
        self._operator_stops.discard(task_id)

    def spawn_step_failed(self, task_id: int, reason: str) -> None:
        """An agent/prompt spawn step failed before or despite the child."""
        self._transition(task_id, "failed", reason)

    def prompt_skipped(self, task_id: int) -> None:
        """Empty stored prompt: ready → idle instead of hanging in starting."""
        self._transition(task_id, "idle", "ready, no prompt to send", allow_from={"starting"})

    def discard(self, task_id: int) -> None:
        """Drop the entry on task cleanup/purge; late events cannot resurrect it."""
        self._cancel_debounce(task_id)
        self.unwatch(task_id)
        self._operator_stops.discard(task_id)
        self._sessions.pop(task_id, None)

    def unwatch(self, task_id: int) -> None:
        watcher = self._watchers.pop(task_id, None)
        if watcher is not None:
            watcher.cancel()

    # --- internals ------------------------------------------------------------

    def _transition(
        self, task_id: int, to: str, reason: str, *, allow_from: set[str] | None = None
    ) -> None:
        """The single guarded transition: re-checks current status so competing
        transitions (exit vs. debounce vs. frames) can't clobber each other."""
        current = self._sessions.get(task_id)
        from_status = current.status if current is not None else None
        if current is None and to != "starting":
            return  # discarded or never-tracked: late events are ignored
        if from_status == "failed":
            return  # terminal until cleanup (no restart path this chunk)
        if allow_from is not None and from_status not in allow_from:
            return
        info = SessionInfo(status=to, reason=reason, since=_now_iso())
        self._sessions[task_id] = info
        self._hub.publish(
            "status_changed",
            {"task_id": task_id, "from": from_status, "to": to, "reason": reason},
        )

    async def _watch_events(self, task_id: int, handle: AgentHandle, queue: asyncio.Queue) -> None:
        # Local import: agent.py type-imports this module, so a module-level
        # import here would be circular.
        from ompire_daemon.agent import EVENT_STREAM_END

        try:
            while True:
                event = await queue.get()
                if event is EVENT_STREAM_END:
                    return
                if event.type == "agent_start":
                    self._cancel_debounce(task_id)
                    self._transition(task_id, "working", "agent_start frame")
                elif event.type == "agent_end":
                    self._start_debounce(task_id, handle)
        finally:
            handle.unsubscribe(queue)

    def _start_debounce(self, task_id: int, handle: AgentHandle) -> None:
        self._cancel_debounce(task_id)
        task = asyncio.create_task(self._debounced_idle(task_id, handle))
        self._debounces[task_id] = task
        task.add_done_callback(lambda t: self._pop_if_current(self._debounces, task_id, t))

    def _cancel_debounce(self, task_id: int) -> None:
        pending = self._debounces.pop(task_id, None)
        if pending is not None:
            pending.cancel()

    async def _debounced_idle(self, task_id: int, handle: AgentHandle) -> None:
        """Turn-boundary rule (design D-3): wait, then check the agent's queue;
        only a quiet, empty-queue result yields idle."""
        await asyncio.sleep(self._idle_debounce)
        reason = f"agent_end, queue empty after {self._idle_debounce}s"
        try:
            response = await handle.request("get_state")
        except Exception as exc:  # noqa: BLE001 — any failure degrades to debounce-only
            logger.warning(
                "get_state failed for task %d; falling back to debounce-only idle: %s",
                task_id,
                exc,
            )
            reason = f"agent_end, {self._idle_debounce}s quiet (state check failed)"
        else:
            data = response.get("data")
            data = data if isinstance(data, dict) else {}
            queued = data.get("queuedMessageCount") or 0
            if data.get("isStreaming") or queued > 0:
                # Not a real turn boundary: stay working, but surface why.
                self._transition(
                    task_id,
                    "working",
                    f"agent_end, but {queued} queued message(s)"
                    if queued
                    else "agent_end, but still streaming",
                    allow_from={"working"},
                )
                return
        self._transition(task_id, "idle", reason, allow_from={"working"})

    @staticmethod
    def _pop_if_current(registry: dict[int, asyncio.Task], task_id: int, task: asyncio.Task) -> None:
        if registry.get(task_id) is task:
            registry.pop(task_id, None)
        if not task.cancelled() and task.exception() is not None:
            logger.error("session tracker task for %d failed", task_id, exc_info=task.exception())
