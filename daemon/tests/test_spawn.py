"""Spawn pipeline tests, driven against a real fixture git repo, a fake
my-workshop script, and the fake omp behind the fake workshop CLI (no real
containers)."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from ompire_daemon.agent import AgentSupervisor
from ompire_daemon.config import Config
from ompire_daemon.events import Event, EventHub
from ompire_daemon.registry.projects import create_project
from ompire_daemon.registry.tasks import (
    clone_path_for,
    create_task,
    get_task,
)
from ompire_daemon.sessions import SessionTracker
from ompire_daemon.spawn import run_spawn_pipeline

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
    runner that drives the pipeline with them."""
    config: Config = app.state.config
    hub = EventHub()
    tracker = SessionTracker(hub, idle_debounce=0.1)
    supervisor = AgentSupervisor(config, hub, tracker)

    async def run(engine, task_id: int, project, run_config: Config | None = None):  # noqa: ANN001
        await run_spawn_pipeline(
            engine, hub, run_config or config, task_id, project, supervisor, tracker
        )

    return run, hub, tracker, supervisor


def _make_project(engine, checkout: Path, *, base_branch: str = "main"):  # noqa: ANN001, ANN202
    return create_project(
        engine,
        name="demo",
        title="Demo",
        upstream_url="https://example.com/demo.git",
        checkout_path=str(checkout),
        base_branch=base_branch,
        default_branch_pattern="ompire/<slug>",
        default_checkout_root=checkout.parent,
    )


def _make_task(engine, config: Config, *, prompt: str = "fix the bug"):  # noqa: ANN001, ANN202
    clone_path = clone_path_for(config.task_dir_root, "demo", "fix-bug")
    return create_task(
        engine,
        project_name="demo",
        slug="fix-bug",
        branch="ompire/fix-bug",
        clone_path=str(clone_path),
        prompt=prompt,
    )


def _drain(queue) -> list[Event]:  # noqa: ANN001
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


async def test_successful_pipeline(app, git_checkout: Path, fake_my_workshop, pipeline) -> None:  # noqa: ANN001
    engine = app.state.engine
    run, hub, tracker, supervisor = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop('echo "ws-demo-fix-bug" > .workshop.lock'),),
    )
    queue = hub.subscribe()

    project = _make_project(engine, git_checkout)
    task = _make_task(engine, config)

    await run(engine, task.id, project, config)

    clone = Path(task.clone_path)
    assert (clone / ".git").is_dir()
    head = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert head == "ompire/fix-bug"

    refreshed = get_task(engine, task.id)
    assert refreshed.state == "created"
    assert refreshed.spawn_completed_at is not None
    assert refreshed.workshop_id == "ws-demo-fix-bug"
    # Session id captured after ready (crash-recovery capability, design D-2).
    assert refreshed.session_id == "fake-session-id"

    # The agent is live with the stored prompt delivered.
    assert supervisor.get(task.id) is not None
    assert tracker.get(task.id) is not None

    spawn_steps = [e.payload for e in _drain(queue) if e.type == "spawn_step"]
    assert [(p["step"], p["status"]) for p in spawn_steps] == [
        ("fetch", "started"),
        ("fetch", "ok"),
        ("clone", "started"),
        ("clone", "ok"),
        ("branch", "started"),
        ("branch", "ok"),
        ("workshop", "started"),
        ("workshop", "ok"),
        ("agent", "started"),
        ("agent", "ok"),
        ("prompt", "started"),
        ("prompt", "ok"),
    ]
    await supervisor.stop(task.id)


async def test_empty_prompt_skips_prompt_step(
    app, git_checkout: Path, fake_my_workshop, pipeline  # noqa: ANN001
) -> None:
    engine = app.state.engine
    run, hub, tracker, supervisor = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop('echo "ws-x" > .workshop.lock'),),
    )
    queue = hub.subscribe()

    project = _make_project(engine, git_checkout)
    task = _make_task(engine, config, prompt="")

    await run(engine, task.id, project, config)

    refreshed = get_task(engine, task.id)
    assert refreshed.state == "created"
    assert refreshed.spawn_completed_at is not None

    steps = [p["step"] for e in _drain(queue) if e.type == "spawn_step" for p in [e.payload]]
    assert "prompt" not in steps
    assert "agent" in steps
    # Promptless task idles after ready instead of hanging in starting.
    assert tracker.get(task.id).status == "idle"
    assert tracker.get(task.id).reason == "ready, no prompt to send"
    await supervisor.stop(task.id)


async def test_agent_step_failure_marks_task_failed(
    app, git_checkout: Path, fake_my_workshop, fake_workshop_cli, pipeline  # noqa: ANN001
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

    project = _make_project(engine, git_checkout)
    task = _make_task(engine, config)

    await run(engine, task.id, project, config)

    refreshed = get_task(engine, task.id)
    assert refreshed.state == "failed"
    assert "agent" in (refreshed.error or "")
    assert "No models available" in (refreshed.error or "")

    drained = _drain(queue)
    failed_steps = [
        e.payload for e in drained if e.type == "spawn_step" and e.payload["status"] == "failed"
    ]
    assert failed_steps and failed_steps[0]["step"] == "agent"
    assert "No models available" in failed_steps[0]["stderr"]
    # The session lands failed too (design D-2).
    assert tracker.get(task.id).status == "failed"
    assert tracker.get(task.id).reason == "spawn step 'agent' failed"


async def test_session_id_capture_failure_leaves_it_null_and_spawn_succeeds(
    app, git_checkout: Path, fake_my_workshop, fake_workshop_cli, pipeline  # noqa: ANN001
) -> None:
    engine = app.state.engine
    run, hub, tracker, supervisor = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop('echo "ws-x" > .workshop.lock'),),
    )
    # The agent starts fine, but `get_state` (used to capture the session id)
    # answers success: false — capture must degrade gracefully, not fail the
    # spawn (design D-2).
    fake_workshop_cli.write_text(FAKE_WORKSHOP_SCRIPT.replace(" happy", " get-state-fails"))
    queue = hub.subscribe()

    project = _make_project(engine, git_checkout)
    task = _make_task(engine, config)

    await run(engine, task.id, project, config)

    refreshed = get_task(engine, task.id)
    assert refreshed.state == "created"
    assert refreshed.spawn_completed_at is not None
    assert refreshed.session_id is None

    spawn_steps = [
        (e.payload["step"], e.payload["status"])
        for e in _drain(queue)
        if e.type == "spawn_step"
    ]
    assert ("agent", "ok") in spawn_steps
    assert ("prompt", "ok") in spawn_steps
    await supervisor.stop(task.id)


async def test_prompt_step_failure_marks_task_failed(
    app, git_checkout: Path, fake_my_workshop, pipeline  # noqa: ANN001
) -> None:
    engine = app.state.engine
    run, hub, tracker, supervisor = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop('echo "ws-x" > .workshop.lock'),),
    )
    project = _make_project(engine, git_checkout)
    # The magic "fail" prompt makes fake omp answer success: false, "boom".
    task = _make_task(engine, config, prompt="fail")

    await run(engine, task.id, project, config)

    refreshed = get_task(engine, task.id)
    assert refreshed.state == "failed"
    assert "prompt" in (refreshed.error or "")
    assert "boom" in (refreshed.error or "")
    assert tracker.get(task.id).status == "failed"
    await supervisor.stop(task.id)


async def test_step_failure_captures_stderr(app, git_checkout: Path, pipeline) -> None:  # noqa: ANN001
    engine = app.state.engine
    run, hub, _, _ = pipeline
    queue = hub.subscribe()

    project = _make_project(engine, git_checkout, base_branch="no-such-branch")
    task = _make_task(engine, app.state.config)

    await run(engine, task.id, project)

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


async def test_leftover_directory_fails(app, git_checkout: Path, pipeline) -> None:  # noqa: ANN001
    engine = app.state.engine
    run, hub, _, _ = pipeline
    queue = hub.subscribe()

    project = _make_project(engine, git_checkout)
    task = _make_task(engine, app.state.config)
    Path(task.clone_path).mkdir(parents=True)

    await run(engine, task.id, project)

    refreshed = get_task(engine, task.id)
    assert refreshed.state == "failed"
    assert "already exists" in (refreshed.error or "")
    failed_steps = [
        e.payload for e in _drain(queue) if e.type == "spawn_step" and e.payload["status"] == "failed"
    ]
    assert failed_steps and failed_steps[0]["step"] == "clone"


async def test_workshop_step_failure(app, git_checkout: Path, fake_my_workshop, pipeline) -> None:  # noqa: ANN001
    engine = app.state.engine
    run, hub, _, _ = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop('echo "launch exploded" >&2; exit 3'),),
    )
    queue = hub.subscribe()

    project = _make_project(engine, git_checkout)
    task = _make_task(engine, config)

    await run(engine, task.id, project, config)

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


async def test_workshop_step_timeout(app, git_checkout: Path, fake_my_workshop, pipeline) -> None:  # noqa: ANN001
    engine = app.state.engine
    run, _, _, _ = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop("sleep 5"),),
        workshop_step_timeout=1,
    )

    project = _make_project(engine, git_checkout)
    task = _make_task(engine, config)

    await run(engine, task.id, project, config)

    refreshed = get_task(engine, task.id)
    assert refreshed.state == "failed"
    assert "timed out after 1s" in (refreshed.error or "")


async def test_workshop_lock_missing_after_success(
    app, git_checkout: Path, fake_my_workshop, pipeline  # noqa: ANN001
) -> None:
    engine = app.state.engine
    run, hub, _, _ = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop("exit 0"),),
    )
    queue = hub.subscribe()

    project = _make_project(engine, git_checkout)
    task = _make_task(engine, config)

    await run(engine, task.id, project, config)

    refreshed = get_task(engine, task.id)
    assert refreshed.state == "failed"
    assert refreshed.workshop_id is None
    assert ".workshop.lock" in (refreshed.error or "")

    failed_steps = [
        e.payload for e in _drain(queue) if e.type == "spawn_step" and e.payload["status"] == "failed"
    ]
    assert failed_steps and failed_steps[0]["step"] == "workshop"


async def test_workshop_tool_missing(app, git_checkout: Path, pipeline) -> None:  # noqa: ANN001
    engine = app.state.engine
    run, _, _, _ = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=("/no/such/my-workshop",),
    )

    project = _make_project(engine, git_checkout)
    task = _make_task(engine, config)

    await run(engine, task.id, project, config)

    refreshed = get_task(engine, task.id)
    assert refreshed.state == "failed"
    assert "cannot exec" in (refreshed.error or "")
