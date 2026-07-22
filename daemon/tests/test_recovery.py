"""Crash-recovery capability tests: `classify_startup_tasks` (the startup
reconciliation matrix's container-probe half) and `run_recovery` (the
background resume job), plus a full-stack shutdown -> restart -> resume
integration test through the REST/app lifecycle."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ompire_daemon import agent as agent_module
from ompire_daemon.agent import AgentSupervisor
from ompire_daemon.app import create_app
from ompire_daemon.config import Config
from ompire_daemon.events import EventHub
from ompire_daemon.recovery import classify_startup_tasks, run_recovery
from ompire_daemon.registry.projects import create_project
from ompire_daemon.registry.tasks import (
    create_task,
    get_task,
    mark_session_id,
    mark_spawn_completed,
)
from ompire_daemon.sessions import SessionTracker

from tests.test_rpc import fake_omp_argv


@pytest.fixture
def engine_project(app, git_checkout: Path):  # noqa: ANN001, ANN201
    engine = app.state.engine
    project = create_project(
        engine,
        name="demo",
        title="Demo",
        upstream_url="https://example.com/demo.git",
        checkout_path=str(git_checkout),
        base_branch="main",
        default_branch_pattern="ompire/<slug>",
        default_checkout_root=git_checkout.parent,
    )
    return engine, project


def _make_task(engine, project, tmp_path: Path, slug: str):  # noqa: ANN001, ANN202
    clone_path = tmp_path / "tasks" / slug
    return create_task(
        engine,
        project_name=project.name,
        slug=slug,
        branch=f"ompire/{slug}",
        clone_path=str(clone_path),
        prompt="fix it",
    )


async def test_classify_missing_container_fails(engine_project, tmp_path: Path) -> None:
    engine, project = engine_project
    task = _make_task(engine, project, tmp_path, "no-clone")
    mark_spawn_completed(engine, task.id)
    mark_session_id(engine, task.id, "sess-1")
    # No clone directory created: workshop_status short-circuits to "absent".

    hub = EventHub()
    tracker = SessionTracker(hub, idle_debounce=0.1)
    recoverable = await classify_startup_tasks(engine, hub, tracker)

    assert recoverable == []
    refreshed = get_task(engine, task.id)
    assert refreshed.state == "failed"
    assert "container" in (refreshed.error or "")
    assert tracker.get(task.id) is None


async def test_classify_recoverable_candidate_seeds_starting(
    engine_project, tmp_path: Path
) -> None:
    engine, project = engine_project
    task = _make_task(engine, project, tmp_path, "recoverable")
    mark_spawn_completed(engine, task.id)
    mark_session_id(engine, task.id, "sess-1")
    Path(task.clone_path).mkdir(parents=True)

    hub = EventHub()
    tracker = SessionTracker(hub, idle_debounce=0.1)
    recoverable = await classify_startup_tasks(engine, hub, tracker)

    assert [t.id for t in recoverable] == [task.id]
    assert recoverable[0].session_id == "sess-1"
    assert get_task(engine, task.id).state == "created"
    assert tracker.get(task.id).status == "starting"
    assert tracker.get(task.id).reason == "recovering after daemon restart"


async def test_run_recovery_resumes_with_resume_argv_and_no_reprompt(
    engine_project, tmp_path: Path, monkeypatch
) -> None:
    engine, project = engine_project
    task = _make_task(engine, project, tmp_path, "resume-me")
    mark_spawn_completed(engine, task.id)
    task = mark_session_id(engine, task.id, "sess-1")

    captured_resume = {}

    def fake_build(clone, env, resume=None):  # noqa: ANN001
        captured_resume["value"] = resume
        return fake_omp_argv("happy")

    monkeypatch.setattr(agent_module, "build_agent_argv", fake_build)

    async def no_preflight(clone_path: str) -> None:
        return None

    monkeypatch.setattr(agent_module, "verify_ask_timeout", no_preflight)

    hub = EventHub()
    tracker = SessionTracker(hub, idle_debounce=0.1)
    config = Config(agent_ready_timeout=5, agent_ring_buffer_size=100)
    supervisor = AgentSupervisor(config, hub, tracker)
    tracker.recovering(task.id)

    await run_recovery(engine, hub, config, supervisor, tracker, [task])

    assert captured_resume["value"] == "sess-1"
    assert tracker.get(task.id).status == "idle"
    assert tracker.get(task.id).reason == "resumed after daemon restart"
    assert supervisor.get(task.id) is not None
    await supervisor.stop(task.id)


async def test_run_recovery_failure_marks_task_and_session_failed(
    engine_project, tmp_path: Path, monkeypatch
) -> None:
    engine, project = engine_project
    task = _make_task(engine, project, tmp_path, "crash-on-resume")
    mark_spawn_completed(engine, task.id)
    task = mark_session_id(engine, task.id, "sess-1")

    monkeypatch.setattr(
        agent_module,
        "build_agent_argv",
        lambda clone, env, resume=None: fake_omp_argv("crash"),
    )

    async def no_preflight(clone_path: str) -> None:
        return None

    monkeypatch.setattr(agent_module, "verify_ask_timeout", no_preflight)

    hub = EventHub()
    events_queue = hub.subscribe()
    tracker = SessionTracker(hub, idle_debounce=0.1)
    config = Config(agent_ready_timeout=5, agent_ring_buffer_size=100)
    supervisor = AgentSupervisor(config, hub, tracker)
    tracker.recovering(task.id)

    await run_recovery(engine, hub, config, supervisor, tracker, [task])

    assert tracker.get(task.id).status == "failed"
    assert "resume failed" in tracker.get(task.id).reason
    refreshed = get_task(engine, task.id)
    assert refreshed.state == "failed"
    assert "resume failed" in (refreshed.error or "")
    task_updates = [
        e.payload for e in _drain(events_queue) if e.type == "task_updated"
    ]
    assert task_updates and task_updates[-1]["state"] == "failed"


def _drain(queue) -> list:  # noqa: ANN001
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


def _wait_settled(client: TestClient, auth_headers: dict, task_id: int, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = client.get(f"/api/tasks/{task_id}", headers=auth_headers).json()
        if task["spawn_completed_at"] is not None or task["state"] == "failed":
            return task
        time.sleep(0.05)
    raise AssertionError("spawn pipeline did not settle in time")


def test_shutdown_then_restart_resumes_without_reprompt(
    tmp_path: Path, git_checkout: Path
) -> None:
    """Full-stack: spawn a live task, shut the daemon down gracefully (the
    TestClient context exit runs the lifespan `finally`), then start a fresh
    `create_app` against the same data dir and confirm the task resumes to
    `idle` without being re-prompted and without ever showing as a crash."""
    fake_my_workshop = tmp_path / "fake-my-workshop"
    fake_my_workshop.write_text('#!/bin/sh\necho "ws-test" > .workshop.lock\n')
    fake_my_workshop.chmod(0o755)
    config = Config(
        data_dir=tmp_path / "data",
        task_dir_root=tmp_path / "tasks",
        checkout_root=tmp_path / "proj",
        my_workshop_command=(str(fake_my_workshop),),
    )
    app = create_app(config, frontend_dist=tmp_path / "no-dist")
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {app.state.auth_token}"}
        client.post(
            "/api/projects",
            headers=headers,
            json={
                "name": "demo",
                "title": "Demo",
                "upstream_url": "https://example.com/demo.git",
                "checkout_path": str(git_checkout),
            },
        )
        response = client.post(
            "/api/tasks",
            headers=headers,
            json={"project_name": "demo", "slug": "fix-bug", "prompt": "fix it"},
        )
        assert response.status_code == 202
        task_id = response.json()["id"]
        settled = _wait_settled(client, headers, task_id)
        assert settled["state"] == "created"
        assert settled["session_id"] is not None
    # TestClient.__exit__ ran the lifespan shutdown: agents.shutdown() sent
    # SIGTERM to the live child, and the exit watcher's shutting-down flag
    # kept it from being reported as a crash.

    task_after_shutdown = get_task(app.state.engine, task_id)
    assert task_after_shutdown.state == "created"
    assert task_after_shutdown.session_id is not None
    app.state.engine.dispose()

    restarted = create_app(config, frontend_dist=tmp_path / "no-dist")
    with TestClient(restarted) as client:
        headers = {"Authorization": f"Bearer {restarted.state.auth_token}"}
        with client.websocket_connect(f"/api/ws?token={restarted.state.auth_token}") as ws:
            snapshot = ws.receive_json()
            assert snapshot["type"] == "snapshot"
            sessions = snapshot["payload"]["sessions"]
            # Recovery already seeded `starting` synchronously before the
            # first snapshot (design D-4/6.2) — never absent, never a crash.
            assert sessions[str(task_id)]["status"] == "starting"
            assert sessions[str(task_id)]["reason"] == "recovering after daemon restart"

            status = sessions[str(task_id)]["status"]
            deadline = time.monotonic() + 10.0
            while status == "starting" and time.monotonic() < deadline:
                event = ws.receive_json()
                if event["type"] == "status_changed" and event["payload"]["task_id"] == task_id:
                    status = event["payload"]["to"]
                assert event["type"] != "agent_exited"

        assert status == "idle"
        task_final = get_task(restarted.state.engine, task_id)
        assert task_final.state == "created"
