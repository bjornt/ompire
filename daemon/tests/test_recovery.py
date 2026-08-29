"""Crash-recovery capability tests: `classify_startup_tasks` (the startup
reconciliation matrix's container-probe half) and `run_recovery` (the
background resume job), plus a full-stack shutdown -> restart -> resume
integration test through the REST/app lifecycle.

Recovery is per-session now (workflow-engine design D-6): recorded sessions
are seeded via `registry.sessions` rows, `classify_startup_tasks` paints each
resumable session `starting`, and `run_recovery` resumes them through the
`WorkflowRunner`-owning supervisor. A spawn-completed task with no recorded
session is a recovery candidate, not a failure — sessions are lazily spawned,
so "no session id" says nothing about recoverability."""

from __future__ import annotations

import subprocess
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
from ompire_daemon.registry.sessions import (
    get_session,
    mark_session_id,
    record_session_spawned,
)
from ompire_daemon.registry.tasks import (
    create_task,
    get_task,
    mark_spawn_completed,
)
from ompire_daemon.registry.workflows import (
    append_step_record,
    finish_step_record,
    list_step_records,
    set_run_status,
)
from ompire_daemon.review import REVIEW_GIT_REF
from ompire_daemon.sessions import SessionTracker
from ompire_daemon.workflows import WorkflowRunner
from tests.test_rpc import fake_omp_argv


@pytest.fixture
def engine_project(app, git_checkout: Path):
    engine = app.state.engine
    project = create_project(
        engine,
        name="demo",
        title="Demo",
        upstream_url="https://example.com/demo.git",
        checkout_path=str(git_checkout),
        default_checkout_root=git_checkout.parent,
    )
    return engine, project


def _make_task(engine, project, tmp_path: Path, slug: str):
    clone_path = tmp_path / "tasks" / slug
    return create_task(
        engine,
        project_name=project.name,
        slug=slug,
        branch=f"ompire/{slug}",
        clone_path=str(clone_path),
        prompt="fix it",
    )


def _record_main_session(engine, task_id: int, omp_session_id: str = "sess-1") -> None:
    """Seed the session rows a successful spawn would have written (lazy
    spawn by the workflow engine records the row, then the omp identity)."""
    record_session_spawned(engine, task_id, "main")
    mark_session_id(engine, task_id, "main", omp_session_id)


async def test_classify_missing_container_fails(engine_project, tmp_path: Path) -> None:
    engine, project = engine_project
    task = _make_task(engine, project, tmp_path, "no-clone")
    mark_spawn_completed(engine, task.id)
    _record_main_session(engine, task.id)
    # No clone directory created: workshop_status short-circuits to "absent".

    hub = EventHub()
    tracker = SessionTracker(hub, idle_debounce=0.1)
    recoverable = await classify_startup_tasks(engine, hub, tracker)

    assert recoverable == []
    refreshed = get_task(engine, task.id)
    assert refreshed.state == "failed"
    assert "container" in (refreshed.error or "")
    assert tracker.get(task.id, "main") is None


async def test_classify_recoverable_candidate_seeds_starting(
    engine_project, tmp_path: Path
) -> None:
    engine, project = engine_project
    task = _make_task(engine, project, tmp_path, "recoverable")
    mark_spawn_completed(engine, task.id)
    _record_main_session(engine, task.id)
    Path(task.clone_path).mkdir(parents=True)

    hub = EventHub()
    tracker = SessionTracker(hub, idle_debounce=0.1)
    recoverable = await classify_startup_tasks(engine, hub, tracker)

    assert [t.id for t in recoverable] == [task.id]
    assert get_session(engine, task.id, "main").omp_session_id == "sess-1"
    assert get_task(engine, task.id).state == "created"
    assert tracker.get(task.id, "main").status == "starting"
    assert tracker.get(task.id, "main").reason == "recovering after daemon restart"


async def test_classify_spawn_completed_without_session_is_recoverable(
    engine_project, tmp_path: Path
) -> None:
    """A spawn-completed task with NO recorded session is a candidate, not a
    failure (workflow-engine design D-6): sessions are lazily spawned, so a
    task may legitimately have none (a command-only workflow, or a run that
    failed before its first agent step)."""
    engine, project = engine_project
    task = _make_task(engine, project, tmp_path, "no-session")
    mark_spawn_completed(engine, task.id)
    Path(task.clone_path).mkdir(parents=True)

    hub = EventHub()
    tracker = SessionTracker(hub, idle_debounce=0.1)
    recoverable = await classify_startup_tasks(engine, hub, tracker)

    assert [t.id for t in recoverable] == [task.id]
    assert get_task(engine, task.id).state == "created"
    # No recorded session: nothing is seeded into the tracker.
    assert tracker.get(task.id, "main") is None


async def test_run_recovery_resumes_with_resume_argv_and_no_reprompt(
    engine_project, tmp_path: Path, monkeypatch
) -> None:
    engine, project = engine_project
    task = _make_task(engine, project, tmp_path, "resume-me")
    mark_spawn_completed(engine, task.id)
    _record_main_session(engine, task.id)

    captured_resume = {}

    def fake_build(clone, env, resume=None, model=None, thinking=None):
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
    tracker.recovering(task.id, "main")
    runner = WorkflowRunner(engine, config, hub, supervisor, tracker)

    await run_recovery(engine, hub, config, supervisor, tracker, runner, [task])

    assert captured_resume["value"] == "sess-1"
    assert tracker.get(task.id, "main").status == "idle"
    assert tracker.get(task.id, "main").reason == "resumed after daemon restart"
    handle = supervisor.get(task.id, "main")
    assert handle is not None
    # The resumed session is not re-prompted: no user message ever echoed.
    assert not [
        event
        for event in handle.snapshot()
        if event.type == "message_start"
        and event.payload.get("message", {}).get("role") == "user"
    ]
    await supervisor.stop(task.id, "main")


async def test_run_recovery_failure_marks_task_and_session_failed(
    engine_project, tmp_path: Path, monkeypatch
) -> None:
    engine, project = engine_project
    task = _make_task(engine, project, tmp_path, "crash-on-resume")
    mark_spawn_completed(engine, task.id)
    _record_main_session(engine, task.id)

    monkeypatch.setattr(
        agent_module,
        "build_agent_argv",
        lambda clone, env, resume=None, model=None, thinking=None: fake_omp_argv("crash"),
    )

    async def no_preflight(clone_path: str) -> None:
        return None

    monkeypatch.setattr(agent_module, "verify_ask_timeout", no_preflight)

    hub = EventHub()
    events_queue = hub.subscribe()
    tracker = SessionTracker(hub, idle_debounce=0.1)
    config = Config(agent_ready_timeout=5, agent_ring_buffer_size=100)
    supervisor = AgentSupervisor(config, hub, tracker)
    tracker.recovering(task.id, "main")
    runner = WorkflowRunner(engine, config, hub, supervisor, tracker)

    await run_recovery(engine, hub, config, supervisor, tracker, runner, [task])

    assert tracker.get(task.id, "main").status == "failed"
    assert "resume failed" in tracker.get(task.id, "main").reason
    refreshed = get_task(engine, task.id)
    assert refreshed.state == "failed"
    assert "resume failed" in (refreshed.error or "")
    task_updates = [
        e.payload for e in _drain(events_queue) if e.type == "task_updated"
    ]
    assert task_updates and task_updates[-1]["state"] == "failed"


async def test_run_recovery_legacy_complete_run_is_not_redriven(
    engine_project, tmp_path: Path, monkeypatch
) -> None:
    """Legacy-migrated tasks (workflow run already `complete`) are never
    re-driven: their recorded sessions resume and land idle, and no prompt
    is sent."""
    engine, project = engine_project
    task = _make_task(engine, project, tmp_path, "legacy-complete")
    mark_spawn_completed(engine, task.id)
    _record_main_session(engine, task.id)
    # The 0008 migration's legacy backfill: a completed run with one ok record.
    record = append_step_record(engine, task.id, step="work", kind="agent", session="main")
    finish_step_record(engine, task.id, record.seq, status="ok")
    task = set_run_status(engine, task.id, "complete", None)

    def fake_build(clone, env, resume=None, model=None, thinking=None):
        return fake_omp_argv("happy")

    monkeypatch.setattr(agent_module, "build_agent_argv", fake_build)

    async def no_preflight(clone_path: str) -> None:
        return None

    monkeypatch.setattr(agent_module, "verify_ask_timeout", no_preflight)

    hub = EventHub()
    tracker = SessionTracker(hub, idle_debounce=0.1)
    config = Config(agent_ready_timeout=5, agent_ring_buffer_size=100)
    supervisor = AgentSupervisor(config, hub, tracker)
    tracker.recovering(task.id, "main")
    runner = WorkflowRunner(engine, config, hub, supervisor, tracker)

    await run_recovery(engine, hub, config, supervisor, tracker, runner, [task])

    assert tracker.get(task.id, "main").status == "idle"
    assert tracker.get(task.id, "main").reason == "resumed after daemon restart"
    refreshed = get_task(engine, task.id)
    assert refreshed.state == "created"
    assert refreshed.workflow_status == "complete"
    assert refreshed.workflow_step is None
    # Never re-driven: the backfilled record stays the only one, and the
    # resumed session received no prompt.
    assert len(list_step_records(engine, task.id)) == 1
    handle = supervisor.get(task.id, "main")
    assert handle is not None
    assert not [
        event
        for event in handle.snapshot()
        if event.type == "message_start"
        and event.payload.get("message", {}).get("role") == "user"
    ]
    await supervisor.stop(task.id, "main")


def _drain(queue) -> list:
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


def _wait_settled(client: TestClient, auth_headers: dict, task_id: int, timeout: float = 15.0) -> dict:
    """Wait until the spawn pipeline AND the handed-off workflow run have
    both settled: `spawn_completed_at` is stamped before the engine handoff,
    so poll through to the run's terminal status."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = client.get(f"/api/tasks/{task_id}", headers=auth_headers).json()
        if task["state"] == "failed":
            return task
        if task["spawn_completed_at"] is not None and task["workflow_status"] == "complete":
            return task
        time.sleep(0.05)
    raise AssertionError("spawn pipeline did not settle in time")


def test_shutdown_then_restart_resumes_without_reprompt(
    tmp_path: Path, git_checkout: Path
) -> None:
    """Full-stack: spawn a live task, shut the daemon down gracefully (the
    TestClient context exit runs the lifespan `finally`), then start a fresh
    `create_app` against the same data dir and confirm the task's `main`
    session resumes to `idle` without being re-prompted and without ever
    showing as a crash."""
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
        tpl = client.post(
            "/api/templates",
            headers=headers,
            json={"name": "demo", "project_name": "demo"},
        )
        assert tpl.status_code == 201
        response = client.post(
            "/api/tasks",
            headers=headers,
            json={"template_name": "demo", "slug": "fix-bug", "prompt": "fix it"},
        )
        assert response.status_code == 202
        task_id = response.json()["id"]
        settled = _wait_settled(client, headers, task_id)
        assert settled["state"] == "created"
        assert settled["workflow_status"] == "complete"
        assert get_session(app.state.engine, task_id, "main").omp_session_id is not None
    # TestClient.__exit__ ran the lifespan shutdown: agents.shutdown() sent
    # SIGTERM to the live child, and the exit watcher's shutting-down flag
    # kept it from being reported as a crash.

    task_after_shutdown = get_task(app.state.engine, task_id)
    assert task_after_shutdown.state == "created"
    assert get_session(app.state.engine, task_id, "main").omp_session_id is not None
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
            # The snapshot's sessions map is nested task -> session.
            assert sessions[str(task_id)]["main"]["status"] == "starting"
            assert sessions[str(task_id)]["main"]["reason"] == "recovering after daemon restart"

            status = sessions[str(task_id)]["main"]["status"]
            deadline = time.monotonic() + 10.0
            while status == "starting" and time.monotonic() < deadline:
                event = ws.receive_json()
                if (
                    event["type"] == "status_changed"
                    and event["payload"]["task_id"] == task_id
                    and event["payload"]["session"] == "main"
                ):
                    status = event["payload"]["to"]
                assert event["type"] != "agent_exited"

        assert status == "idle"
        task_final = get_task(restarted.state.engine, task_id)
        assert task_final.state == "created"
        assert task_final.workflow_status == "complete"


# --- review history across a restart (review capability; ADR-0016) ----------


def _restart_config(tmp_path: Path) -> Config:
    fake_my_workshop = tmp_path / "fake-my-workshop"
    fake_my_workshop.write_text('#!/bin/sh\necho "ws-test" > .workshop.lock\n')
    fake_my_workshop.chmod(0o755)
    return Config(
        data_dir=tmp_path / "data",
        task_dir_root=tmp_path / "tasks",
        checkout_root=tmp_path / "proj",
        my_workshop_command=(str(fake_my_workshop),),
    )


def _spawn_live_task(app, client: TestClient, git_checkout: Path) -> int:
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
    assert (
        client.post(
            "/api/templates", headers=headers, json={"name": "demo", "project_name": "demo"}
        ).status_code
        == 201
    )
    response = client.post(
        "/api/tasks",
        headers=headers,
        json={"template_name": "demo", "slug": "fix-bug", "prompt": "fix it"},
    )
    assert response.status_code == 202
    task_id = response.json()["id"]
    _wait_settled(client, headers, task_id)
    return task_id


def _snapshot_review(client: TestClient, token: str, task_id: int) -> dict | None:
    with client.websocket_connect(f"/api/ws?token={token}") as ws:
        snapshot = ws.receive_json()
        return snapshot["payload"]["reviews"].get(str(task_id))


def test_restart_preserves_approved_review_history(
    tmp_path: Path, git_checkout: Path
) -> None:
    """An approval earned before a restart still stands afterwards: the
    operator is not asked to re-review, and no dead llmvet link is offered."""
    from ompire_daemon.registry.reviews import append_iteration, open_review

    config = _restart_config(tmp_path)
    app = create_app(config, frontend_dist=tmp_path / "no-dist")
    with TestClient(app) as client:
        task_id = _spawn_live_task(app, client, git_checkout)
        engine = app.state.engine
        open_review(engine, task_id)
        append_iteration(engine, task_id, outcome="comments", comment_count=2)
        # The reviewer exited, so its marker is cleared before the restart.
        app.state.reviews.drop_review(task_id)
        from ompire_daemon.registry.reviews import clear_process_marker

        clear_process_marker(engine, task_id)
        open_review(engine, task_id)
        append_iteration(engine, task_id, outcome="approved", status="approved")
        clear_process_marker(engine, task_id)
    app.state.engine.dispose()

    restarted = create_app(config, frontend_dist=tmp_path / "no-dist")
    with TestClient(restarted) as client:
        review = _snapshot_review(client, restarted.state.auth_token, task_id)

    assert review is not None
    assert review["status"] == "approved"
    # Multi-pass history survives in order.
    assert [it["outcome"] for it in review["iterations"]] == ["comments", "approved"]
    assert review["iterations"][0]["comment_count"] == 2
    # The reviewer process did not survive the restart.
    assert review["url"] is None
    assert review["port"] is None


def test_restart_marks_an_open_review_interrupted(
    tmp_path: Path, git_checkout: Path
) -> None:
    """A review that was open when the daemon died becomes visibly
    interrupted rather than disappearing, and its primary session comes back
    recovering — never `reviewing`."""
    from ompire_daemon.registry.reviews import get_review, open_review

    config = _restart_config(tmp_path)
    app = create_app(config, frontend_dist=tmp_path / "no-dist")
    with TestClient(app) as client:
        task_id = _spawn_live_task(app, client, git_checkout)
        # A live reviewer: the row is open with an uncleared process marker
        # and the clone is parked behind `refs/ompire/review-orig` — exactly
        # the state an ungraceful crash mid-review leaves behind.
        open_review(app.state.engine, task_id)
        record = get_review(app.state.engine, task_id)
        assert record is not None and record.process_started_at is not None
        clone = Path(get_task(app.state.engine, task_id).clone_path)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=clone, check=True, capture_output=True, text=True
        ).stdout.strip()
        subprocess.run(["git", "update-ref", REVIEW_GIT_REF, head], cwd=clone, check=True)
    app.state.engine.dispose()

    restarted = create_app(config, frontend_dist=tmp_path / "no-dist")
    with TestClient(restarted) as client:
        review = _snapshot_review(client, restarted.state.auth_token, task_id)
        with client.websocket_connect(
            f"/api/ws?token={restarted.state.auth_token}"
        ) as ws:
            snapshot = ws.receive_json()
            session = snapshot["payload"]["sessions"][str(task_id)]["main"]

    assert review is not None
    assert review["status"] == "aborted"
    assert [it["outcome"] for it in review["iterations"]] == ["interrupted"]
    assert review["url"] is None
    assert session["status"] != "reviewing"
    # The clone was restored from its durable ref before the first snapshot.
    assert (
        subprocess.run(
            ["git", "rev-parse", "--verify", REVIEW_GIT_REF],
            cwd=clone,
            capture_output=True,
            check=False,
        ).returncode
        != 0
    )


def test_restart_leaves_a_comments_review_open_for_the_agent(
    tmp_path: Path, git_checkout: Path
) -> None:
    """Comments went back to the agent, so the review is `open` on purpose.
    Its reviewer had already exited, so it is not a restart casualty."""
    from ompire_daemon.registry.reviews import (
        append_iteration,
        clear_process_marker,
        open_review,
    )

    config = _restart_config(tmp_path)
    app = create_app(config, frontend_dist=tmp_path / "no-dist")
    with TestClient(app) as client:
        task_id = _spawn_live_task(app, client, git_checkout)
        engine = app.state.engine
        open_review(engine, task_id)
        append_iteration(engine, task_id, outcome="comments", comment_count=1)
        clear_process_marker(engine, task_id)
    app.state.engine.dispose()

    restarted = create_app(config, frontend_dist=tmp_path / "no-dist")
    with TestClient(restarted) as client:
        review = _snapshot_review(client, restarted.state.auth_token, task_id)

    assert review is not None
    assert review["status"] == "open"
    assert [it["outcome"] for it in review["iterations"]] == ["comments"]


def test_restart_of_a_task_with_no_review_has_no_review_entry(
    tmp_path: Path, git_checkout: Path
) -> None:
    """Absence is never filled in: a task that predates review history, or
    never ran one, has no entry to restore."""
    config = _restart_config(tmp_path)
    app = create_app(config, frontend_dist=tmp_path / "no-dist")
    with TestClient(app) as client:
        task_id = _spawn_live_task(app, client, git_checkout)
    app.state.engine.dispose()

    restarted = create_app(config, frontend_dist=tmp_path / "no-dist")
    with TestClient(restarted) as client:
        review = _snapshot_review(client, restarted.state.auth_token, task_id)

    assert review is None


# --- project setup reconciliation (ADR-0022) ----------------------------------


def test_restart_marks_an_interrupted_clone_failed(tmp_path: Path) -> None:
    """A row left `cloning` by a stopped daemon is resolved at the next
    startup — before any client can see the project list — and never sits
    pending forever."""
    config = _restart_config(tmp_path)
    app = create_app(config, frontend_dist=tmp_path / "no-dist")
    destination = config.checkout_root / "cloned"
    staging = config.checkout_root / ".ompire-clone-cloned"
    staging.mkdir(parents=True)
    (staging / "partial").write_text("half a clone\n")
    create_project(
        app.state.engine,
        name="cloned",
        title="Cloned",
        upstream_url="https://example.com/cloned.git",
        checkout_path=str(destination),
        default_checkout_root=config.checkout_root,
        checkout_mode="cloned",
        setup_state="cloning",
    )

    restarted = create_app(config, frontend_dist=tmp_path / "no-dist")

    with TestClient(restarted) as client:
        headers = {"Authorization": f"Bearer {restarted.state.auth_token}"}
        body = client.get("/api/projects/cloned", headers=headers).json()
    assert body["setup_state"] == "failed"
    assert "interrupted by daemon restart" in body["setup_error"]
    assert not staging.exists()
    assert not destination.exists()


def test_restart_marks_a_completed_clone_ready(
    tmp_path: Path, git_checkout: Path
) -> None:
    """The daemon died between the rename and the row write; the filesystem
    is the authority."""
    config = _restart_config(tmp_path)
    app = create_app(config, frontend_dist=tmp_path / "no-dist")
    create_project(
        app.state.engine,
        name="demo",
        title="Demo",
        upstream_url="https://example.com/demo.git",
        checkout_path=str(git_checkout),
        default_checkout_root=config.checkout_root,
        checkout_mode="cloned",
        setup_state="cloning",
    )

    restarted = create_app(config, frontend_dist=tmp_path / "no-dist")

    with TestClient(restarted) as client:
        headers = {"Authorization": f"Bearer {restarted.state.auth_token}"}
        body = client.get("/api/projects/demo", headers=headers).json()
    assert body["setup_state"] == "ready"
    assert body["setup_error"] is None
