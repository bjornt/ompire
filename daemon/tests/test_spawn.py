"""Spawn pipeline tests, driven against a real fixture git repo and a fake
my-workshop script (no real containers)."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from ompire_daemon.config import Config
from ompire_daemon.events import Event, EventHub
from ompire_daemon.registry.projects import create_project
from ompire_daemon.registry.tasks import (
    clone_path_for,
    create_task,
    get_task,
)
from ompire_daemon.spawn import run_spawn_pipeline


@pytest.fixture
def fake_my_workshop(tmp_path: Path):
    """Factory for fake my-workshop scripts; returns the script path."""

    def make(body: str) -> str:
        script = tmp_path / "fake-my-workshop"
        script.write_text(f"#!/bin/sh\n{body}\n")
        script.chmod(0o755)
        return str(script)

    return make


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


def _make_task(engine, config: Config):  # noqa: ANN001, ANN202
    clone_path = clone_path_for(config.task_dir_root, "demo", "fix-bug")
    return create_task(
        engine,
        project_name="demo",
        slug="fix-bug",
        branch="ompire/fix-bug",
        clone_path=str(clone_path),
        prompt="fix the bug",
    )


def _drain(queue) -> list[Event]:  # noqa: ANN001
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


async def test_successful_pipeline(app, git_checkout: Path, fake_my_workshop) -> None:  # noqa: ANN001
    engine = app.state.engine
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop('echo "ws-demo-fix-bug" > .workshop.lock'),),
    )
    events = EventHub()
    queue = events.subscribe()

    project = _make_project(engine, git_checkout)
    task = _make_task(engine, config)

    await run_spawn_pipeline(engine, events, config, task.id, project)

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
    ]


async def test_step_failure_captures_stderr(app, git_checkout: Path) -> None:  # noqa: ANN001
    engine = app.state.engine
    config: Config = app.state.config
    events = EventHub()
    queue = events.subscribe()

    project = _make_project(engine, git_checkout, base_branch="no-such-branch")
    task = _make_task(engine, config)

    await run_spawn_pipeline(engine, events, config, task.id, project)

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


async def test_leftover_directory_fails(app, git_checkout: Path) -> None:  # noqa: ANN001
    engine = app.state.engine
    config: Config = app.state.config
    events = EventHub()
    queue = events.subscribe()

    project = _make_project(engine, git_checkout)
    task = _make_task(engine, config)
    Path(task.clone_path).mkdir(parents=True)

    await run_spawn_pipeline(engine, events, config, task.id, project)

    refreshed = get_task(engine, task.id)
    assert refreshed.state == "failed"
    assert "already exists" in (refreshed.error or "")
    failed_steps = [
        e.payload for e in _drain(queue) if e.type == "spawn_step" and e.payload["status"] == "failed"
    ]
    assert failed_steps and failed_steps[0]["step"] == "clone"


async def test_workshop_step_failure(app, git_checkout: Path, fake_my_workshop) -> None:  # noqa: ANN001
    engine = app.state.engine
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop('echo "launch exploded" >&2; exit 3'),),
    )
    events = EventHub()
    queue = events.subscribe()

    project = _make_project(engine, git_checkout)
    task = _make_task(engine, config)

    await run_spawn_pipeline(engine, events, config, task.id, project)

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


async def test_workshop_step_timeout(app, git_checkout: Path, fake_my_workshop) -> None:  # noqa: ANN001
    engine = app.state.engine
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop("sleep 5"),),
        workshop_step_timeout=1,
    )
    events = EventHub()

    project = _make_project(engine, git_checkout)
    task = _make_task(engine, config)

    await run_spawn_pipeline(engine, events, config, task.id, project)

    refreshed = get_task(engine, task.id)
    assert refreshed.state == "failed"
    assert "timed out after 1s" in (refreshed.error or "")


async def test_workshop_lock_missing_after_success(app, git_checkout: Path, fake_my_workshop) -> None:  # noqa: ANN001
    engine = app.state.engine
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop("exit 0"),),
    )
    events = EventHub()
    queue = events.subscribe()

    project = _make_project(engine, git_checkout)
    task = _make_task(engine, config)

    await run_spawn_pipeline(engine, events, config, task.id, project)

    refreshed = get_task(engine, task.id)
    assert refreshed.state == "failed"
    assert refreshed.workshop_id is None
    assert ".workshop.lock" in (refreshed.error or "")

    failed_steps = [
        e.payload for e in _drain(queue) if e.type == "spawn_step" and e.payload["status"] == "failed"
    ]
    assert failed_steps and failed_steps[0]["step"] == "workshop"


async def test_workshop_tool_missing(app, git_checkout: Path) -> None:  # noqa: ANN001
    engine = app.state.engine
    config: Config = replace(
        app.state.config,
        my_workshop_command=("/no/such/my-workshop",),
    )
    events = EventHub()

    project = _make_project(engine, git_checkout)
    task = _make_task(engine, config)

    await run_spawn_pipeline(engine, events, config, task.id, project)

    refreshed = get_task(engine, task.id)
    assert refreshed.state == "failed"
    assert "cannot exec" in (refreshed.error or "")
