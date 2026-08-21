"""Spawn pipeline tests, driven against a real fixture git repo, a fake
my-workshop script, and the fake omp behind the fake workshop CLI (no real
containers).

The pipeline itself runs only the workspace steps (fetch/clone/branch/
workshop); agent start and prompt delivery are the workflow engine's job
(`workflow_step` events for the single-step run's `work` step on session
`main`). Because the handoff is async, tests that need the session up wait
for the run to settle (`_wait_workflow_settled`) before asserting."""

from __future__ import annotations

import asyncio
import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from ompire_daemon.agent import AgentSupervisor
from ompire_daemon.config import Config
from ompire_daemon.events import Event, EventHub
from ompire_daemon.registry.projects import create_project
from ompire_daemon.registry.sessions import get_session
from ompire_daemon.registry.tasks import (
    clone_path_for,
    create_task,
    get_task,
)
from ompire_daemon.registry.templates import create_template
from ompire_daemon.sessions import SessionTracker
from ompire_daemon.spawn import run_spawn_pipeline
from ompire_daemon.workflows import WorkflowRunner
from tests.conftest import FAKE_WORKSHOP_SCRIPT


@pytest.fixture
def fake_my_workshop(tmp_path: Path):
    """Factory for fake my-workshop scripts; returns the script path."""

    def make(body: str) -> str:
        script = tmp_path / "fake-my-workshop"
        script.write_text(f"#!/bin/sh\n{body}\n")
        script.chmod(0o755)
        return str(script)

    return make


@pytest.fixture
def pipeline(app):
    """A hub + tracker + supervisor trio sharing the app's config, plus a
    runner that drives the pipeline with them. The workflow engine the
    pipeline hands off to is built over the same trio, so session/status
    assertions observe the real engine's lazy spawn."""
    config: Config = app.state.config
    hub = EventHub()
    tracker = SessionTracker(hub, idle_debounce=0.1)
    supervisor = AgentSupervisor(config, hub, tracker)

    async def run(engine, task_id: int, run_config: Config | None = None, **overrides):
        effective = run_config or config
        runner = WorkflowRunner(engine, effective, hub, supervisor, tracker)
        await run_spawn_pipeline(engine, hub, effective, task_id, runner, **overrides)

    return run, hub, tracker, supervisor


def _make_project(engine, checkout: Path):
    return create_project(
        engine,
        name="demo",
        title="Demo",
        upstream_url="https://example.com/demo.git",
        checkout_path=str(checkout),
        default_checkout_root=checkout.parent,
    )


def _make_template(
    engine,
    checkout: Path,
    *,
    base_branch: str = "main",
    preamble: str = "",
    model: str | None = None,
    thinking: str | None = None,
):
    _make_project(engine, checkout)
    return create_template(
        engine,
        name="demo",
        project_name="demo",
        base_branch=base_branch,
        branch_pattern="ompire/<slug>",
        preamble=preamble,
        model=model,
        thinking=thinking,
    )


def _make_task(engine, config: Config, *, prompt: str = "fix the bug"):
    clone_path = clone_path_for(config.task_dir_root, "demo", "fix-bug")
    return create_task(
        engine,
        project_name="demo",
        template_name="demo",
        slug="fix-bug",
        branch="ompire/fix-bug",
        clone_path=str(clone_path),
        prompt=prompt,
    )


def _drain(queue) -> list[Event]:
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


async def _wait_workflow_settled(engine, task_id: int, timeout: float = 10.0):
    """Poll the registry until the task's workflow run reaches a terminal
    status. The pipeline hands the task to the engine asynchronously, so
    post-handoff state (live session, tracker status, workflow_step events)
    is only stable once the run is complete/failed."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        task = get_task(engine, task_id)
        if task.workflow_status in ("complete", "failed"):
            return task
        await asyncio.sleep(0.02)
    raise AssertionError("workflow run did not settle in time")


def _user_messages(handle) -> list[str]:
    """The user-role message texts echoed by fake omp in the handle's ring
    buffer — what the daemon actually prompted the session with."""
    return [
        event.payload["message"]["content"][0]["text"]
        for event in handle.snapshot()
        if event.type == "message_start"
        and event.payload.get("message", {}).get("role") == "user"
    ]


async def test_successful_pipeline(app, git_checkout: Path, fake_my_workshop, pipeline) -> None:
    engine = app.state.engine
    run, hub, tracker, supervisor = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop('echo "ws-demo-fix-bug" > .workshop.lock'),),
    )
    queue = hub.subscribe()

    _make_template(engine, git_checkout)
    task = _make_task(engine, config)

    await run(engine, task.id, config)
    settled = await _wait_workflow_settled(engine, task.id)

    clone = Path(task.clone_path)
    assert (clone / ".git").is_dir()
    head = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert head == "ompire/fix-bug"

    assert settled.state == "created"
    assert settled.spawn_completed_at is not None
    assert settled.workshop_id == "ws-demo-fix-bug"
    assert settled.workflow_status == "complete"
    # Session id captured after ready, recorded per session (crash-recovery
    # capability, design D-2).
    session = get_session(engine, task.id, "main")
    assert session is not None
    assert session.omp_session_id == "fake-session-id"

    # The agent is live with the stored prompt delivered.
    assert supervisor.get(task.id, "main") is not None
    assert tracker.get(task.id, "main") is not None

    drained = _drain(queue)
    spawn_steps = [e.payload for e in drained if e.type == "spawn_step"]
    assert [(p["step"], p["status"]) for p in spawn_steps] == [
        ("fetch", "started"),
        ("fetch", "ok"),
        ("clone", "started"),
        ("clone", "ok"),
        ("branch", "started"),
        ("branch", "ok"),
        ("workshop", "started"),
        ("workshop", "ok"),
    ]
    # Agent start + prompt delivery are the workflow run's `work` step now.
    workflow_steps = [e.payload for e in drained if e.type == "workflow_step"]
    assert [(p["step"], p["kind"], p["session"], p["status"]) for p in workflow_steps] == [
        ("work", "agent", "main", "started"),
        ("work", "agent", "main", "ok"),
    ]
    await supervisor.stop(task.id, "main")


async def test_empty_prompt_skips_prompt_step(
    app, git_checkout: Path, fake_my_workshop, pipeline
) -> None:
    engine = app.state.engine
    run, hub, tracker, supervisor = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop('echo "ws-x" > .workshop.lock'),),
    )
    queue = hub.subscribe()

    _make_template(engine, git_checkout)
    task = _make_task(engine, config, prompt="")

    await run(engine, task.id, config)
    settled = await _wait_workflow_settled(engine, task.id)

    assert settled.state == "created"
    assert settled.spawn_completed_at is not None
    assert settled.workflow_status == "complete"

    drained = _drain(queue)
    # No prompt/agent spawn steps exist at all: the pipeline is workspace-only.
    steps = [e.payload["step"] for e in drained if e.type == "spawn_step"]
    assert set(steps) == {"fetch", "clone", "branch", "workshop"}
    # The engine still spawns the session, then skips the empty prompt.
    workflow_steps = [
        (e.payload["step"], e.payload["status"]) for e in drained if e.type == "workflow_step"
    ]
    assert workflow_steps == [("work", "started"), ("work", "ok")]
    # Promptless task idles after ready instead of hanging in starting.
    assert tracker.get(task.id, "main").status == "idle"
    assert tracker.get(task.id, "main").reason == "ready, no prompt to send"
    await supervisor.stop(task.id, "main")


async def test_clone_excludes_outcome_dir_from_git(
    app, git_checkout: Path, fake_my_workshop, pipeline
) -> None:
    """The clone step appends `.ompire/` to `.git/info/exclude` (workflow
    engine design D-3), so a written `.ompire/outcome.json` never dirties
    `git status`."""
    engine = app.state.engine
    run, _, _, supervisor = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop('echo "ws-x" > .workshop.lock'),),
    )

    _make_template(engine, git_checkout)
    task = _make_task(engine, config)

    await run(engine, task.id, config)
    await _wait_workflow_settled(engine, task.id)

    clone = Path(task.clone_path)
    exclude_lines = (clone / ".git" / "info" / "exclude").read_text().splitlines()
    assert ".ompire/" in exclude_lines

    def status_porcelain() -> str:
        return subprocess.run(
            ["git", "-C", str(clone), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    before = status_porcelain()
    outcome = clone / ".ompire" / "outcome.json"
    outcome.parent.mkdir()
    outcome.write_text(json.dumps({"version": 1, "status": "success", "summary": "done"}))
    assert status_porcelain() == before
    await supervisor.stop(task.id, "main")


async def test_agent_step_failure_fails_the_run(
    app, git_checkout: Path, fake_my_workshop, fake_workshop_cli, pipeline
) -> None:
    engine = app.state.engine
    run, hub, tracker, _ = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop('echo "ws-x" > .workshop.lock'),),
    )
    # The rpc-ui spawn now crashes before ready with stderr.
    fake_workshop_cli.write_text(FAKE_WORKSHOP_SCRIPT.replace(" happy", " crash"))
    queue = hub.subscribe()

    _make_template(engine, git_checkout)
    task = _make_task(engine, config)

    await run(engine, task.id, config)
    settled = await _wait_workflow_settled(engine, task.id)

    # The workspace is fine — the agent spawn failure is the workflow run's
    # `work` step failing (task registry state stays `created`).
    assert settled.state == "created"
    assert settled.workflow_status == "failed"
    assert "agent" in (settled.error or "")
    assert "No models available" in (settled.error or "")

    drained = _drain(queue)
    spawn_failed = [
        e.payload for e in drained if e.type == "spawn_step" and e.payload["status"] == "failed"
    ]
    assert not spawn_failed
    workflow_failed = [
        e.payload for e in drained if e.type == "workflow_step" and e.payload["status"] == "failed"
    ]
    assert workflow_failed and workflow_failed[0]["step"] == "work"
    assert workflow_failed[0]["kind"] == "agent"
    assert workflow_failed[0]["session"] == "main"
    assert "No models available" in workflow_failed[0]["error"]
    # The session lands failed too (workflow-engine design D-2).
    assert tracker.get(task.id, "main").status == "failed"
    assert "session spawn failed" in tracker.get(task.id, "main").reason


async def test_session_id_capture_failure_leaves_it_null_and_spawn_succeeds(
    app, git_checkout: Path, fake_my_workshop, fake_workshop_cli, pipeline
) -> None:
    engine = app.state.engine
    run, hub, _tracker, supervisor = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop('echo "ws-x" > .workshop.lock'),),
    )
    # The agent starts fine, but `get_state` (used to capture the session id)
    # answers success: false — capture must degrade gracefully, not fail the
    # spawn (design D-2).
    fake_workshop_cli.write_text(FAKE_WORKSHOP_SCRIPT.replace(" happy", " get-state-fails"))
    queue = hub.subscribe()

    _make_template(engine, git_checkout)
    task = _make_task(engine, config)

    await run(engine, task.id, config)
    settled = await _wait_workflow_settled(engine, task.id)

    assert settled.state == "created"
    assert settled.spawn_completed_at is not None
    assert settled.workflow_status == "complete"
    session = get_session(engine, task.id, "main")
    assert session is not None
    assert session.omp_session_id is None

    workflow_steps = [
        (e.payload["step"], e.payload["status"])
        for e in _drain(queue)
        if e.type == "workflow_step"
    ]
    assert ("work", "started") in workflow_steps
    assert ("work", "ok") in workflow_steps
    await supervisor.stop(task.id, "main")


async def test_prompt_failure_fails_the_run(
    app, git_checkout: Path, fake_my_workshop, pipeline
) -> None:
    engine = app.state.engine
    run, _hub, _tracker, supervisor = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop('echo "ws-x" > .workshop.lock'),),
    )
    _make_template(engine, git_checkout)
    # The magic "fail" prompt makes fake omp answer success: false, "boom".
    task = _make_task(engine, config, prompt="fail")

    await run(engine, task.id, config)
    settled = await _wait_workflow_settled(engine, task.id)

    assert settled.state == "created"
    assert settled.workflow_status == "failed"
    assert "prompt" in (settled.error or "")
    assert "boom" in (settled.error or "")
    # A prompt-delivery failure is not a session death: the live session
    # stays up as the operator's escape hatch (workflow-engine design D-2).
    assert supervisor.get(task.id, "main") is not None
    await supervisor.stop(task.id, "main")


async def test_step_failure_captures_stderr(app, git_checkout: Path, pipeline) -> None:
    engine = app.state.engine
    run, hub, _, _ = pipeline
    queue = hub.subscribe()

    _make_template(engine, git_checkout, base_branch="no-such-branch")
    task = _make_task(engine, app.state.config)

    await run(engine, task.id)

    refreshed = get_task(engine, task.id)
    assert refreshed.state == "failed"
    assert refreshed.error is not None and "branch" in refreshed.error
    drained = _drain(queue)
    failed_steps = [
        e.payload for e in drained if e.type == "spawn_step" and e.payload["status"] == "failed"
    ]
    assert len(failed_steps) == 1
    assert failed_steps[0]["step"] == "branch"
    assert failed_steps[0]["stderr"]
    task_updates = [e.payload for e in drained if e.type == "task_updated"]
    assert task_updates[-1]["state"] == "failed"


async def test_leftover_directory_fails(app, git_checkout: Path, pipeline) -> None:
    engine = app.state.engine
    run, hub, _, _ = pipeline
    queue = hub.subscribe()

    _make_template(engine, git_checkout)
    task = _make_task(engine, app.state.config)
    Path(task.clone_path).mkdir(parents=True)

    await run(engine, task.id)

    refreshed = get_task(engine, task.id)
    assert refreshed.state == "failed"
    assert "already exists" in (refreshed.error or "")
    failed_steps = [
        e.payload for e in _drain(queue) if e.type == "spawn_step" and e.payload["status"] == "failed"
    ]
    assert failed_steps and failed_steps[0]["step"] == "clone"


async def test_workshop_step_failure(app, git_checkout: Path, fake_my_workshop, pipeline) -> None:
    engine = app.state.engine
    run, hub, _, _ = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop('echo "launch exploded" >&2; exit 3'),),
    )
    queue = hub.subscribe()

    _make_template(engine, git_checkout)
    task = _make_task(engine, config)

    await run(engine, task.id, config)

    refreshed = get_task(engine, task.id)
    assert refreshed.state == "failed"
    assert refreshed.workshop_id is None
    assert "workshop" in (refreshed.error or "")
    assert "launch exploded" in (refreshed.error or "")

    failed_steps = [
        e.payload for e in _drain(queue) if e.type == "spawn_step" and e.payload["status"] == "failed"
    ]
    assert failed_steps and failed_steps[0]["step"] == "workshop"
    assert "launch exploded" in failed_steps[0]["stderr"]


async def test_workshop_step_timeout(app, git_checkout: Path, fake_my_workshop, pipeline) -> None:
    engine = app.state.engine
    run, _, _, _ = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop("sleep 5"),),
        workshop_step_timeout=1,
    )

    _make_template(engine, git_checkout)
    task = _make_task(engine, config)

    await run(engine, task.id, config)

    refreshed = get_task(engine, task.id)
    assert refreshed.state == "failed"
    assert "timed out after 1s" in (refreshed.error or "")


async def test_workshop_lock_missing_after_success(
    app, git_checkout: Path, fake_my_workshop, pipeline
) -> None:
    engine = app.state.engine
    run, hub, _, _ = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop("exit 0"),),
    )
    queue = hub.subscribe()

    _make_template(engine, git_checkout)
    task = _make_task(engine, config)

    await run(engine, task.id, config)

    refreshed = get_task(engine, task.id)
    assert refreshed.state == "failed"
    assert refreshed.workshop_id is None
    assert ".workshop.lock" in (refreshed.error or "")

    failed_steps = [
        e.payload for e in _drain(queue) if e.type == "spawn_step" and e.payload["status"] == "failed"
    ]
    assert failed_steps and failed_steps[0]["step"] == "workshop"


async def test_workshop_tool_missing(app, git_checkout: Path, pipeline) -> None:
    engine = app.state.engine
    run, _, _, _ = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=("/no/such/my-workshop",),
    )

    _make_template(engine, git_checkout)
    task = _make_task(engine, config)

    await run(engine, task.id, config)

    refreshed = get_task(engine, task.id)
    assert refreshed.state == "failed"
    assert "cannot exec" in (refreshed.error or "")


# --- Template-driven spawn (templates capability) ---------------------------


async def test_template_missing_at_pipeline_start_fails_before_git(
    app, git_checkout: Path, fake_my_workshop, pipeline
) -> None:
    engine = app.state.engine
    run, hub, tracker, _ = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop('echo "ws-x" > .workshop.lock'),),
    )
    queue = hub.subscribe()

    _make_template(engine, git_checkout)
    task = _make_task(engine, config)
    # The template disappears between the 202 and pipeline start (raw delete:
    # the REST guard blocks this once the task row exists — the race needs
    # the row gone underneath a live task, e.g. a DB restore).
    from ompire_daemon.db import templates as templates_table

    with engine.begin() as conn:
        conn.execute(templates_table.delete().where(templates_table.c.name == "demo"))

    await run(engine, task.id, config)

    refreshed = get_task(engine, task.id)
    assert refreshed.state == "failed"
    assert "template" in (refreshed.error or "")
    assert "'demo'" in (refreshed.error or "")
    # No clone ever happened: the failure lands before any git command, and
    # no spawn_step events are published.
    assert not Path(task.clone_path).exists()
    drained = _drain(queue)
    assert not [e for e in drained if e.type == "spawn_step"]
    # No session entry was ever seeded (the run never reached an agent step).
    assert tracker.get(task.id, "main") is None


async def test_preamble_prepended_to_prompt(
    app, git_checkout: Path, fake_my_workshop, pipeline
) -> None:
    engine = app.state.engine
    run, _, _, supervisor = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop('echo "ws-x" > .workshop.lock'),),
    )

    _make_template(engine, git_checkout, preamble="You are on team omega.")
    task = _make_task(engine, config, prompt="fix the bug")

    await run(engine, task.id, config)
    settled = await _wait_workflow_settled(engine, task.id)
    assert settled.state == "created"

    # The fake omp echoes the prompt back in its `message_start` user event,
    # fanned out on the agent handle's ring buffer (not the hub). The run
    # completes at the session's debounced idle — after the whole burst — so
    # the echo is already in the ring buffer when the run settles.
    handle = supervisor.get(task.id, "main")
    assert handle is not None
    assert _user_messages(handle) == ["You are on team omega.\n\nfix the bug"]
    await supervisor.stop(task.id, "main")


async def test_empty_preamble_sends_prompt_unchanged(
    app, git_checkout: Path, fake_my_workshop, pipeline
) -> None:
    engine = app.state.engine
    run, _, _, supervisor = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop('echo "ws-x" > .workshop.lock'),),
    )

    _make_template(engine, git_checkout, preamble="")
    task = _make_task(engine, config, prompt="fix the bug")

    await run(engine, task.id, config)
    await _wait_workflow_settled(engine, task.id)

    handle = supervisor.get(task.id, "main")
    assert handle is not None
    assert _user_messages(handle) == ["fix the bug"]
    await supervisor.stop(task.id, "main")


async def test_preamble_alone_never_prompts(
    app, git_checkout: Path, fake_my_workshop, pipeline
) -> None:
    engine = app.state.engine
    run, hub, tracker, supervisor = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop('echo "ws-x" > .workshop.lock'),),
    )
    queue = hub.subscribe()

    _make_template(engine, git_checkout, preamble="You are on team omega.")
    task = _make_task(engine, config, prompt="")

    await run(engine, task.id, config)
    settled = await _wait_workflow_settled(engine, task.id)

    assert settled.state == "created"
    assert settled.workflow_status == "complete"
    steps = [p["step"] for e in _drain(queue) if e.type == "spawn_step" for p in [e.payload]]
    assert set(steps) == {"fetch", "clone", "branch", "workshop"}
    # The session spawned but nothing was ever sent to it.
    assert tracker.get(task.id, "main").status == "idle"
    assert tracker.get(task.id, "main").reason == "ready, no prompt to send"
    handle = supervisor.get(task.id, "main")
    assert handle is not None
    assert _user_messages(handle) == []
    await supervisor.stop(task.id, "main")


def _argv_capturing_workshop(fake_workshop_cli: Path, argv_file: Path) -> None:
    """Like the conftest fake, but records the rpc-ui spawn argv to a file."""
    from tests.conftest import FAKE_OMP

    fake_workshop_cli.write_text(
        f"""#!/bin/sh
case "$*" in
  *"config get ask.timeout"*) echo 0 ;;
  *"--mode rpc-ui"*)
    printf '%s\\n' "$*" > {argv_file}
    exec {__import__("sys").executable} -u {FAKE_OMP} happy ;;
  *) exit 0 ;;
esac
"""
    )


async def test_template_model_thinking_on_agent_argv(
    app, git_checkout: Path, fake_my_workshop, fake_workshop_cli, tmp_path: Path, pipeline
) -> None:
    engine = app.state.engine
    run, _, _, supervisor = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop('echo "ws-x" > .workshop.lock'),),
    )
    argv_file = tmp_path / "argv.txt"
    _argv_capturing_workshop(fake_workshop_cli, argv_file)

    _make_template(engine, git_checkout, model="fable-5", thinking="medium")
    task = _make_task(engine, config)

    await run(engine, task.id, config)
    await _wait_workflow_settled(engine, task.id)

    argv = argv_file.read_text()
    assert "--model fable-5" in argv
    assert "--thinking medium" in argv
    await supervisor.stop(task.id, "main")


async def test_overrides_beat_template_values(
    app, git_checkout: Path, fake_my_workshop, fake_workshop_cli, tmp_path: Path, pipeline
) -> None:
    engine = app.state.engine
    run, _, _, supervisor = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop('echo "ws-x" > .workshop.lock'),),
    )
    argv_file = tmp_path / "argv.txt"
    _argv_capturing_workshop(fake_workshop_cli, argv_file)

    _make_template(engine, git_checkout, model="fable-5", thinking="medium")
    task = _make_task(engine, config)

    await run(engine, task.id, config, model_override="zephyr-9", thinking_override="high")
    await _wait_workflow_settled(engine, task.id)

    argv = argv_file.read_text()
    assert "--model zephyr-9" in argv
    assert "--thinking high" in argv
    assert "fable-5" not in argv
    assert "medium" not in argv
    await supervisor.stop(task.id, "main")


async def test_template_defaults_omit_flags(
    app, git_checkout: Path, fake_my_workshop, fake_workshop_cli, tmp_path: Path, pipeline
) -> None:
    engine = app.state.engine
    run, _, _, supervisor = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop('echo "ws-x" > .workshop.lock'),),
    )
    argv_file = tmp_path / "argv.txt"
    _argv_capturing_workshop(fake_workshop_cli, argv_file)

    _make_template(engine, git_checkout)  # null model/thinking
    task = _make_task(engine, config)

    await run(engine, task.id, config)
    await _wait_workflow_settled(engine, task.id)

    argv = argv_file.read_text()
    assert "--model" not in argv
    assert "--thinking" not in argv
    await supervisor.stop(task.id, "main")
