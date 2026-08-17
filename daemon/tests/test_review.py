"""Tests for the llmvet review capability (design D-3/D-4/D-5/D-6)."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ompire_daemon.config import Config
from ompire_daemon.review import REVIEW_GIT_REF, ReviewManager


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _write_fake_llmvet(tmp_path: Path, stdout: str, code: int) -> Path:
    script = tmp_path / f"fake-llmvet-{code}"
    script.write_text(
        f"#!/bin/sh\necho {stdout!r}\nexit {code}\n"
        if stdout
        else f"#!/bin/sh\nexit {code}\n"
    )
    script.chmod(0o755)
    return script


def _write_fake_llmvet_comments(tmp_path: Path) -> Path:
    script = tmp_path / "fake-llmvet-comments"
    script.write_text(
        '#!/bin/sh\n'
        'echo "> Please fix the thing"\n'
        'echo "> And the other thing"\n'
        'exit 0\n'
    )
    script.chmod(0o755)
    return script


@pytest.fixture
def review_config(daemon_config: Config, tmp_path: Path) -> Config:
    fake_llmvet = tmp_path / "fake-llmvet"
    fake_llmvet.write_text("#!/bin/sh\nexit 0\n")
    fake_llmvet.chmod(0o755)
    return Config(
        **{
            **daemon_config.__dict__,
            "llmvet_command": (str(fake_llmvet),),
            "review_port_range": (37000, 37005),
        }
    )


@pytest.fixture
def review_app(review_config: Config, tmp_path: Path):
    from ompire_daemon.app import create_app

    return create_app(review_config, frontend_dist=tmp_path / "no-dist")


@pytest.fixture
def review_client(review_app) -> TestClient:
    with TestClient(review_app) as client:
        yield client


@pytest.fixture
def demo_project_and_task(review_client: TestClient, auth_headers: dict[str, str], git_checkout: Path):
    """Create a project and a spawned-to-idle task via REST; return task id."""
    client = review_client
    r = client.post(
        "/api/projects",
        headers=auth_headers,
        json={
            "name": "demo",
            "title": "Demo",
            "upstream_url": "https://example.com/demo.git",
            "checkout_path": str(git_checkout),
        },
    )
    assert r.status_code == 201, r.text
    r = client.post(
        "/api/templates",
        headers=auth_headers,
        json={"name": "demo", "project_name": "demo"},
    )
    assert r.status_code == 201, r.text
    r = client.post(
        "/api/tasks",
        headers=auth_headers,
        json={"template_name": "demo", "slug": "task1", "prompt": "hello"},
    )
    assert r.status_code == 202, r.text
    task_id = r.json()["id"]

    # Wait for fake pipeline to land the session idle.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with client.websocket_connect(f"/api/ws?token={client.app.state.auth_token}") as ws:
            snapshot = ws.receive_json()
            session = snapshot["payload"]["sessions"].get(str(task_id), {}).get("main")
            if session and session["status"] == "idle":
                return task_id
        time.sleep(0.05)
    raise RuntimeError("task did not reach idle")


class TestResetDance:
    @pytest.mark.asyncio
    async def test_reset_dance_exposes_full_delta_and_restores_working_tree(
        self, review_app, tmp_path: Path
    ) -> None:
        app = review_app
        reviews = app.state.reviews
        reviews.start()

        # Build a local clone with checkpoint commits ahead of origin/main.
        def git(*args: str, cwd: Path) -> None:
            subprocess.run(
                ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                cwd=cwd,
                check=True,
                capture_output=True,
            )

        upstream = tmp_path / "upstream.git"
        upstream.mkdir()
        git("init", "--bare", "--initial-branch=main", ".", cwd=upstream)

        checkout = tmp_path / "proj" / "demo"
        checkout.mkdir(parents=True)
        git("init", "--initial-branch=main", ".", cwd=checkout)
        (checkout / "file.txt").write_text("base\n")
        git("add", "file.txt", cwd=checkout)
        git("commit", "-m", "base", cwd=checkout)
        git("remote", "add", "origin", str(upstream), cwd=checkout)
        git("push", "origin", "main", cwd=checkout)

        (checkout / "file.txt").write_text("base\nchange1\n")
        git("commit", "-am", "cp1", cwd=checkout)
        (checkout / "file.txt").write_text("base\nchange1\nchange2\n")
        git("commit", "-am", "cp2", cwd=checkout)

        # Save current HEAD and run the dance manually.
        orig = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=checkout, check=True, capture_output=True, text=True
        ).stdout.strip()
        await reviews._save_review_orig(str(checkout))
        await reviews._reset_to_merge_base(str(checkout), "main")

        diff = subprocess.run(
            ["git", "diff", "--stat"], cwd=checkout, capture_output=True, text=True
        ).stdout
        assert "2 insertions" in diff

        await reviews._restore(str(checkout))
        restored = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=checkout, capture_output=True, text=True
        ).stdout.strip()
        assert restored == orig
        status = subprocess.run(
            ["git", "status", "--short"], cwd=checkout, capture_output=True, text=True
        ).stdout.strip()
        assert status == ""

    @pytest.mark.asyncio
    async def test_startup_restore_of_parked_clone(
        self, review_app, tmp_path: Path
    ) -> None:
        app = review_app

        def git(*args: str, cwd: Path) -> None:
            subprocess.run(
                ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                cwd=cwd,
                check=True,
                capture_output=True,
            )

        checkout = tmp_path / "demo"
        checkout.mkdir()
        git("init", "--initial-branch=main", ".", cwd=checkout)
        (checkout / "file.txt").write_text("base\n")
        git("add", "file.txt", cwd=checkout)
        git("commit", "-m", "base", cwd=checkout)
        orig = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=checkout, check=True, capture_output=True, text=True
        ).stdout.strip()

        # Park the clone at an earlier ref and leave the marker ref.
        subprocess.run(["git", "update-ref", REVIEW_GIT_REF, orig], cwd=checkout, check=True)
        subprocess.run(["git", "reset", "--mixed", orig], cwd=checkout, check=True)

        restored = await ReviewManager.restore_parked_clone(
            str(checkout), app.state.config.spawn_step_timeout
        )
        assert restored is True
        current = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=checkout, capture_output=True, text=True
        ).stdout.strip()
        assert current == orig
        ref_exists = subprocess.run(
            ["git", "rev-parse", "--verify", REVIEW_GIT_REF],
            cwd=checkout,
            capture_output=True,
            text=True,
        )
        assert ref_exists.returncode != 0


class TestReviewRestGuards:
    def test_review_404_unknown_task(self, review_client: TestClient, auth_headers: dict[str, str]) -> None:
        r = review_client.post("/api/tasks/99999/review", headers=auth_headers)
        assert r.status_code == 404

    def test_cancel_review_404_unknown_task(self, review_client: TestClient, auth_headers: dict[str, str]) -> None:
        r = review_client.post("/api/tasks/99999/review/cancel", headers=auth_headers)
        assert r.status_code == 404

    def test_review_409_when_not_idle(
        self, review_client: TestClient, auth_headers: dict[str, str], git_checkout: Path
    ) -> None:
        client = review_client
        client.post(
            "/api/projects",
            headers=auth_headers,
            json={
                "name": "demo",
                "title": "Demo",
                "upstream_url": "https://example.com/demo.git",
                "checkout_path": str(git_checkout),
            },
        )
        client.post(
            "/api/templates",
            headers=auth_headers,
            json={"name": "demo", "project_name": "demo"},
        )
        r = client.post(
            "/api/tasks",
            headers=auth_headers,
            json={"template_name": "demo", "slug": "task1", "prompt": "hello"},
        )
        task_id = r.json()["id"]
        # Task is created but not yet idle.
        r = client.post(f"/api/tasks/{task_id}/review", headers=auth_headers)
        assert r.status_code == 409

    def test_review_409_no_live_agent(
        self, review_client: TestClient, auth_headers: dict[str, str], git_checkout: Path
    ) -> None:
        client = review_client
        client.post(
            "/api/projects",
            headers=auth_headers,
            json={
                "name": "demo",
                "title": "Demo",
                "upstream_url": "https://example.com/demo.git",
                "checkout_path": str(git_checkout),
            },
        )
        client.post(
            "/api/templates",
            headers=auth_headers,
            json={"name": "demo", "project_name": "demo"},
        )
        r = client.post(
            "/api/tasks",
            headers=auth_headers,
            json={"template_name": "demo", "slug": "task1", "prompt": "hello"},
        )
        task_id = r.json()["id"]
        # Manually seed an idle session without a live agent.
        sessions = client.app.state.sessions
        sessions.recovering(task_id, 'main')
        sessions.session_recovered(task_id, 'main')
        r = client.post(f"/api/tasks/{task_id}/review", headers=auth_headers)
        assert r.status_code == 409
        assert "no live agent" in r.json()["detail"]

    def test_snapshot_carries_reviews_map(
        self, review_client: TestClient, auth_headers: dict[str, str]
    ) -> None:
        with review_client.websocket_connect(
            f"/api/ws?token={review_client.app.state.auth_token}"
        ) as ws:
            snapshot = ws.receive_json()
            assert "reviews" in snapshot["payload"]
            assert snapshot["payload"]["reviews"] == {}

    def test_review_starts_and_broadcasts_started(
        self, review_client: TestClient, auth_headers: dict[str, str], demo_project_and_task: int
    ) -> None:
        client = review_client
        task_id = demo_project_and_task
        with client.websocket_connect(f"/api/ws?token={client.app.state.auth_token}") as ws:
            ws.receive_json()  # snapshot
            r = client.post(f"/api/tasks/{task_id}/review", headers=auth_headers)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["status"] == "open"
            assert body["url"].startswith("http://127.0.0.1:")
            # The session transition and review_started both fire; drain until
            # we see the review event.
            event = ws.receive_json()
            while event["type"] != "review_started":
                event = ws.receive_json()
            assert event["payload"]["task_id"] == task_id
            assert event["payload"]["url"] == body["url"]


class TestReviewManagerLifecycle:
    @pytest.mark.asyncio
    async def test_approved_iteration_and_idle_transition(
        self, review_app, tmp_path: Path, git_checkout: Path
    ) -> None:
        app = review_app
        fake_llmvet = _write_fake_llmvet(tmp_path, "", 0)
        app.state.config = Config(
            **{**app.state.config.__dict__, "llmvet_command": (str(fake_llmvet),)}
        )
        reviews = app.state.reviews
        reviews._config = app.state.config
        reviews.start()

        from ompire_daemon.registry.tasks import get_task

        engine = app.state.engine
        # Seed project/task directly; review manager only needs the task row.
        with engine.begin() as conn:
            from ompire_daemon.db import projects, tasks, templates

            conn.execute(
                projects.insert().values(
                    name="demo",
                    title="Demo",
                    upstream_url="https://example.com/demo.git",
                    fork_url=None,
                    checkout_path=str(git_checkout),
                )
            )
            now0 = _now_iso()
            conn.execute(
                templates.insert().values(
                    name="demo",
                    project_name="demo",
                    base_branch="main",
                    branch_pattern="ompire/<slug>",
                    workflow="single-step",
                    workshop_additions="project",
                    model=None,
                    thinking=None,
                    preamble="",
                    created_at=now0,
                    updated_at=now0,
                )
            )
            now = _now_iso()
            result = conn.execute(
                tasks.insert().values(
                    project_name="demo",
                    template_name="demo",
                    slug="task1",
                    branch="ompire/task1",
                    clone_path=str(git_checkout),
                    state="created",
                    prompt="hello",
                    error=None,
                    workshop_id=None,
                    workflow_name="single-step",
                    workflow_status=None,
                    workflow_step=None,
                    spawn_completed_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            task_id = result.inserted_primary_key[0]
        task = get_task(engine, task_id)
        app.state.sessions.recovering(task_id, 'main')
        app.state.sessions.session_recovered(task_id, 'main')

        state = await reviews.start_review(task)
        assert state.status == "open"
        # Wait for fake llmvet (exit 0) to finish.
        deadline = asyncio.get_event_loop().time() + 5
        while reviews.get(task_id) and reviews.get(task_id).status == "open":
            if asyncio.get_event_loop().time() > deadline:
                raise RuntimeError("review did not finish")
            await asyncio.sleep(0.01)

        final = reviews.get(task_id)
        assert final is not None
        assert final.status == "approved"
        assert len(final.iterations) == 1
        assert final.iterations[0].outcome == "approved"
        session = app.state.sessions.get(task_id, "main")
        assert session is not None
        assert session.status == "idle"

    @pytest.mark.asyncio
    async def test_aborted_iteration(
        self, review_app, tmp_path: Path, git_checkout: Path
    ) -> None:
        app = review_app
        fake_llmvet = _write_fake_llmvet(tmp_path, "", 130)
        app.state.config = Config(
            **{**app.state.config.__dict__, "llmvet_command": (str(fake_llmvet),)}
        )
        reviews = app.state.reviews
        reviews._config = app.state.config
        reviews.start()

        from ompire_daemon.db import projects, tasks, templates
        from ompire_daemon.registry.tasks import get_task

        engine = app.state.engine
        with engine.begin() as conn:
            conn.execute(
                projects.insert().values(
                    name="demo",
                    title="Demo",
                    upstream_url="https://example.com/demo.git",
                    fork_url=None,
                    checkout_path=str(git_checkout),
                )
            )
            now0 = _now_iso()
            conn.execute(
                templates.insert().values(
                    name="demo",
                    project_name="demo",
                    base_branch="main",
                    branch_pattern="ompire/<slug>",
                    workflow="single-step",
                    workshop_additions="project",
                    model=None,
                    thinking=None,
                    preamble="",
                    created_at=now0,
                    updated_at=now0,
                )
            )
            now = _now_iso()
            result = conn.execute(
                tasks.insert().values(
                    project_name="demo",
                    template_name="demo",
                    slug="task1",
                    branch="ompire/task1",
                    clone_path=str(git_checkout),
                    state="created",
                    prompt="hello",
                    error=None,
                    workshop_id=None,
                    workflow_name="single-step",
                    workflow_status=None,
                    workflow_step=None,
                    spawn_completed_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            task_id = result.inserted_primary_key[0]
        task = get_task(engine, task_id)
        app.state.sessions.recovering(task_id, 'main')
        app.state.sessions.session_recovered(task_id, 'main')

        await reviews.start_review(task)
        # Wait for fake llmvet (exit 130) to finish.
        deadline = asyncio.get_event_loop().time() + 5
        while reviews.get(task_id) and reviews.get(task_id).status == "open":
            if asyncio.get_event_loop().time() > deadline:
                raise RuntimeError("review did not finish")
            await asyncio.sleep(0.01)

        final = reviews.get(task_id)
        assert final is not None
        assert final.status == "aborted"
        assert len(final.iterations) == 1
        assert final.iterations[0].outcome == "aborted"
        session = app.state.sessions.get(task_id, "main")
        assert session is not None
        assert session.status == "idle"

    @pytest.mark.asyncio
    async def test_comments_iteration_errors_without_live_agent(
        self, review_app, tmp_path: Path, git_checkout: Path
    ) -> None:
        app = review_app
        fake_llmvet = _write_fake_llmvet_comments(tmp_path)
        app.state.config = Config(
            **{**app.state.config.__dict__, "llmvet_command": (str(fake_llmvet),)}
        )
        reviews = app.state.reviews
        reviews._config = app.state.config
        reviews.start()

        from ompire_daemon.db import projects, tasks, templates
        from ompire_daemon.registry.tasks import get_task

        engine = app.state.engine
        with engine.begin() as conn:
            conn.execute(
                projects.insert().values(
                    name="demo",
                    title="Demo",
                    upstream_url="https://example.com/demo.git",
                    fork_url=None,
                    checkout_path=str(git_checkout),
                )
            )
            now0 = _now_iso()
            conn.execute(
                templates.insert().values(
                    name="demo",
                    project_name="demo",
                    base_branch="main",
                    branch_pattern="ompire/<slug>",
                    workflow="single-step",
                    workshop_additions="project",
                    model=None,
                    thinking=None,
                    preamble="",
                    created_at=now0,
                    updated_at=now0,
                )
            )
            now = _now_iso()
            result = conn.execute(
                tasks.insert().values(
                    project_name="demo",
                    template_name="demo",
                    slug="task1",
                    branch="ompire/task1",
                    clone_path=str(git_checkout),
                    state="created",
                    prompt="hello",
                    error=None,
                    workshop_id=None,
                    workflow_name="single-step",
                    workflow_status=None,
                    workflow_step=None,
                    spawn_completed_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            task_id = result.inserted_primary_key[0]
        task = get_task(engine, task_id)
        app.state.sessions.recovering(task_id, 'main')
        app.state.sessions.session_recovered(task_id, 'main')

        await reviews.start_review(task)
        deadline = asyncio.get_event_loop().time() + 5
        while reviews.get(task_id) and reviews.get(task_id).status == "open":
            if asyncio.get_event_loop().time() > deadline:
                raise RuntimeError("review did not finish")
            await asyncio.sleep(0.01)

        final = reviews.get(task_id)
        assert final is not None
        assert final.status == "error"
        assert len(final.iterations) == 2
        assert final.iterations[0].outcome == "comments"
        assert final.iterations[0].comment_count == 2
        # The second iteration recorded the error (no live agent).
        assert final.iterations[1].outcome == "error"
