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
# Deliberately much larger than DEBOUNCE/the sleeps unrelated tests use, so
# the stall watchdog never fires incidentally in tests that don't exercise it
# (e.g. a queued-message re-check sleeping past DEBOUNCE*3).
STALL_THRESHOLD = 30.0
FAST_STALL_THRESHOLD = 0.2


@pytest.fixture
def tracked(monkeypatch: pytest.MonkeyPatch):
    """Supervisor + tracker wired to the fake omp, with a fast debounce and a
    stall watchdog slow enough not to interfere with unrelated tests."""
    scenario = {"name": "happy"}
    monkeypatch.setattr(
        agent_module, "build_agent_argv", lambda clone, env: fake_omp_argv(scenario["name"])
    )

    async def no_preflight(clone_path: str) -> None:
        return None

    monkeypatch.setattr(agent_module, "verify_ask_timeout", no_preflight)
    hub = EventHub()
    tracker = SessionTracker(hub, idle_debounce=DEBOUNCE, stall_threshold=STALL_THRESHOLD)
    config = Config(agent_ready_timeout=5, agent_ring_buffer_size=100)
    supervisor = AgentSupervisor(config, hub, tracker)
    return supervisor, tracker, hub, scenario


@pytest.fixture
def tracked_stall(monkeypatch: pytest.MonkeyPatch):
    """Like `tracked`, but with a fast stall watchdog and a debounce slow
    enough not to fire during a stall test's timeframe."""
    scenario = {"name": "happy"}
    monkeypatch.setattr(
        agent_module, "build_agent_argv", lambda clone, env: fake_omp_argv(scenario["name"])
    )

    async def no_preflight(clone_path: str) -> None:
        return None

    monkeypatch.setattr(agent_module, "verify_ask_timeout", no_preflight)
    hub = EventHub()
    tracker = SessionTracker(hub, idle_debounce=5.0, stall_threshold=FAST_STALL_THRESHOLD)
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


async def test_ask_enters_waiting_input_and_posts_question(tracked) -> None:
    supervisor, tracker, hub, _ = tracked
    queue = hub.subscribe()
    handle = await supervisor.start(1, "/clone")
    await handle.prompt("hi")
    await wait_for_status(queue, "working")

    # Confirmed by dogfooding 2026-07-20: real omp emits `extension_ui_request`
    # *before* `tool_execution_start` for `ask`, so the daemon posts the
    # question provisionally (empty `questions`) and then upgrades it in
    # place — wait for the upgraded post rather than the first one.
    posted_queue = hub.subscribe()
    await handle.prompt("ask")
    transitions = await wait_for_status(queue, "waiting-input")
    assert transitions[-1]["from"] == "working"
    assert tracker.get(1).status == "waiting-input"

    async with asyncio.timeout(5):
        while True:
            event = await posted_queue.get()
            if event.type == "question_posted" and event.payload["question"]["questions"]:
                break

    pending = tracker.pending(1)
    assert pending is not None
    assert pending.kind == "ask"
    assert pending.questions[0].recommended == "Yes, both loops (Recommended)"
    assert pending.questions[0].options[0].value == "Yes, both loops (Recommended)"

    # Snapshot carries the pending question (reconnect scenario).
    snap = tracker.snapshot()
    assert snap[1]["question"]["id"] == pending.id

    await supervisor.stop(1)


async def test_answering_ask_clears_and_returns_to_working(tracked) -> None:
    # Mirrors what the answer endpoint (task 4.2) does: write the reply over
    # stdin, then tell the tracker the question was answered.
    supervisor, tracker, hub, _ = tracked
    queue = hub.subscribe()
    handle = await supervisor.start(1, "/clone")
    await handle.prompt("hi")
    await wait_for_status(queue, "working")

    await handle.prompt("ask")
    await wait_for_status(queue, "waiting-input")
    pending = tracker.pending(1)
    assert pending is not None

    resolved_queue = hub.subscribe()
    await handle.respond_ui_request(pending.id, {"value": "Yes, both loops (Recommended)"})
    resolved = tracker.answer_pending(1)
    assert resolved is not None and resolved.id == pending.id

    async with asyncio.timeout(5):
        while True:
            event = await resolved_queue.get()
            if event.type == "question_resolved":
                assert event.payload == {"task_id": 1, "question_id": pending.id}
                break

    assert tracker.pending(1) is None
    transitions = await wait_for_status(queue, "working")
    assert transitions[-1]["from"] == "waiting-input"
    assert transitions[-1]["reason"] == "operator answered the pending question"
    # The burst continues on to its own agent_end -> idle.
    await wait_for_status(queue, "idle")
    await supervisor.stop(1)


async def test_approval_enters_waiting_approval(tracked) -> None:
    supervisor, tracker, hub, _ = tracked
    queue = hub.subscribe()
    handle = await supervisor.start(1, "/clone")
    await handle.prompt("hi")
    await wait_for_status(queue, "working")

    await handle.prompt("approve")
    transitions = await wait_for_status(queue, "waiting-approval")
    assert transitions[-1]["from"] == "working"
    pending = tracker.pending(1)
    assert pending is not None
    assert pending.kind == "approval"

    await handle.respond_ui_request(pending.id, {"value": "Approve"})
    tracker.answer_pending(1)
    transitions = await wait_for_status(queue, "working")
    assert transitions[-1]["from"] == "waiting-approval"
    await wait_for_status(queue, "idle")
    assert tracker.pending(1) is None
    await supervisor.stop(1)


async def test_ask_cancelled_without_answer_returns_to_working(tracked) -> None:
    # Confirmed by dogfooding 2026-07-20: `tool_execution_end` must clear the
    # pending question and return the session to `working` on its own — not
    # only via the answer endpoint's optimistic transition — or a cancelled
    # ask leaves the session stuck in `waiting-input` forever.
    supervisor, tracker, hub, _ = tracked
    queue = hub.subscribe()
    handle = await supervisor.start(1, "/clone")
    await handle.prompt("hi")
    await wait_for_status(queue, "working")

    await handle.prompt("ask-cancel")
    transitions = await wait_for_status(queue, "waiting-input")
    assert transitions[-1]["from"] == "working"

    transitions = await wait_for_status(queue, "working")
    assert transitions[-1]["from"] == "waiting-input"
    assert transitions[-1]["reason"] == "ask tool_execution_end"
    assert tracker.pending(1) is None

    await wait_for_status(queue, "idle")
    await supervisor.stop(1)


async def test_silence_stalls_a_working_session(tracked_stall) -> None:
    supervisor, tracker, hub, _ = tracked_stall
    queue = hub.subscribe()
    handle = await supervisor.start(1, "/clone")
    # "no-end" bursts through agent_start with no trailing agent_end, so the
    # session stays working and silent past the stall threshold.
    await handle.prompt("no-end")

    transitions = await wait_for_status(queue, "stalled")
    assert transitions[-1]["from"] == "working"
    assert "no frames for" in transitions[-1]["reason"]
    assert tracker.get(1).status == "stalled"
    await supervisor.stop(1)


async def test_frame_recovers_a_stalled_session(tracked_stall) -> None:
    supervisor, tracker, hub, _ = tracked_stall
    queue = hub.subscribe()
    handle = await supervisor.start(1, "/clone")
    await handle.prompt("no-end")
    await wait_for_status(queue, "stalled")

    # Any interpreted frame recovers it; the next burst's leading
    # extension_ui_request (setWidget) is the first frame to arrive. This
    # burst also has no trailing agent_end, so the watchdog re-arms and the
    # session stalls again on its own silence.
    await handle.prompt("no-end")
    transitions = await wait_for_status(queue, "working")
    assert transitions[-1]["from"] == "stalled"
    assert "frame" in transitions[-1]["reason"]

    await wait_for_status(queue, "stalled")
    await supervisor.stop(1)


async def test_exit_during_stall_still_fails(tracked_stall) -> None:
    supervisor, tracker, hub, _ = tracked_stall
    queue = hub.subscribe()
    handle = await supervisor.start(1, "/clone")
    await handle.prompt("no-end")
    await wait_for_status(queue, "stalled")

    await supervisor.stop(1)

    transitions = await wait_for_status(queue, "failed")
    assert transitions[-1]["from"] == "stalled"
    assert tracker.get(1).status == "failed"
    # No late stall lands after the exit either.
    await asyncio.sleep(FAST_STALL_THRESHOLD * 3)
    assert tracker.get(1).status == "failed"


async def test_auto_retry_start_and_end_transitions(tracked) -> None:
    supervisor, tracker, hub, _ = tracked
    queue = hub.subscribe()
    handle = await supervisor.start(1, "/clone")

    await handle.prompt("auto-retry")

    transitions = await wait_for_status(queue, "retrying")
    assert transitions[-1]["from"] == "working"
    # Confirmed against the omp source (see `omp-rpc-field-assumptions`):
    # `attempt`/`maxAttempts`/`errorMessage`, not `reason`/`message`/`error`.
    assert transitions[-1]["reason"] == "auto_retry_start: HTTP 429 from gateway (attempt 1/5)"

    transitions = await wait_for_status(queue, "working")
    assert transitions[-1]["from"] == "retrying"
    assert transitions[-1]["reason"] == "auto_retry_end frame"

    await wait_for_status(queue, "idle")
    await supervisor.stop(1)


async def test_exit_during_retry_still_fails(tracked) -> None:
    supervisor, tracker, hub, _ = tracked
    queue = hub.subscribe()
    handle = await supervisor.start(1, "/clone")

    await handle.prompt("auto-retry-hang")
    await wait_for_status(queue, "retrying")

    await supervisor.stop(1)

    transitions = await wait_for_status(queue, "failed")
    assert transitions[-1]["from"] == "retrying"
    assert tracker.get(1).status == "failed"


async def test_exit_during_waiting_discards_pending_no_resolve(tracked) -> None:
    supervisor, tracker, hub, _ = tracked
    queue = hub.subscribe()
    handle = await supervisor.start(1, "/clone")
    await handle.prompt("hi")
    await wait_for_status(queue, "working")

    await handle.prompt("ask")
    await wait_for_status(queue, "waiting-input")
    assert tracker.pending(1) is not None

    resolved_queue = hub.subscribe()
    await supervisor.stop(1)

    transitions = await wait_for_status(queue, "failed")
    assert transitions[-1]["from"] == "waiting-input"
    assert tracker.pending(1) is None
    # No question_resolved: exit wins without a separate resolve broadcast.
    while not resolved_queue.empty():
        event = resolved_queue.get_nowait()
        assert event.type != "question_resolved"
