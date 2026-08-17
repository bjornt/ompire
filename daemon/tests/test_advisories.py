"""AdvisorySampler tests: throttled stats, context-high crossing/clearing,
maybe-waiting on idle entry/clearing on leaving idle, and sample-failure
tolerance — driven directly against the public sampling methods with a fake
handle, plus one integration check that the tracker hooks actually fire."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ompire_daemon import agent as agent_module
from ompire_daemon.advisories import AdvisorySampler
from ompire_daemon.agent import AgentSupervisor
from ompire_daemon.config import Config
from ompire_daemon.events import EventHub
from ompire_daemon.sessions import SessionTracker

from tests.test_rpc import fake_omp_argv

THROTTLE = 0.15


class FakeHandle:
    """A minimal `AgentHandle` stand-in: `request(type)` returns/raises a
    canned response configured per test."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self._responses = responses
        self.requested: list[str] = []

    async def request(self, request_type: str, **_fields: Any) -> dict[str, Any]:
        self.requested.append(request_type)
        response = self._responses[request_type]
        if isinstance(response, Exception):
            raise response
        return response


def _state(context_pct: float | None) -> dict[str, Any]:
    return {"success": True, "data": {} if context_pct is None else {"contextPercent": context_pct}}


def _stats(input_tokens: int = 100, output_tokens: int = 20, cost: float = 0.01) -> dict[str, Any]:
    return {
        "success": True,
        "data": {"inputTokens": input_tokens, "outputTokens": output_tokens, "totalCostUsd": cost},
    }


async def _collect_one(hub: EventHub, event_type: str, timeout: float = 5.0):
    queue = hub.subscribe()
    async with asyncio.timeout(timeout):
        while True:
            event = await queue.get()
            if event.type == event_type:
                return event.payload


def test_looks_like_a_question_heuristic() -> None:
    from ompire_daemon.advisories import _looks_like_a_question

    assert _looks_like_a_question("Widen the fix to both loops?")
    assert _looks_like_a_question("Should I proceed")
    assert not _looks_like_a_question("Done, ready for review.")
    assert not _looks_like_a_question("")


async def test_sample_turn_end_broadcasts_stats() -> None:
    hub = EventHub()
    sampler = AdvisorySampler(hub, stats_throttle_interval=THROTTLE, context_advisory_threshold=80)
    handle = FakeHandle({"get_state": _state(42), "get_session_stats": _stats(100, 20, 0.01)})

    queue = hub.subscribe()
    await sampler.sample_turn_end(1, "main", handle)

    event = await queue.get()
    assert event.type == "stats"
    assert event.payload == {
        "task_id": 1,
        "session": "main",
        "context_pct": 42,
        "tokens": {"input": 100, "output": 20},
        "cost": 0.01,
    }


async def test_rapid_turns_are_throttled() -> None:
    hub = EventHub()
    sampler = AdvisorySampler(hub, stats_throttle_interval=THROTTLE, context_advisory_threshold=80)
    handle = FakeHandle({"get_state": _state(10), "get_session_stats": _stats()})
    queue = hub.subscribe()

    await sampler.sample_turn_end(1, "main", handle)
    await sampler.sample_turn_end(1, "main", handle)  # within the throttle window

    stats_events = []
    with pytest.raises(asyncio.TimeoutError):
        async with asyncio.timeout(0.05):
            while True:
                stats_events.append(await queue.get())
    assert len(stats_events) == 1

    await asyncio.sleep(THROTTLE)
    await sampler.sample_turn_end(1, "main", handle)
    event = await queue.get()
    assert event.type == "stats"


async def test_context_high_fires_once_and_rearms_after_dropping(monkeypatch: pytest.MonkeyPatch) -> None:
    hub = EventHub()
    sampler = AdvisorySampler(hub, stats_throttle_interval=0, context_advisory_threshold=80)
    queue = hub.subscribe()

    async def sample(pct: float) -> None:
        await sampler.sample_turn_end(1, "main", FakeHandle({"get_state": _state(pct), "get_session_stats": _stats()}))

    await sample(50)  # below threshold: stats only
    event = await queue.get()
    assert event.type == "stats"

    await sample(85)  # crosses threshold: stats + advisory
    assert (await queue.get()).type == "stats"
    advisory = await queue.get()
    assert advisory.type == "advisory"
    assert advisory.payload == {"task_id": 1, "session": "main", "kind": "context-high", "context_pct": 85}

    await sample(90)  # stays high: stats only, no re-advise
    assert (await queue.get()).type == "stats"
    with pytest.raises(asyncio.TimeoutError):
        async with asyncio.timeout(0.05):
            await queue.get()

    await sample(60)  # drops below: stats + advisory_cleared
    assert (await queue.get()).type == "stats"
    cleared = await queue.get()
    assert cleared.type == "advisory_cleared"
    assert cleared.payload == {"task_id": 1, "session": "main", "kind": "context-high"}

    await sample(85)  # crosses again: re-fires
    assert (await queue.get()).type == "stats"
    advisory = await queue.get()
    assert advisory.type == "advisory"
    assert advisory.payload == {"task_id": 1, "session": "main", "kind": "context-high", "context_pct": 85}


async def test_sample_failure_is_swallowed_and_broadcasts_nothing() -> None:
    hub = EventHub()
    sampler = AdvisorySampler(hub, stats_throttle_interval=0, context_advisory_threshold=80)
    handle = FakeHandle({"get_state": RuntimeError("boom"), "get_session_stats": _stats()})
    queue = hub.subscribe()

    await sampler.sample_turn_end(1, "main", handle)  # must not raise

    with pytest.raises(asyncio.TimeoutError):
        async with asyncio.timeout(0.05):
            await queue.get()


async def test_maybe_waiting_on_question_like_idle() -> None:
    hub = EventHub()
    sampler = AdvisorySampler(hub, stats_throttle_interval=THROTTLE, context_advisory_threshold=80)
    handle = FakeHandle({"get_last_assistant_text": {"success": True, "data": {"text": "Widen the fix?"}}})
    queue = hub.subscribe()

    await sampler.sample_idle_entered(1, "main", handle)

    event = await queue.get()
    assert event.type == "advisory"
    assert event.payload == {"task_id": 1, "session": "main", "kind": "maybe-waiting"}


async def test_non_question_idle_does_not_advise() -> None:
    hub = EventHub()
    sampler = AdvisorySampler(hub, stats_throttle_interval=THROTTLE, context_advisory_threshold=80)
    handle = FakeHandle(
        {"get_last_assistant_text": {"success": True, "data": {"text": "Done, ready for review."}}}
    )
    queue = hub.subscribe()

    await sampler.sample_idle_entered(1, "main", handle)

    with pytest.raises(asyncio.TimeoutError):
        async with asyncio.timeout(0.05):
            await queue.get()


async def test_maybe_waiting_sample_failure_is_swallowed() -> None:
    hub = EventHub()
    sampler = AdvisorySampler(hub, stats_throttle_interval=THROTTLE, context_advisory_threshold=80)
    handle = FakeHandle({"get_last_assistant_text": RuntimeError("boom")})

    await sampler.sample_idle_entered(1, "main", handle)  # must not raise


async def test_maybe_waiting_clears_on_leaving_idle() -> None:
    hub = EventHub()
    sampler = AdvisorySampler(hub, stats_throttle_interval=THROTTLE, context_advisory_threshold=80)
    handle = FakeHandle({"get_last_assistant_text": {"success": True, "data": {"text": "Widen the fix?"}}})
    sampler.start()
    try:
        queue = hub.subscribe()
        await sampler.sample_idle_entered(1, "main", handle)
        posted = await queue.get()
        assert posted.type == "advisory"

        hub.publish(
            "status_changed",
            {"task_id": 1, "session": "main", "from": "idle", "to": "working", "reason": "x"},
        )
        async with asyncio.timeout(5):
            while True:
                cleared = await queue.get()
                if cleared.type == "advisory_cleared":
                    break
        assert cleared.payload == {"task_id": 1, "session": "main", "kind": "maybe-waiting"}
    finally:
        await sampler.stop()


async def test_clear_task_drops_bookkeeping() -> None:
    hub = EventHub()
    sampler = AdvisorySampler(hub, stats_throttle_interval=0, context_advisory_threshold=80)
    await sampler.sample_turn_end(
        1, "main", FakeHandle({"get_state": _state(90), "get_session_stats": _stats()})
    )
    assert (1, "main") in sampler._context_high  # noqa: SLF001 — bookkeeping check

    sampler.clear_task(1)

    assert (1, "main") not in sampler._context_high  # noqa: SLF001
    assert (1, "main") not in sampler._last_sampled_at  # noqa: SLF001


@pytest.fixture
def tracked(monkeypatch: pytest.MonkeyPatch):
    """A real supervisor + tracker + advisory sampler wired to the fake omp,
    confirming the tracker hooks actually fire (not just the sampler's public
    methods in isolation)."""
    monkeypatch.setattr(
        agent_module, "build_agent_argv", lambda clone, env, resume=None, model=None, thinking=None: fake_omp_argv("happy")
    )

    async def no_preflight(clone_path: str) -> None:
        return None

    monkeypatch.setattr(agent_module, "verify_ask_timeout", no_preflight)
    hub = EventHub()
    tracker = SessionTracker(hub, idle_debounce=0.1)
    sampler = AdvisorySampler(hub, stats_throttle_interval=0, context_advisory_threshold=80)
    sampler.register(tracker)
    config = Config(agent_ready_timeout=5, agent_ring_buffer_size=100)
    supervisor = AgentSupervisor(config, hub, tracker)
    return supervisor, tracker, hub, sampler


async def test_turn_end_hook_fires_through_real_tracker(tracked) -> None:
    supervisor, tracker, hub, sampler = tracked
    queue = hub.subscribe()
    handle = await supervisor.start(1, "main", "/clone")
    await handle.prompt("hi")

    async with asyncio.timeout(5):
        while True:
            event = await queue.get()
            if event.type == "stats":
                break
    assert event.payload["task_id"] == 1
    assert event.payload["session"] == "main"
    assert event.payload["tokens"] == {"input": 1200, "output": 340}

    await supervisor.stop(1, "main")
