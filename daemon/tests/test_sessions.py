"""SessionTracker tests against the fake omp fixture: the D4 core-state
machine driven through a real supervisor (spawn, frames, exits)."""

from __future__ import annotations

import asyncio

import pytest

from ompire_daemon import agent as agent_module
from ompire_daemon.agent import AgentSupervisor
from ompire_daemon.config import Config
from ompire_daemon.events import EventHub
from ompire_daemon.sessions import SessionTracker

from tests.test_rpc import fake_omp_argv

DEBOUNCE = 0.2


@pytest.fixture
def tracked(monkeypatch: pytest.MonkeyPatch):
    """Supervisor + tracker wired to the fake omp, with a fast debounce."""
    scenario = {"name": "happy"}
    monkeypatch.setattr(
        agent_module, "build_agent_argv", lambda clone, env: fake_omp_argv(scenario["name"])
    )

    async def no_preflight(clone_path: str) -> None:
        return None

    monkeypatch.setattr(agent_module, "verify_ask_timeout", no_preflight)
    hub = EventHub()
    tracker = SessionTracker(hub, idle_debounce=DEBOUNCE)
    config = Config(agent_ready_timeout=5, agent_ring_buffer_size=100)
    supervisor = AgentSupervisor(config, hub, tracker)
    return supervisor, tracker, hub, scenario


async def wait_for_status(
    queue: asyncio.Queue, status: str, timeout: float = 5.0
) -> list[dict]:
    """Pull status_changed payloads off `queue` until `to == status` (inclusive)."""
    seen = []
    async with asyncio.timeout(timeout):
        while True:
            event = await queue.get()
            if event.type != "status_changed":
                continue
            seen.append(event.payload)
            if event.payload["to"] == status:
                return seen


async def test_starting_to_working(tracked) -> None:
    supervisor, tracker, hub, _ = tracked
    queue = hub.subscribe()
    handle = await supervisor.start(1, "/clone")
    assert tracker.get(1).status == "starting"
    assert tracker.get(1).reason == "agent spawned"

    await handle.prompt("hi")
    transitions = await wait_for_status(queue, "working")
    assert [(p["from"], p["to"]) for p in transitions] == [(None, "starting"), ("starting", "working")]
    assert transitions[-1]["reason"] == "agent_start frame"
    await supervisor.stop(1)


async def test_quiet_end_goes_idle(tracked) -> None:
    supervisor, tracker, hub, _ = tracked
    queue = hub.subscribe()
    handle = await supervisor.start(1, "/clone")
    await handle.prompt("hi")

    transitions = await wait_for_status(queue, "idle")
    assert transitions[-1]["from"] == "working"
    assert "queue empty" in transitions[-1]["reason"]
    assert tracker.get(1).status == "idle"
    await supervisor.stop(1)


async def test_chained_turns_never_flicker_through_idle(tracked) -> None:
    supervisor, tracker, hub, _ = tracked
    queue = hub.subscribe()
    handle = await supervisor.start(1, "/clone")
    # Second prompt right after the first burst's agent_end: its agent_start
    # lands inside the debounce window and cancels the pending idle.
    await handle.prompt("hi")
    await handle.prompt("again")

    transitions = await wait_for_status(queue, "idle")
    statuses = [p["to"] for p in transitions]
    assert statuses.count("idle") == 1  # only the final quiet boundary
    assert tracker.get(1).status == "idle"
    await supervisor.stop(1)


async def test_queued_messages_stay_working(tracked) -> None:
    supervisor, tracker, hub, _ = tracked
    queue = hub.subscribe()
    handle = await supervisor.start(1, "/clone")
    await handle.prompt("queue")  # fake omp reports queuedMessageCount: 1

    transitions = await wait_for_status(queue, "working")
    assert transitions[-1]["to"] == "working"
    # Wait past the debounce + state check: still working, reason names the queue.
    await asyncio.sleep(DEBOUNCE * 3)
    assert tracker.get(1).status == "working"
    assert "queued" in tracker.get(1).reason
    await supervisor.stop(1)


async def test_get_state_failure_falls_back_to_debounce_only(tracked) -> None:
    supervisor, tracker, hub, scenario = tracked
    scenario["name"] = "get-state-fails"
    queue = hub.subscribe()
    handle = await supervisor.start(1, "/clone")
    await handle.prompt("hi")

    transitions = await wait_for_status(queue, "idle")
    assert "state check failed" in transitions[-1]["reason"]
    await supervisor.stop(1)


async def test_exit_during_debounce_wins(tracked) -> None:
    supervisor, tracker, hub, _ = tracked
    queue = hub.subscribe()
    handle = await supervisor.start(1, "/clone")
    await handle.prompt("hi")
    # agent_end just arrived; kill the child inside the debounce window.
    await supervisor.stop(1)

    transitions = await wait_for_status(queue, "failed")
    assert "idle" not in [p["to"] for p in transitions]
    assert tracker.get(1).status == "failed"
    # No late idle lands after the exit either.
    await asyncio.sleep(DEBOUNCE * 3)
    assert tracker.get(1).status == "failed"


async def test_crash_reason_names_exit_code(tracked) -> None:
    supervisor, tracker, hub, scenario = tracked
    scenario["name"] = "exit-after-ready"
    queue = hub.subscribe()
    await supervisor.start(1, "/clone")

    transitions = await wait_for_status(queue, "failed")
    assert transitions[-1]["reason"] == "process exited with code 7"


async def test_operator_stop_reason(tracked) -> None:
    supervisor, tracker, hub, _ = tracked
    queue = hub.subscribe()
    await supervisor.start(1, "/clone")
    tracker.expect_operator_stop(1)
    await supervisor.stop(1)

    transitions = await wait_for_status(queue, "failed")
    assert transitions[-1]["reason"] == "stopped by operator"


async def test_failed_status_outlives_deregistration(tracked) -> None:
    supervisor, tracker, hub, scenario = tracked
    scenario["name"] = "exit-after-ready"
    queue = hub.subscribe()
    await supervisor.start(1, "/clone")
    await wait_for_status(queue, "failed")

    # The supervisor has dropped the handle, but the status sticks.
    async with asyncio.timeout(5):
        while supervisor.get(1) is not None:
            await asyncio.sleep(0.01)
    assert tracker.get(1).status == "failed"
    assert 1 in tracker.snapshot()


async def test_cleanup_discards_entry(tracked) -> None:
    supervisor, tracker, hub, scenario = tracked
    scenario["name"] = "exit-after-ready"
    queue = hub.subscribe()
    await supervisor.start(1, "/clone")
    await wait_for_status(queue, "failed")

    tracker.discard(1)
    assert tracker.get(1) is None
    assert tracker.snapshot() == {}


async def test_prompt_skipped_goes_idle_from_starting(tracked) -> None:
    supervisor, tracker, hub, _ = tracked
    queue = hub.subscribe()
    await supervisor.start(1, "/clone")
    tracker.prompt_skipped(1)

    transitions = await wait_for_status(queue, "idle")
    assert transitions[-1]["reason"] == "ready, no prompt to send"
    await supervisor.stop(1)


def test_snapshot_shape(tracked) -> None:
    _, tracker, hub, _ = tracked
    assert tracker.snapshot() == {}
