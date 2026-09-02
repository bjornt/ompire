"""AgentHandle and AgentSupervisor tests against the fake omp fixture."""

from __future__ import annotations

import asyncio

import pytest

from ompire_daemon import agent as agent_module
from ompire_daemon.agent import (
    EVENT_STREAM_END,
    AgentAlreadyRunningError,
    AgentHandle,
    AgentStartError,
    AgentSupervisor,
    NoLiveAgentError,
    build_agent_argv,
)
from ompire_daemon.config import Config
from ompire_daemon.events import EventHub
from ompire_daemon.rpc import AgentGoneError
from ompire_daemon.sessions import SessionTracker
from tests.test_rpc import fake_omp_argv


async def start_fake(scenario: str = "happy", **kwargs) -> AgentHandle:
    kwargs.setdefault("ready_timeout", 5)
    kwargs.setdefault("ring_buffer_size", 100)
    return await AgentHandle.start(fake_omp_argv(scenario), **kwargs)


async def drain_until(queue: asyncio.Queue, event_type: str, timeout: float = 5.0) -> list:
    """Pull events off `queue` until one of `event_type` arrives (inclusive)."""
    seen = []
    async with asyncio.timeout(timeout):
        while True:
            event = await queue.get()
            seen.append(event)
            if event is not EVENT_STREAM_END and event.type == event_type:
                return seen


async def test_handshake_success() -> None:
    handle = await start_fake()
    assert handle.returncode is None
    await handle.kill()


async def test_handshake_timeout_kills_child() -> None:
    with pytest.raises(AgentStartError, match="no ready frame within"):
        await start_fake("silent", ready_timeout=0.3)


async def test_startup_failure_captures_stderr() -> None:
    with pytest.raises(AgentStartError, match="exited before ready") as excinfo:
        await start_fake("crash")
    assert "No models available" in excinfo.value.stderr


async def test_prompt_ack_with_interleaved_events() -> None:
    handle = await start_fake()
    queue = handle.subscribe()
    response = await asyncio.wait_for(handle.prompt("hi"), timeout=5)
    assert response["success"] is True
    seen = await drain_until(queue, "agent_end")
    types = [event.type for event in seen]
    assert "extension_ui_request" in types
    assert types.index("agent_start") < types.index("agent_end")
    await handle.kill()


async def test_response_failure() -> None:
    handle = await start_fake()
    from ompire_daemon.rpc import RequestFailedError

    with pytest.raises(RequestFailedError, match="boom"):
        await asyncio.wait_for(handle.prompt("fail"), timeout=5)
    await handle.kill()


async def test_exit_detected_with_code() -> None:
    handle = await start_fake("exit-after-ready")
    code = await asyncio.wait_for(handle.wait_exited(), timeout=5)
    assert code == 7


async def test_exit_fails_inflight_request_and_sends_sentinel() -> None:
    handle = await start_fake()
    queue = handle.subscribe()
    with pytest.raises(AgentGoneError):
        await asyncio.wait_for(handle.prompt("die"), timeout=5)
    assert await asyncio.wait_for(handle.wait_exited(), timeout=5) == 23
    async with asyncio.timeout(5):
        while True:
            if await queue.get() is EVENT_STREAM_END:
                break


async def test_ring_buffer_replays_in_order_and_caps_size() -> None:
    handle = await start_fake(ring_buffer_size=5)
    queue = handle.subscribe()
    await asyncio.wait_for(handle.prompt("hi"), timeout=5)
    live = await drain_until(queue, "agent_end")
    replay = handle.snapshot()
    assert len(replay) == 5  # capped at ring size, keeping the most recent
    assert [event.type for event in replay] == [event.type for event in live[-5:]]
    await handle.kill()


def test_build_agent_argv_recipe() -> None:
    argv = build_agent_argv("/clones/t1")
    assert argv == [
        "workshop", "exec", "-p", "/clones/t1", "--",
        "omp", "--mode", "rpc-ui", "--no-title",
    ]
    # No environment-injection prefix (ADR-0015).
    assert "env" not in argv
    # Sessions stay ON and the nonexistent -s flag is never used (design D-2).
    assert "--no-session" not in argv
    assert "-s" not in argv


def test_build_agent_argv_resume_appends_flag() -> None:
    argv = build_agent_argv("/clones/t1", resume="sess-abc")
    assert argv[-2:] == ["--resume", "sess-abc"]


def test_build_agent_argv_no_resume_by_default() -> None:
    argv = build_agent_argv("/clones/t1")
    assert "--resume" not in argv


def test_build_agent_argv_model_and_thinking() -> None:
    argv = build_agent_argv("/clones/t1", model="fable-5", thinking="high")
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "fable-5"
    assert "--thinking" in argv
    assert argv[argv.index("--thinking") + 1] == "high"


def test_build_agent_argv_omits_unset_model_thinking() -> None:
    argv = build_agent_argv("/clones/t1")
    assert "--model" not in argv
    assert "--thinking" not in argv


def test_build_agent_argv_model_only() -> None:
    argv = build_agent_argv("/clones/t1", model="fable-5")
    assert "--model" in argv
    assert "--thinking" not in argv


async def test_read_session_id_from_get_state() -> None:
    handle = await start_fake()
    session_id = await handle.read_session_id()
    assert session_id == "fake-session-id"
    await handle.kill()


async def test_read_session_id_returns_none_on_request_failure() -> None:
    handle = await start_fake("get-state-fails")
    assert await handle.read_session_id() is None
    await handle.kill()


async def test_terminate_sigterm_exits_promptly() -> None:
    handle = await start_fake()
    await asyncio.wait_for(handle.terminate(grace=5), timeout=5)
    assert handle.returncode is not None


async def test_terminate_falls_back_to_sigkill_for_wedged_child() -> None:
    handle = await start_fake("ignore-term")
    await asyncio.wait_for(handle.terminate(grace=0.3), timeout=5)
    assert handle.returncode is not None


async def test_terminate_is_idempotent_after_exit() -> None:
    handle = await start_fake("exit-after-ready")
    await handle.wait_exited()
    await asyncio.wait_for(handle.terminate(grace=1), timeout=5)
    assert handle.returncode == 7


@pytest.fixture
def supervisor(monkeypatch: pytest.MonkeyPatch):
    """A supervisor whose spawns hit the fake omp and skip the container
    preflight; tests flip `scenario` to exercise failure paths."""
    scenario = {"name": "happy"}
    monkeypatch.setattr(
        agent_module,
        "build_agent_argv",
        lambda clone, resume=None, model=None, thinking=None: fake_omp_argv(scenario["name"]),
    )

    async def no_preflight(clone_path: str) -> None:
        return None

    monkeypatch.setattr(agent_module, "verify_ask_timeout", no_preflight)
    hub = EventHub()
    config = Config(agent_ready_timeout=5, agent_ring_buffer_size=100)
    return AgentSupervisor(config, hub), hub, scenario


async def test_supervisor_start_get_stop(supervisor) -> None:
    sup, hub, _ = supervisor
    hub_queue = hub.subscribe()
    handle = await sup.start(1, "main", "/clone")
    assert sup.get(1, "main") is handle
    with pytest.raises(AgentAlreadyRunningError):
        await sup.start(1, "main", "/clone")
    await sup.stop(1, "main")
    event = await asyncio.wait_for(hub_queue.get(), timeout=5)
    assert event.type == "agent_exited"
    assert event.payload["task_id"] == 1
    assert event.payload["session"] == "main"
    assert event.payload["exit_code"] != 0  # killed
    # The handle is dropped once the waiter has published.
    async with asyncio.timeout(5):
        while sup.get(1, "main") is not None:
            await asyncio.sleep(0.01)


async def test_supervisor_stop_without_agent() -> None:
    sup = AgentSupervisor(Config(), EventHub())
    with pytest.raises(NoLiveAgentError):
        await sup.stop(42, "main")


async def test_supervisor_publishes_exit_code_on_crash(supervisor) -> None:
    sup, hub, scenario = supervisor
    scenario["name"] = "exit-after-ready"
    hub_queue = hub.subscribe()
    await sup.start(2, "main", "/clone")
    event = await asyncio.wait_for(hub_queue.get(), timeout=5)
    assert event.type == "agent_exited"
    assert event.payload == {"task_id": 2, "session": "main", "exit_code": 7}


async def test_supervisor_resume_appends_resume_flag(monkeypatch) -> None:
    hub = EventHub()
    config = Config(agent_ready_timeout=5, agent_ring_buffer_size=100)
    sup = AgentSupervisor(config, hub)
    captured = {}

    def fake_build(clone, resume=None, model=None, thinking=None):
        captured["resume"] = resume
        return fake_omp_argv("happy")

    monkeypatch.setattr(agent_module, "build_agent_argv", fake_build)

    async def no_preflight(clone_path: str) -> None:
        return None

    monkeypatch.setattr(agent_module, "verify_ask_timeout", no_preflight)

    await sup.start(1, "main", "/clone", resume="sess-abc")
    assert captured["resume"] == "sess-abc"
    await sup.stop(1, "main")


async def test_supervisor_threads_model_and_thinking(monkeypatch) -> None:
    hub = EventHub()
    config = Config(agent_ready_timeout=5, agent_ring_buffer_size=100)
    sup = AgentSupervisor(config, hub)
    captured = {}

    def fake_build(clone, resume=None, model=None, thinking=None):
        captured["model"] = model
        captured["thinking"] = thinking
        return fake_omp_argv("happy")

    monkeypatch.setattr(agent_module, "build_agent_argv", fake_build)

    async def no_preflight(clone_path: str) -> None:
        return None

    monkeypatch.setattr(agent_module, "verify_ask_timeout", no_preflight)

    await sup.start(1, "main", "/clone", model="fable-5", thinking="high")
    assert captured["model"] == "fable-5"
    assert captured["thinking"] == "high"
    await sup.stop(1, "main")


@pytest.fixture
def tracked_supervisor(monkeypatch: pytest.MonkeyPatch):
    """Like `supervisor`, but wired to a real `SessionTracker` so recovery's
    tracker interactions (`recovering`, the exit watcher's shutdown skip) can
    be observed."""
    scenario = {"name": "happy"}
    monkeypatch.setattr(
        agent_module,
        "build_agent_argv",
        lambda clone, resume=None, model=None, thinking=None: fake_omp_argv(scenario["name"]),
    )

    async def no_preflight(clone_path: str) -> None:
        return None

    monkeypatch.setattr(agent_module, "verify_ask_timeout", no_preflight)
    hub = EventHub()
    tracker = SessionTracker(hub, idle_debounce=0.1)
    config = Config(agent_ready_timeout=5, agent_ring_buffer_size=100)
    return AgentSupervisor(config, hub, tracker), tracker, hub, scenario


async def test_supervisor_resume_does_not_clobber_recovering_reason(tracked_supervisor) -> None:
    sup, tracker, _, _ = tracked_supervisor
    tracker.recovering(1, "main")

    await sup.start(1, "main", "/clone", resume="sess-abc")

    # `agent_spawning`'s generic "agent spawned" reason is skipped for a
    # resume (design D-4): the recovery reason painted before the resume
    # call started is left in place until the caller drives it further.
    assert tracker.get(1, "main").status == "starting"
    assert tracker.get(1, "main").reason == "recovering after daemon restart"
    await sup.stop(1, "main")


async def test_supervisor_shutdown_terminates_without_marking_failed(tracked_supervisor) -> None:
    sup, tracker, hub, _ = tracked_supervisor
    hub_queue = hub.subscribe()
    await sup.start(1, "main", "/clone")
    assert tracker.get(1, "main").status == "starting"

    await asyncio.wait_for(sup.shutdown(), timeout=5)

    assert sup.get(1, "main") is None
    # No crash reported: no `agent_exited` event, no `failed` transition.
    assert tracker.get(1, "main").status == "starting"
    while not hub_queue.empty():
        event = hub_queue.get_nowait()
        assert event.type != "agent_exited"


async def test_verify_ask_timeout_accepts_zero(fake_workshop_cli, tmp_path) -> None:
    fake_workshop_cli.write_text("#!/bin/sh\necho 0\n")
    await agent_module.verify_ask_timeout(str(tmp_path))


async def test_verify_ask_timeout_rejects_nonzero(fake_workshop_cli, tmp_path) -> None:
    fake_workshop_cli.write_text('#!/bin/sh\necho "ask.timeout = 5"\n')
    with pytest.raises(AgentStartError, match="ask.timeout is '5'"):
        await agent_module.verify_ask_timeout(str(tmp_path))


async def test_verify_ask_timeout_rejects_command_failure(fake_workshop_cli, tmp_path) -> None:
    fake_workshop_cli.write_text('#!/bin/sh\necho "no such workshop" >&2\nexit 1\n')
    with pytest.raises(AgentStartError, match="cannot read ask.timeout"):
        await agent_module.verify_ask_timeout(str(tmp_path))
