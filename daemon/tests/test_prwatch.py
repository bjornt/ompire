"""PR watcher tests (merge-poll capability): `gh pr view --json` polling,
durable pr_state transitions, terminal-state and failure behavior.

`gh` is faked with stub scripts via `gh_command` (the test_ship.py pattern);
no test ever touches the real CLI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import Engine

from ompire_daemon.config import Config
from ompire_daemon.db import make_engine
from ompire_daemon.events import EventHub
from ompire_daemon.migrate import upgrade_head
from ompire_daemon.prwatch import PrWatcher, _parse_pr_view
from ompire_daemon.registry.projects import create_project
from ompire_daemon.registry.tasks import (
    Task,
    create_task,
    get_task,
    mark_archived,
    mark_pr_state,
    mark_pr_url,
    mark_spawn_completed,
)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    db_path = tmp_path / "ompire.db"
    upgrade_head(db_path)
    return make_engine(db_path)


@pytest.fixture
def task(engine: Engine, tmp_path: Path) -> Task:
    create_project(
        engine,
        name="demo",
        title="Demo",
        upstream_url="https://github.com/upowner/uprepo.git",
        checkout_path=str(tmp_path / "checkout"),
        default_checkout_root=tmp_path / "proj",
    )
    created = create_task(
        engine,
        project_name="demo",
        slug="fix-thing",
        branch="ompire/fix-thing",
        clone_path=str(tmp_path / "tasks" / "demo" / "fix-thing"),
        prompt="fix it",
    )
    return mark_pr_url(engine, created.id, "https://github.com/upowner/uprepo/pull/7")


def _fake_gh(tmp_path: Path, payload: dict | None, exit_code: int = 0) -> tuple[str, ...]:
    """A stub `gh` printing `payload` as JSON; `payload=None` emits garbage."""
    script = tmp_path / "fake-gh"
    body = json.dumps(payload) if payload is not None else "not json"
    script.write_text(f"#!/bin/sh\necho '{body}'\nexit {exit_code}\n")
    script.chmod(0o755)
    return (str(script),)


def _watcher(tmp_path: Path, engine: Engine, hub: EventHub, gh: tuple[str, ...]) -> PrWatcher:
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)  # the watcher runs gh with data_dir as cwd
    config = Config(data_dir=data_dir, gh_command=gh, pr_poll_interval=60)
    return PrWatcher(config, engine, hub)


async def test_open_to_merged_transition_persists_and_broadcasts(
    engine: Engine, task: Task, tmp_path: Path
) -> None:
    hub = EventHub()
    queue = hub.subscribe()
    gh = _fake_gh(tmp_path, {"state": "MERGED", "mergedAt": "2026-08-14T09:30:00Z"})

    await _watcher(tmp_path, engine, hub, gh).poll_once()

    updated = get_task(engine, task.id)
    assert updated.pr_state == "merged"
    assert updated.pr_merged_at == "2026-08-14T09:30:00Z"
    event = queue.get_nowait()
    assert event.type == "task_updated"
    assert event.payload["id"] == task.id
    assert event.payload["pr_state"] == "merged"


async def test_open_state_recorded_on_first_poll(engine: Engine, task: Task, tmp_path: Path) -> None:
    hub = EventHub()
    gh = _fake_gh(tmp_path, {"state": "OPEN", "mergedAt": None})

    await _watcher(tmp_path, engine, hub, gh).poll_once()

    updated = get_task(engine, task.id)
    assert updated.pr_state == "open"
    assert updated.pr_merged_at is None


async def test_unchanged_state_writes_nothing(engine: Engine, task: Task, tmp_path: Path) -> None:
    mark_pr_state(engine, task.id, "open")
    hub = EventHub()
    queue = hub.subscribe()
    gh = _fake_gh(tmp_path, {"state": "OPEN", "mergedAt": None})

    await _watcher(tmp_path, engine, hub, gh).poll_once()

    assert queue.empty()
    assert get_task(engine, task.id).pr_state == "open"


async def test_terminal_states_are_not_polled(engine: Engine, task: Task, tmp_path: Path) -> None:
    mark_pr_state(engine, task.id, "merged", "2026-08-13T00:00:00Z")
    hub = EventHub()
    queue = hub.subscribe()
    # A gh that records every invocation: none may happen for a terminal PR.
    counter = tmp_path / "gh-calls"
    script = tmp_path / "fake-gh"
    script.write_text(f'#!/bin/sh\necho x >> "{counter}"\necho \'{{"state":"OPEN"}}\'\n')
    script.chmod(0o755)

    await _watcher(tmp_path, engine, hub, (str(script),)).poll_once()

    assert not counter.exists()
    assert queue.empty()


async def test_archived_tasks_are_not_polled(engine: Engine, task: Task, tmp_path: Path) -> None:
    mark_archived(engine, task.id)
    hub = EventHub()
    counter = tmp_path / "gh-calls"
    script = tmp_path / "fake-gh"
    script.write_text(f'#!/bin/sh\necho x >> "{counter}"\necho \'{{"state":"OPEN"}}\'\n')
    script.chmod(0o755)

    await _watcher(tmp_path, engine, hub, (str(script),)).poll_once()

    assert not counter.exists()


async def test_failed_poll_changes_nothing_and_survives(
    engine: Engine, task: Task, tmp_path: Path
) -> None:
    hub = EventHub()
    queue = hub.subscribe()
    # The observed unauthenticated-gh failure mode (findings 1.1): exit 4.
    gh = _fake_gh(tmp_path, {"state": "MERGED", "mergedAt": "2026-08-14T09:30:00Z"}, exit_code=4)

    watcher = _watcher(tmp_path, engine, hub, gh)
    await watcher.poll_once()  # must not raise
    await watcher.poll_once()

    assert get_task(engine, task.id).pr_state is None
    assert queue.empty()


async def test_unparseable_output_changes_nothing(engine: Engine, task: Task, tmp_path: Path) -> None:
    hub = EventHub()
    gh = _fake_gh(tmp_path, None)

    await _watcher(tmp_path, engine, hub, gh).poll_once()

    assert get_task(engine, task.id).pr_state is None


def test_parse_pr_view_maps_states() -> None:
    assert _parse_pr_view('{"state":"OPEN","mergedAt":null}') == ("open", None)
    assert _parse_pr_view('{"state":"MERGED","mergedAt":"2026-08-14T09:30:00Z"}') == (
        "merged",
        "2026-08-14T09:30:00Z",
    )
    assert _parse_pr_view('{"state":"CLOSED","mergedAt":null}') == ("closed", None)
    assert _parse_pr_view("not json") == (None, None)
    assert _parse_pr_view('{"state":"WEIRD"}') == (None, None)
    assert _parse_pr_view('["not-a-dict"]') == (None, None)
    # A merged PR without a usable timestamp still records the state.
    assert _parse_pr_view('{"state":"MERGED","mergedAt":null}') == ("merged", None)


async def test_spawn_completed_unshipped_tasks_are_not_polled(
    engine: Engine, task: Task, tmp_path: Path
) -> None:
    # A task mid-flight (no pr_url yet) must never hit GitHub.
    unshipped = create_task(
        engine,
        project_name="demo",
        slug="other",
        branch="ompire/other",
        clone_path=str(tmp_path / "tasks" / "demo" / "other"),
        prompt="x",
    )
    mark_spawn_completed(engine, unshipped.id)
    # And the shipped task from the fixture is archived: nothing pollable.
    mark_archived(engine, task.id)
    hub = EventHub()
    counter = tmp_path / "gh-calls"
    script = tmp_path / "fake-gh"
    script.write_text(f'#!/bin/sh\necho x >> "{counter}"\necho \'{{"state":"OPEN"}}\'\n')
    script.chmod(0o755)

    await _watcher(tmp_path, engine, hub, (str(script),)).poll_once()

    assert not counter.exists()


def test_watcher_runs_under_the_app_lifecycle(tmp_path: Path) -> None:
    """End-to-end through a booted app: the lifespan-started watcher polls the
    stub gh and the merged state lands on REST task payloads (which the WS
    snapshot serializes identically)."""
    import time

    from fastapi.testclient import TestClient

    from ompire_daemon.app import create_app

    gh = _fake_gh(tmp_path, {"state": "MERGED", "mergedAt": "2026-08-14T09:30:00Z"})
    fake_my_workshop = tmp_path / "fake-my-workshop"
    fake_my_workshop.write_text('#!/bin/sh\necho "ws-test" > .workshop.lock\n')
    fake_my_workshop.chmod(0o755)
    config = Config(
        data_dir=tmp_path / "data",
        task_dir_root=tmp_path / "tasks",
        checkout_root=tmp_path / "proj",
        my_workshop_command=(str(fake_my_workshop),),
        gh_command=gh,
        pr_poll_interval=60,
    )
    app = create_app(config, frontend_dist=tmp_path / "no-dist")
    # Seed a shipped task directly on the app's registry (the spawn pipeline
    # is spawn-chunk behavior, not under test here).
    create_project(
        app.state.engine,
        name="demo",
        title="Demo",
        upstream_url="https://github.com/upowner/uprepo.git",
        checkout_path=str(tmp_path / "checkout"),
        default_checkout_root=tmp_path / "proj",
    )
    seeded = create_task(
        app.state.engine,
        project_name="demo",
        slug="fix-thing",
        branch="ompire/fix-thing",
        clone_path=str(tmp_path / "tasks" / "demo" / "fix-thing"),
        prompt="fix it",
    )
    mark_pr_url(app.state.engine, seeded.id, "https://github.com/upowner/uprepo/pull/7")

    headers = {"Authorization": f"Bearer {app.state.auth_token}"}
    with TestClient(app) as client:  # entering the lifespan starts the watcher
        deadline = time.monotonic() + 10
        body: list[dict] = []
        while time.monotonic() < deadline:
            body = client.get("/api/tasks", headers=headers).json()
            if body[0]["pr_state"] == "merged":
                assert body[0]["pr_merged_at"] == "2026-08-14T09:30:00Z"
                return
            time.sleep(0.1)
    raise AssertionError(f"watcher did not record the merge; tasks: {body}")
