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
    MissingResumeIdentityError,
    ModelConfigurationError,
    NoLiveAgentError,
    SessionBusyError,
    build_agent_argv,
    role_flag_value,
)
from ompire_daemon.config import Config
from ompire_daemon.events import Event, EventHub
from ompire_daemon.rpc import AgentGoneError
from ompire_daemon.sessions import SessionTracker
from tests.conftest import fake_argv_builder, fake_sandbox_start, make_test_policy
from tests.test_rpc import fake_omp_argv


async def start_fake(scenario: str = "happy", **kwargs) -> AgentHandle:
    kwargs.setdefault("ready_timeout", 5)
    kwargs.setdefault("ring_buffer_size", 100)
    # Started with the real native flags so the fake reports the policy back
    # through `get_state`, the way the daemon's handshake expects.
    argv = build_agent_argv(policy=make_test_policy())
    # A contract test may build its own host-side process before adopting it;
    # production always starts the process through the resource boundary.
    process = await asyncio.create_subprocess_exec(
        *fake_omp_argv(scenario, *argv[argv.index("--no-title") + 1 :]),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=4 * 1024 * 1024,
    )
    return await AgentHandle.start(process, **kwargs)


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
    argv = build_agent_argv(policy=make_test_policy())
    assert argv[:5] == ["omp", "--mode", "rpc-ui", "--no-title", "--model"]
    # No environment-injection prefix (ADR-0015).
    assert "env" not in argv
    # Sessions stay ON and the nonexistent -s flag is never used (design D-2).
    assert "--no-session" not in argv
    assert "-s" not in argv


def test_build_agent_argv_resume_appends_flag() -> None:
    argv = build_agent_argv(policy=make_test_policy(), resume="sess-abc")
    assert argv[-2:] == ["--resume", "sess-abc"]


def test_build_agent_argv_no_resume_by_default() -> None:
    argv = build_agent_argv(policy=make_test_policy())
    assert "--resume" not in argv


def test_build_agent_argv_carries_every_role_pair() -> None:
    """All four roles reach the child, each with its own thinking level
    (ADR-0026). Flags and the `provider/model-id:LEVEL` encoding verified
    against omp v18.1.10."""
    argv = build_agent_argv(policy=make_test_policy())
    assert argv[argv.index("--model") + 1] == "testing/main-model"
    assert argv[argv.index("--thinking") + 1] == "medium"
    assert argv[argv.index("--smol") + 1] == "testing/smol-model:low"
    assert argv[argv.index("--slow") + 1] == "testing/slow-model:high"
    assert argv[argv.index("--plan") + 1] == "testing/plan-model:xhigh"


def test_build_agent_argv_never_omits_the_policy() -> None:
    """There is no "unset means omp's default" case: inheriting the host's
    model settings is what a profile exists to prevent."""
    argv = build_agent_argv(policy=make_test_policy())
    for flag in ("--model", "--thinking", "--smol", "--slow", "--plan"):
        assert flag in argv


def test_role_flag_value_keeps_nested_model_ids_intact() -> None:
    """Only the *first* slash separates provider from model id, so a nested
    catalog path survives into the flag."""
    from ompire_daemon.model_config import RoleBinding
    from ompire_daemon.work.inputs import split_model_identifier

    binding = RoleBinding(model="vendor/family/model-9", thinking="low")
    assert role_flag_value(binding) == "vendor/family/model-9:low"
    assert split_model_identifier(binding.model) == ("vendor", "family/model-9")


async def test_start_refuses_a_child_running_a_different_model() -> None:
    """omp fuzzy-matches `--model`, so a started child is not proof it obeyed:
    the supervisor reads the active model back and refuses to prompt when it
    disagrees with the accepted policy (ADR-0026)."""
    handle = await start_fake()
    policy = make_test_policy(default={"model": "testing/other-model", "thinking": "medium"})
    with pytest.raises(ModelConfigurationError) as exc_info:
        await handle.apply_model_policy(policy, reassert=False)
    assert "substituted model" in str(exc_info.value)
    await handle.kill()


async def test_resume_reasserts_the_accepted_pair_before_any_prompt() -> None:
    """A resumed omp restores its own model settings from the session file,
    so the accepted pair is re-asserted over the acknowledged native controls
    and then verified."""
    handle = await start_fake()
    policy = make_test_policy()
    state = await handle.apply_model_policy(policy, reassert=True)
    assert state.model == "testing/main-model"
    assert state.thinking_level == "medium"
    await handle.kill()


async def test_refused_model_is_not_silently_accepted() -> None:
    """Real omp answers `success: false` and leaves the previous model in
    place for an unknown id (v18.1.10 probe); that must fail, not fall back."""
    handle = await start_fake()
    policy = make_test_policy(default={"model": "testing/unknown-model", "thinking": "low"})
    with pytest.raises(ModelConfigurationError) as exc_info:
        await handle.apply_model_policy(policy, reassert=True)
    assert "Model not found" in str(exc_info.value)
    await handle.kill()


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
        fake_argv_builder(scenario),
    )
    monkeypatch.setattr(agent_module, "start_sandbox_process", fake_sandbox_start)

    async def no_preflight(clone_path: str) -> None:
        return None

    monkeypatch.setattr(agent_module, "verify_ask_timeout", no_preflight)
    hub = EventHub()
    config = Config(agent_ready_timeout=5, agent_ring_buffer_size=100)
    return AgentSupervisor(config, hub), hub, scenario


async def test_supervisor_start_get_stop(supervisor) -> None:
    sup, hub, _ = supervisor
    hub_queue = hub.subscribe()
    handle = await sup.start(1, "main", "/clone", policy=make_test_policy())
    assert sup.get(1, "main") is handle
    with pytest.raises(AgentAlreadyRunningError):
        await sup.start(1, "main", "/clone", policy=make_test_policy())
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
    scenario["name"] = "exit-after-start"
    hub_queue = hub.subscribe()
    await sup.start(2, "main", "/clone", policy=make_test_policy())
    event = await asyncio.wait_for(hub_queue.get(), timeout=5)
    assert event.type == "agent_exited"
    assert event.payload == {"task_id": 2, "session": "main", "exit_code": 7}


async def test_supervisor_resume_appends_resume_flag(monkeypatch) -> None:
    hub = EventHub()
    config = Config(agent_ready_timeout=5, agent_ring_buffer_size=100)
    sup = AgentSupervisor(config, hub)
    captured = {}

    build = fake_argv_builder("happy")

    def fake_build(*, policy, resume=None):
        captured["resume"] = resume
        return build(policy=policy, resume=resume)

    monkeypatch.setattr(agent_module, "build_agent_argv", fake_build)
    monkeypatch.setattr(agent_module, "start_sandbox_process", fake_sandbox_start)

    async def no_preflight(clone_path: str) -> None:
        return None

    monkeypatch.setattr(agent_module, "verify_ask_timeout", no_preflight)

    await sup.start(1, "main", "/clone", policy=make_test_policy(), resume="sess-abc")
    assert captured["resume"] == "sess-abc"
    await sup.stop(1, "main")


async def test_supervisor_threads_the_whole_policy(monkeypatch) -> None:
    """The supervisor passes the complete role map through, and records what
    the child reports back on the handle so a later step can tell whether a
    cached session is running the policy it needs (ADR-0026)."""
    hub = EventHub()
    config = Config(agent_ready_timeout=5, agent_ring_buffer_size=100)
    sup = AgentSupervisor(config, hub)
    captured = {}
    build = fake_argv_builder("happy")

    def fake_build(*, policy, resume=None):
        captured["policy"] = policy
        return build(policy=policy, resume=resume)

    monkeypatch.setattr(agent_module, "build_agent_argv", fake_build)
    monkeypatch.setattr(agent_module, "start_sandbox_process", fake_sandbox_start)

    async def no_preflight(clone_path: str) -> None:
        return None

    monkeypatch.setattr(agent_module, "verify_ask_timeout", no_preflight)

    policy = make_test_policy()
    handle = await sup.start(1, "main", "/clone", policy=policy)
    assert captured["policy"] == policy
    assert handle.policy == policy
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
        fake_argv_builder(scenario),
    )
    monkeypatch.setattr(agent_module, "start_sandbox_process", fake_sandbox_start)

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

    await sup.start(1, "main", "/clone", policy=make_test_policy(), resume="sess-abc")

    # `agent_spawning`'s generic "agent spawned" reason is skipped for a
    # resume (design D-4): the recovery reason painted before the resume
    # call started is left in place until the caller drives it further.
    assert tracker.get(1, "main").status == "starting"
    assert tracker.get(1, "main").reason == "recovering after daemon restart"
    await sup.stop(1, "main")


async def test_supervisor_shutdown_terminates_without_marking_failed(tracked_supervisor) -> None:
    sup, tracker, hub, _ = tracked_supervisor
    hub_queue = hub.subscribe()
    await sup.start(1, "main", "/clone", policy=make_test_policy())
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


# --- between-turn model policy handoff (ADR-0027) ----------------------------


def _policy(**roles) -> object:
    """A policy differing from `make_test_policy()` in exactly the named
    roles, so a test says which dimension of the transition it is about."""
    return make_test_policy(**roles)


@pytest.fixture
def handoff(monkeypatch: pytest.MonkeyPatch):
    """A supervisor over the fake omp, plus the resume arguments its spawns
    were built with — the only way to tell a replacement that resumed the
    native session from one that started a fresh conversation."""
    scenario = {"name": "happy"}
    build = fake_argv_builder(scenario)
    resumes: list[str | None] = []

    def fake_build(*, policy, resume=None):
        resumes.append(resume)
        return build(policy=policy, resume=resume)

    monkeypatch.setattr(agent_module, "build_agent_argv", fake_build)
    monkeypatch.setattr(agent_module, "start_sandbox_process", fake_sandbox_start)

    async def no_preflight(clone_path: str) -> None:
        return None

    monkeypatch.setattr(agent_module, "verify_ask_timeout", no_preflight)
    hub = EventHub()
    config = Config(agent_ready_timeout=5, agent_ring_buffer_size=100, shutdown_grace=2)
    return AgentSupervisor(config, hub), hub, scenario, resumes


async def test_unchanged_policy_keeps_the_process_and_still_verifies_it(handoff) -> None:
    """A cached handle is not evidence: the active pair is reasserted and read
    back before the prompt, so an externally changed model cannot masquerade
    as the accepted policy."""
    sup, _, _, resumes = handoff
    policy = make_test_policy()
    commits: list[int] = []
    first = await sup.start(1, "main", "/clone", policy=policy)

    same = await sup.apply_session_policy(
        1, "main", "/clone", policy=policy, commit=lambda: commits.append(1)
    )

    assert same is first
    assert resumes == [None]  # nothing was replaced
    assert commits == [1]
    await sup.stop(1, "main")


async def test_an_active_only_change_reconfigures_in_place(handoff) -> None:
    """Only the active pair differs, and omp has acknowledged controls for
    exactly that — so the conversation is kept without a restart."""
    sup, hub, _, resumes = handoff
    hub_queue = hub.subscribe()
    first = await sup.start(1, "main", "/clone", policy=make_test_policy())

    changed = _policy(default={"model": "testing/other-model", "thinking": "high"})
    same = await sup.apply_session_policy(
        1, "main", "/clone", policy=changed, commit=lambda: None
    )

    assert same is first
    assert same.policy == changed
    assert resumes == [None]
    assert hub_queue.empty()  # no exit, no replacement
    await sup.stop(1, "main")


async def test_an_auxiliary_change_replaces_the_process_and_resumes_the_session(
    handoff,
) -> None:
    """omp v18.1.10 has no auxiliary-role setter — `--smol`/`--slow`/`--plan`
    are start-time flags — so the only honest way to change one is to replace
    the child and resume its native session."""
    sup, hub, _, resumes = handoff
    hub_queue = hub.subscribe()
    first = await sup.start(1, "main", "/clone", policy=make_test_policy())
    first.events.append(Event(type="agent_end", payload={"marker": "before"}))

    changed = _policy(slow={"model": "testing/other-slow", "thinking": "xhigh"})
    replacement = await sup.apply_session_policy(
        1, "main", "/clone", policy=changed, commit=lambda: None
    )

    assert replacement is not first
    assert first.returncode is not None
    assert sup.get(1, "main") is replacement
    # The replacement resumed the recorded native session rather than opening
    # a new conversation.
    assert resumes == [None, "fake-session-id"]
    # The transcript the session already showed is carried forward.
    assert any(e.payload.get("marker") == "before" for e in replacement.snapshot())
    # A handoff is not a crash: the retired child's exit publishes nothing.
    await asyncio.sleep(0.1)
    assert hub_queue.empty()
    await sup.stop(1, "main")


async def test_a_retired_child_cannot_unregister_its_replacement(handoff) -> None:
    """The old exit watcher must not fire late and drop the handle the
    session is now using."""
    sup, _, _, _ = handoff
    await sup.start(1, "main", "/clone", policy=make_test_policy())
    changed = _policy(plan={"model": "testing/other-plan", "thinking": "off"})
    replacement = await sup.apply_session_policy(
        1, "main", "/clone", policy=changed, commit=lambda: None
    )

    await asyncio.sleep(0.2)
    assert sup.get(1, "main") is replacement
    assert replacement.returncode is None
    await sup.stop(1, "main")


async def test_a_busy_session_refuses_the_transition_without_interrupting_it(
    handoff,
) -> None:
    """Configuration never aborts work in flight: the transition is refused
    and the caller fails through the ordinary infrastructure path."""
    sup, _, scenario, _ = handoff
    scenario["name"] = "busy"
    handle = await sup.start(1, "main", "/clone", policy=make_test_policy())
    committed: list[int] = []

    changed = _policy(slow={"model": "testing/other-slow", "thinking": "off"})
    with pytest.raises(SessionBusyError, match="not at a turn boundary"):
        await sup.apply_session_policy(
            1, "main", "/clone", policy=changed, commit=lambda: committed.append(1)
        )

    # The turn is untouched and nothing was recorded as applied.
    assert sup.get(1, "main") is handle
    assert handle.returncode is None
    assert committed == []
    await sup.stop(1, "main")


async def test_a_refused_reconfiguration_leaves_no_promptable_handle(handoff) -> None:
    """A child that answered part of the handshake is on an unknown active
    pair. Keeping it would send the next turn under settings nobody verified."""
    sup, _, _, _ = handoff
    await sup.start(1, "main", "/clone", policy=make_test_policy())
    committed: list[int] = []

    # Real omp answers `success: false` and leaves the previous model in
    # place when the id is unknown (v18.1.10 probe); the fake reproduces it.
    changed = _policy(default={"model": "testing/unknown-model", "thinking": "low"})
    with pytest.raises(ModelConfigurationError, match="refused model"):
        await sup.apply_session_policy(
            1, "main", "/clone", policy=changed, commit=lambda: committed.append(1)
        )

    assert sup.get(1, "main") is None
    assert committed == []


async def test_a_failed_applied_state_write_kills_the_candidate(handoff) -> None:
    """The applied record is the thing a restart trusts. If it cannot be
    written, the process it describes must not survive to be prompted."""
    sup, _, _, _ = handoff
    await sup.start(1, "main", "/clone", policy=make_test_policy())

    def explode() -> None:
        raise RuntimeError("disk is full")

    changed = _policy(default={"model": "testing/other-model", "thinking": "low"})
    with pytest.raises(ModelConfigurationError, match="could not be recorded"):
        await sup.apply_session_policy(
            1, "main", "/clone", policy=changed, commit=explode
        )

    assert sup.get(1, "main") is None


async def test_a_replacement_that_resumes_a_different_session_is_refused(
    handoff, monkeypatch
) -> None:
    """A reported session id is not proof the conversation came back: omp
    starts a new session when the recorded one names nothing."""
    sup, _, _, _ = handoff
    await sup.start(1, "main", "/clone", policy=make_test_policy())

    real_read = AgentHandle.read_session_id
    calls = {"n": 0}

    async def drifting(self):
        calls["n"] += 1
        # First call captures the identity to resume; the readback after the
        # restart reports a different one.
        return "fake-session-id" if calls["n"] == 1 else "some-other-session"

    monkeypatch.setattr(AgentHandle, "read_session_id", drifting)
    changed = _policy(smol={"model": "testing/other-smol", "thinking": "off"})
    with pytest.raises(ModelConfigurationError, match="different conversation"):
        await sup.apply_session_policy(
            1, "main", "/clone", policy=changed, commit=lambda: None
        )

    assert sup.get(1, "main") is None
    monkeypatch.setattr(AgentHandle, "read_session_id", real_read)


async def test_a_replacement_without_a_resume_identity_is_refused(handoff, monkeypatch) -> None:
    """Starting fresh would silently drop the transcript; failing keeps the
    workspace and the history intact."""
    sup, _, _, resumes = handoff
    handle = await sup.start(1, "main", "/clone", policy=make_test_policy())

    async def unnamed(self):
        return None

    monkeypatch.setattr(AgentHandle, "read_session_id", unnamed)
    changed = _policy(smol={"model": "testing/other-smol", "thinking": "off"})
    with pytest.raises(MissingResumeIdentityError):
        await sup.apply_session_policy(
            1, "main", "/clone", policy=changed, commit=lambda: None
        )

    # Nothing was stopped and nothing was started: the refusal comes before
    # the live child is touched.
    assert sup.get(1, "main") is handle
    assert resumes == [None]


async def test_a_prompting_caller_waits_for_a_handoff_rather_than_racing_it(
    handoff,
) -> None:
    """`acquire` takes the same boundary, so a follow-up cannot be handed a
    child that is already being retired."""
    sup, _, _, _ = handoff
    await sup.start(1, "main", "/clone", policy=make_test_policy())
    changed = _policy(plan={"model": "testing/other-plan", "thinking": "off"})

    handoff_task = asyncio.create_task(
        sup.apply_session_policy(1, "main", "/clone", policy=changed, commit=lambda: None)
    )
    await asyncio.sleep(0)
    acquired = await sup.acquire(1, "main")
    replacement = await handoff_task

    assert acquired is replacement
    assert acquired.returncode is None
    await sup.stop(1, "main")
