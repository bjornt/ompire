"""Turn-boundary advisory sampler (SPEC Decision 4 "advisory signals"; design
D-5/D-6): piggybacks on the same turn boundary the idle debounce already
uses to sample `get_state`/`get_session_stats`, broadcasting a throttled
`stats` event and a `context-high` advisory on threshold crossings, plus an
idle-entry "maybe waiting for a reply" heuristic decoration. Never
contributes to attention tiers or notifications — a decoration, not a state
(SPEC Decision 4).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from ompire_daemon.events import EventHub

if TYPE_CHECKING:
    from ompire_daemon.agent import AgentHandle
    from ompire_daemon.sessions import SessionTracker

logger = logging.getLogger(__name__)

# Lightweight question heuristic (design D-6): ends with `?`, or opens with a
# small interrogative-lead set.
_QUESTION_LEAD_RE = re.compile(
    r"^(is|are|do|does|did|can|could|should|would|will|which|what|who|where|when|why|how)\b",
    re.IGNORECASE,
)


def _looks_like_a_question(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    return stripped.endswith("?") or bool(_QUESTION_LEAD_RE.match(stripped))


def _context_percent(state_data: dict[str, Any]) -> float | None:
    """Mirrors the frontend's `contextPercent` heuristic (`agentStatus.ts`):
    field names beyond isStreaming/queued are an open SPEC question, so this
    reads a few candidate shapes defensively and returns `None` when none
    match."""
    percent = state_data.get("contextPercent")
    if isinstance(percent, (int, float)) and not isinstance(percent, bool):
        return round(float(percent))
    usage = state_data.get("contextUsage")
    if isinstance(usage, (int, float)) and not isinstance(usage, bool):
        return round(float(usage) * 100 if usage <= 1 else float(usage))
    used = state_data.get("contextTokens")
    maximum = state_data.get("maxContextTokens") or state_data.get("contextWindow")
    if (
        isinstance(used, (int, float))
        and not isinstance(used, bool)
        and isinstance(maximum, (int, float))
        and not isinstance(maximum, bool)
        and maximum > 0
    ):
        return round((float(used) / float(maximum)) * 100)
    return None


class AdvisorySampler:
    """No per-task state lives on the session tracker (mirrors design D-1's
    "notifier owns tiers" split): this owns all advisory bookkeeping."""

    def __init__(
        self,
        hub: EventHub,
        *,
        stats_throttle_interval: float,
        context_advisory_threshold: int,
    ) -> None:
        self._hub = hub
        self._throttle = stats_throttle_interval
        self._threshold = context_advisory_threshold
        # Keyed (task_id, session) like the tracker (workflow-engine D-1).
        self._last_sampled_at: dict[tuple[int, str], float] = {}
        self._context_high: set[tuple[int, str]] = set()
        self._maybe_waiting: set[tuple[int, str]] = set()
        self._queue: asyncio.Queue | None = None
        self._run_task: asyncio.Task | None = None

    def register(self, tracker: SessionTracker) -> None:
        """Hooks the tracker's turn-boundary/idle-entry points (design D-5:
        "hooking the existing debounce path or subscribing to the watcher")
        without the tracker knowing anything about advisories itself."""
        tracker.add_turn_end_hook(self.sample_turn_end)
        tracker.add_idle_entered_hook(self.sample_idle_entered)

    def start(self) -> None:
        """Subscribes to `status_changed` so `maybe-waiting` clears when a
        session leaves `idle` by any path (design D-6)."""
        self._queue = self._hub.subscribe()
        self._run_task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._run_task is not None:
            self._run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._run_task
            self._run_task = None
        if self._queue is not None:
            self._hub.unsubscribe(self._queue)
            self._queue = None

    async def _run(self) -> None:
        assert self._queue is not None
        while True:
            event = await self._queue.get()
            if event.type != "status_changed":
                continue
            task_id = event.payload.get("task_id")
            session = event.payload.get("session")
            to_status = event.payload.get("to")
            if (
                isinstance(task_id, int)
                and isinstance(session, str)
                and isinstance(to_status, str)
            ):
                self._clear_maybe_waiting_if_left_idle(task_id, session, to_status)

    def clear_task(self, task_id: int) -> None:
        """Drop per-task bookkeeping (task cleanup/purge, mirroring
        `SessionTracker.discard`)."""
        for key in [key for key in self._last_sampled_at if key[0] == task_id]:
            self._last_sampled_at.pop(key, None)
        for key in [key for key in self._context_high if key[0] == task_id]:
            self._context_high.discard(key)
        for key in [key for key in self._maybe_waiting if key[0] == task_id]:
            self._maybe_waiting.discard(key)

    # --- sampling (invoked via the tracker hooks, or directly in tests) ------

    async def sample_turn_end(self, task_id: int, session: str, handle: AgentHandle) -> None:
        """Throttled `get_state`/`get_session_stats` sample at a turn
        boundary (design D-5): broadcasts `stats` and updates the
        `context-high` advisory. A failed sample is logged and skipped —
        it must never disrupt the session's idle transition, which the
        tracker drives independently of this hook."""
        key = (task_id, session)
        now = time.monotonic()
        last = self._last_sampled_at.get(key)
        if last is not None and now - last < self._throttle:
            return
        try:
            state_response = await handle.request("get_state")
            stats_response = await handle.request("get_session_stats")
        except Exception as exc:  # noqa: BLE001 — sampling must never disrupt idle
            logger.warning(
                "advisory stats sample failed for task %d session %s: %s", task_id, session, exc
            )
            return
        self._last_sampled_at[key] = now

        state_data = state_response.get("data")
        state_data = state_data if isinstance(state_data, dict) else {}
        stats_data = stats_response.get("data")
        stats_data = stats_data if isinstance(stats_data, dict) else {}

        context_pct = _context_percent(state_data)
        self._hub.publish(
            "stats",
            {
                "task_id": task_id,
                "session": session,
                "context_pct": context_pct,
                "tokens": {
                    "input": stats_data.get("inputTokens"),
                    "output": stats_data.get("outputTokens"),
                },
                "cost": stats_data.get("totalCostUsd"),
            },
        )
        self._update_context_advisory(task_id, session, context_pct)

    def _update_context_advisory(
        self, task_id: int, session: str, context_pct: float | None
    ) -> None:
        if context_pct is None:
            return
        key = (task_id, session)
        over = context_pct >= self._threshold
        was_over = key in self._context_high
        if over and not was_over:
            self._context_high.add(key)
            self._hub.publish(
                "advisory",
                {
                    "task_id": task_id,
                    "session": session,
                    "kind": "context-high",
                    "context_pct": context_pct,
                },
            )
        elif not over and was_over:
            self._context_high.discard(key)
            self._hub.publish(
                "advisory_cleared",
                {"task_id": task_id, "session": session, "kind": "context-high"},
            )

    async def sample_idle_entered(self, task_id: int, session: str, handle: AgentHandle) -> None:
        """`get_last_assistant_text` + question heuristic on idle entry
        (design D-6). **`get_last_assistant_text`'s RPC shape is unverified
        against live omp** (dogfood verification point, same pattern as the
        `omp-rpc-field-assumptions` memory note): read defensively and skip
        silently on failure — this is a decoration, never load-bearing."""
        try:
            response = await handle.request("get_last_assistant_text")
        except Exception as exc:  # noqa: BLE001 — a decoration must never disrupt idle
            logger.warning(
                "maybe-waiting sample failed for task %d session %s: %s", task_id, session, exc
            )
            return
        data = response.get("data")
        text = data.get("text") if isinstance(data, dict) else None
        if not isinstance(text, str) or not _looks_like_a_question(text):
            return
        self._maybe_waiting.add((task_id, session))
        self._hub.publish(
            "advisory", {"task_id": task_id, "session": session, "kind": "maybe-waiting"}
        )

    def _clear_maybe_waiting_if_left_idle(
        self, task_id: int, session: str, to_status: str
    ) -> None:
        key = (task_id, session)
        if to_status != "idle" and key in self._maybe_waiting:
            self._maybe_waiting.discard(key)
            self._hub.publish(
                "advisory_cleared",
                {"task_id": task_id, "session": session, "kind": "maybe-waiting"},
            )
