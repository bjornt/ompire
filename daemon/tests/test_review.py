"""Tests for the llmvet review capability (design D-3/D-4/D-5/D-6)."""

from __future__ import annotations

import asyncio
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ompire_daemon.config import Config
from ompire_daemon.execution_inputs import encode_execution_inputs
from ompire_daemon.review import REVIEW_GIT_REF, ReviewManager
from ompire_daemon.ship import ShipManager
from tests.conftest import TEST_ROLES, make_execution_inputs, spawn_task


def _add_change(clone: Path, name: str = "worked.txt", text: str = "work\n") -> None:
    """Give a clone something to review.

    Review captures a candidate now (ADR-0032) and refuses an empty delta, so a
    test that reviews has to have produced something — which is also what a
    real task has by the time it is reviewed."""
    (clone / name).write_text(text, encoding="utf-8")


def _create_demo_profile(client: TestClient) -> None:
    """The `demo` global model profile a launch inherits from the project."""
    from ompire_daemon.registry.model_profiles import create_model_profile

    create_model_profile(client.app.state.engine, name="demo", roles=TEST_ROLES)


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
    _create_demo_profile(client)
    r = client.post(
        "/api/projects",
        headers=auth_headers,
        json={
            "name": "demo",
            "title": "Demo",
            "upstream_url": "https://example.com/demo.git",
            "checkout_path": str(git_checkout),
            "default_model_profile": "demo",
        },
    )
    assert r.status_code == 201, r.text
    r = spawn_task(client, auth_headers, slug="task1", prompt="hello")
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


class TestCandidateReviewView:
    @pytest.mark.asyncio
    async def test_review_view_exposes_full_delta_and_leaves_the_clone_alone(
        self, review_app, tmp_path: Path
    ) -> None:
        """The reviewer reads the candidate, not the task's live tree.

        Same delta the reset dance used to expose — committed checkpoints and
        pending edits together — but assembled in a separate checkout, so the
        task clone's HEAD, index, and working tree are untouched throughout.
        """
        from ompire_daemon.delivery import capture_candidate, prepare_review_view
        from ompire_daemon.registry.tasks import Task

        app = review_app

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

        clone = tmp_path / "proj" / "demo"
        clone.mkdir(parents=True)
        git("init", "--initial-branch=main", ".", cwd=clone)
        (clone / "file.txt").write_text("base\n")
        git("add", "file.txt", cwd=clone)
        git("commit", "-m", "base", cwd=clone)
        git("remote", "add", "origin", str(upstream), cwd=clone)
        git("push", "origin", "main", cwd=clone)

        (clone / "file.txt").write_text("base\nchange1\n")
        git("commit", "-am", "cp1", cwd=clone)
        (clone / "file.txt").write_text("base\nchange1\nchange2\n")
        git("commit", "-am", "cp2", cwd=clone)
        # Pending work an agent left uncommitted, plus a new untracked file.
        (clone / "file.txt").write_text("base\nchange1\nchange2\npending\n")
        (clone / "new.txt").write_text("brand new\n")

        def rev(*args: str) -> str:
            return subprocess.run(
                ["git", *args], cwd=clone, check=True, capture_output=True, text=True
            ).stdout.strip()

        before_head = rev("rev-parse", "HEAD")
        before_status = rev("status", "--porcelain")

        task = Task(
            id=1,
            project_name="demo",
            slug="task1",
            branch="main",
            clone_path=str(clone),
            state="created",
            prompt="hello",
            error=None,
            workshop_id=None,
            workflow_name="single-step",
            workflow_status=None,
            workflow_step=None,
            workflow_result=None,
            pr_url=None,
            pr_state=None,
            pr_merged_at=None,
            spawn_completed_at=None,
            created_at=_now_iso(),
            updated_at=_now_iso(),
            execution_inputs=None,
        )
        candidate = await capture_candidate(
            app.state.config, app.state.engine, task, base_branch="main"
        )
        view = await prepare_review_view(app.state.config, task.id, candidate)

        # The view shows every part of the delta as reviewable change.
        diff = subprocess.run(
            ["git", "diff", "--stat"], cwd=view, capture_output=True, text=True, check=False
        ).stdout
        assert "3 insertions" in diff
        untracked = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=view,
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        assert "?? new.txt" in untracked
        assert (view / "new.txt").read_text() == "brand new\n"

        # And the task clone is exactly as it was.
        assert rev("rev-parse", "HEAD") == before_head
        assert rev("status", "--porcelain") == before_status

    @pytest.mark.asyncio
    async def test_capture_refuses_a_clone_that_configures_content_filters(
        self, review_app, tmp_path: Path
    ) -> None:
        """An agent-writable clone must not choose what gets captured."""
        from ompire_daemon.delivery import UnsafeCloneConfigError, capture_candidate
        from ompire_daemon.registry.tasks import Task

        app = review_app

        def git(*args: str, cwd: Path) -> None:
            subprocess.run(
                ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                cwd=cwd,
                check=True,
                capture_output=True,
            )

        upstream = tmp_path / "filtered.git"
        upstream.mkdir()
        git("init", "--bare", "--initial-branch=main", ".", cwd=upstream)
        clone = tmp_path / "filtered"
        clone.mkdir()
        git("init", "--initial-branch=main", ".", cwd=clone)
        (clone / "a.txt").write_text("a\n")
        git("add", "a.txt", cwd=clone)
        git("commit", "-m", "base", cwd=clone)
        git("remote", "add", "origin", str(upstream), cwd=clone)
        git("push", "origin", "main", cwd=clone)
        (clone / "b.txt").write_text("b\n")
        git("config", "filter.evil.clean", "cat /etc/passwd", cwd=clone)

        task = Task(
            id=2,
            project_name="demo",
            slug="task2",
            branch="main",
            clone_path=str(clone),
            state="created",
            prompt="hello",
            error=None,
            workshop_id=None,
            workflow_name="single-step",
            workflow_status=None,
            workflow_step=None,
            workflow_result=None,
            pr_url=None,
            pr_state=None,
            pr_merged_at=None,
            spawn_completed_at=None,
            created_at=_now_iso(),
            updated_at=_now_iso(),
            execution_inputs=None,
        )
        with pytest.raises(UnsafeCloneConfigError) as caught:
            await capture_candidate(
                app.state.config, app.state.engine, task, base_branch="main"
            )
        assert "filter.evil.clean" in str(caught.value)


class TestLegacyParkedClone:
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
        assert restored == "restored"
        current = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=checkout, capture_output=True, text=True, check=False
        ).stdout.strip()
        assert current == orig
        ref_exists = subprocess.run(
            ["git", "rev-parse", "--verify", REVIEW_GIT_REF],
            cwd=checkout,
            capture_output=True,
            text=True,
            check=False,
        )
        assert ref_exists.returncode != 0

    @pytest.mark.asyncio
    async def test_a_clone_with_no_legacy_ref_reports_absent(
        self, review_app, tmp_path: Path
    ) -> None:
        """`absent` and `unsafe` must stay distinguishable: only the second is a
        reason to stop working on a task."""
        app = review_app
        checkout = tmp_path / "clean"
        checkout.mkdir()
        subprocess.run(
            ["git", "init", "--initial-branch=main", "."],
            cwd=checkout,
            check=True,
            capture_output=True,
        )
        assert (
            await ReviewManager.restore_parked_clone(
                str(checkout), app.state.config.spawn_step_timeout
            )
            == "absent"
        )
        assert (
            await ShipManager.restore_parked_clone(
                str(checkout), app.state.config.spawn_step_timeout
            )
            == "absent"
        )

    @pytest.mark.asyncio
    async def test_an_unrestorable_legacy_ref_is_kept_and_reported_unsafe(
        self, review_app, tmp_path: Path
    ) -> None:
        """A ref Ompire cannot honour is evidence. It stays, and the caller is
        told, rather than being deleted or silently ignored."""
        app = review_app
        checkout = tmp_path / "broken"
        checkout.mkdir()
        subprocess.run(
            ["git", "init", "--initial-branch=main", "."],
            cwd=checkout,
            check=True,
            capture_output=True,
        )
        for name in ("file.txt",):
            (checkout / name).write_text("base\n")
        subprocess.run(["git", "add", "."], cwd=checkout, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "base"],
            cwd=checkout,
            check=True,
            capture_output=True,
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=checkout,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        # A parked ref pointing at an object this clone does not have: the
        # restore cannot succeed, so the marker must survive.
        missing = "0" * 39 + "1"
        (checkout / ".git" / "refs" / "ompire").mkdir(parents=True, exist_ok=True)
        (checkout / ".git" / "refs" / "ompire" / "ship-orig").write_text(missing + "\n")
        (checkout / ".git" / "refs" / "ompire" / "review-orig").write_text(missing + "\n")

        assert (
            await ShipManager.restore_parked_clone(
                str(checkout), app.state.config.spawn_step_timeout
            )
            == "unsafe"
        )
        assert (
            await ReviewManager.restore_parked_clone(
                str(checkout), app.state.config.spawn_step_timeout
            )
            == "unsafe"
        )
        # Nothing was destroyed: the refs are still there and HEAD is unmoved.
        for ref in ("refs/ompire/ship-orig", "refs/ompire/review-orig"):
            assert (
                subprocess.run(
                    ["git", "rev-parse", "--verify", ref],
                    cwd=checkout,
                    capture_output=True,
                    check=False,
                ).returncode
                == 0
            ), f"{ref} was removed without a verified restore"
        assert (
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=checkout,
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
            == head
        )


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
        _create_demo_profile(client)
        client.post(
            "/api/projects",
            headers=auth_headers,
            json={
                "name": "demo",
                "title": "Demo",
                "upstream_url": "https://example.com/demo.git",
                "checkout_path": str(git_checkout),
                "default_model_profile": "demo",
            },
        )
        r = spawn_task(client, auth_headers, slug="task1", prompt="hello")
        task_id = r.json()["id"]
        # Task is created but not yet idle.
        r = client.post(f"/api/tasks/{task_id}/review", headers=auth_headers)
        assert r.status_code == 409

    def test_review_refuses_an_empty_delta(
        self, review_client: TestClient, auth_headers: dict[str, str], demo_project_and_task: int
    ) -> None:
        """Nothing to review is a refusal that says so, not a reviewer error."""
        r = review_client.post(
            f"/api/tasks/{demo_project_and_task}/review", headers=auth_headers
        )
        assert r.status_code == 409, r.text
        assert "nothing to deliver" in r.json()["detail"]

    def test_review_409_no_live_agent(
        self, review_client: TestClient, auth_headers: dict[str, str], git_checkout: Path
    ) -> None:
        client = review_client
        _create_demo_profile(client)
        client.post(
            "/api/projects",
            headers=auth_headers,
            json={
                "name": "demo",
                "title": "Demo",
                "upstream_url": "https://example.com/demo.git",
                "checkout_path": str(git_checkout),
                "default_model_profile": "demo",
            },
        )
        r = spawn_task(client, auth_headers, slug="task1", prompt="hello")
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
        task = client.get(f"/api/tasks/{task_id}", headers=auth_headers).json()
        _add_change(Path(task["clone_path"]))
        with client.websocket_connect(f"/api/ws?token={client.app.state.auth_token}") as ws:
            ws.receive_json()  # snapshot
            r = client.post(f"/api/tasks/{task_id}/review", headers=auth_headers)
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["status"] == "open"
            assert body["url"].startswith("http://127.0.0.1:")
            # The approval this review can produce will name the content it
            # graded, not merely that something was approved (ADR-0032).
            assert body["candidate_id"]
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
        seeded_inputs = encode_execution_inputs(
            make_execution_inputs(
                engine=engine,
                checkout_path=str(git_checkout),
                branch="ompire/task1",
            )
        )
        # Seed project/task directly; review manager only needs the task row.
        with engine.begin() as conn:
            from ompire_daemon.db import projects, tasks

            conn.execute(
                projects.insert().values(
                    name="demo",
                    title="Demo",
                    upstream_url="https://example.com/demo.git",
                    fork_url=None,
                    checkout_path=str(git_checkout),
                    branch_pattern="ompire/<slug>",
                )
            )
            now = _now_iso()
            result = conn.execute(
                tasks.insert().values(
                    project_name="demo",
                    execution_inputs_json=seeded_inputs,
                    slug="task1",
                    branch="ompire/task1",
                    clone_path=str(git_checkout),
                    state="created",
                    prompt="hello",
                    error=None,
                    workshop_id=None,
                    workflow_name="plain",
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

        _add_change(git_checkout)

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

        from ompire_daemon.db import projects, tasks
        from ompire_daemon.registry.tasks import get_task

        engine = app.state.engine
        seeded_inputs = encode_execution_inputs(
            make_execution_inputs(
                engine=engine,
                checkout_path=str(git_checkout),
                branch="ompire/task1",
            )
        )
        with engine.begin() as conn:
            conn.execute(
                projects.insert().values(
                    name="demo",
                    title="Demo",
                    upstream_url="https://example.com/demo.git",
                    fork_url=None,
                    checkout_path=str(git_checkout),
                    branch_pattern="ompire/<slug>",
                )
            )
            now = _now_iso()
            result = conn.execute(
                tasks.insert().values(
                    project_name="demo",
                    execution_inputs_json=seeded_inputs,
                    slug="task1",
                    branch="ompire/task1",
                    clone_path=str(git_checkout),
                    state="created",
                    prompt="hello",
                    error=None,
                    workshop_id=None,
                    workflow_name="plain",
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

        _add_change(git_checkout)

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

        from ompire_daemon.db import projects, tasks
        from ompire_daemon.registry.tasks import get_task

        engine = app.state.engine
        seeded_inputs = encode_execution_inputs(
            make_execution_inputs(
                engine=engine,
                checkout_path=str(git_checkout),
                branch="ompire/task1",
            )
        )
        with engine.begin() as conn:
            conn.execute(
                projects.insert().values(
                    name="demo",
                    title="Demo",
                    upstream_url="https://example.com/demo.git",
                    fork_url=None,
                    checkout_path=str(git_checkout),
                    branch_pattern="ompire/<slug>",
                )
            )
            now = _now_iso()
            result = conn.execute(
                tasks.insert().values(
                    project_name="demo",
                    execution_inputs_json=seeded_inputs,
                    slug="task1",
                    branch="ompire/task1",
                    clone_path=str(git_checkout),
                    state="created",
                    prompt="hello",
                    error=None,
                    workshop_id=None,
                    workflow_name="plain",
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

        _add_change(git_checkout)

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


def _seed_project_and_task(engine, git_checkout: Path, slug: str = "task1") -> int:
    """Seed project/task rows directly — the same shape the lifecycle tests
    build inline, with the task carrying its accepted inputs. Returns the
    task id."""
    from ompire_daemon.db import projects, tasks

    # Retaining the pinned definition is itself a write, so it happens before
    # the seeding transaction rather than inside it.
    seeded_inputs = encode_execution_inputs(
        make_execution_inputs(
            engine=engine, checkout_path=str(git_checkout), branch=f"ompire/{slug}"
        )
    )
    with engine.begin() as conn:
        if conn.execute(projects.select().where(projects.c.name == "demo")).first() is None:
            conn.execute(
                projects.insert().values(
                    name="demo",
                    title="Demo",
                    upstream_url="https://example.com/demo.git",
                    fork_url=None,
                    checkout_path=str(git_checkout),
                    branch_pattern="ompire/<slug>",
                )
            )
        now = _now_iso()
        result = conn.execute(
            tasks.insert().values(
                project_name="demo",
                execution_inputs_json=seeded_inputs,
                slug=slug,
                branch=f"ompire/{slug}",
                clone_path=str(git_checkout),
                state="created",
                prompt="hello",
                error=None,
                workshop_id=None,
                workflow_name="plain",
                workflow_status=None,
                workflow_step=None,
                spawn_completed_at=None,
                created_at=now,
                updated_at=now,
            )
        )
        return result.inserted_primary_key[0]


class TestReviewDurability:
    """Review status and iterations are durable rows, not manager memory
    (ADR-0016's review slice)."""

    @pytest.mark.asyncio
    async def test_completed_review_is_written_to_the_registry(
        self, review_app, tmp_path: Path, git_checkout: Path
    ) -> None:
        from ompire_daemon.registry.reviews import get_review
        from ompire_daemon.registry.tasks import get_task

        app = review_app
        fake_llmvet = _write_fake_llmvet(tmp_path, "", 0)
        app.state.config = Config(
            **{**app.state.config.__dict__, "llmvet_command": (str(fake_llmvet),)}
        )
        reviews = app.state.reviews
        reviews._config = app.state.config
        reviews.start()

        engine = app.state.engine
        task_id = _seed_project_and_task(engine, git_checkout)
        app.state.sessions.recovering(task_id, "main")
        app.state.sessions.session_recovered(task_id, "main")

        # The row exists as soon as the review opens, with the write-ahead
        # process marker stamped before llmvet was launched.
        _add_change(git_checkout)
        await reviews.start_review(get_task(engine, task_id))
        opened = get_review(engine, task_id)
        assert opened is not None
        assert opened.status == "open"
        assert opened.process_started_at is not None

        deadline = asyncio.get_event_loop().time() + 5
        while reviews.get(task_id) and reviews.get(task_id).status == "open":
            if asyncio.get_event_loop().time() > deadline:
                raise RuntimeError("review did not finish")
            await asyncio.sleep(0.01)

        record = get_review(engine, task_id)
        assert record is not None
        assert record.status == "approved"
        assert [it.outcome for it in record.iterations] == ["approved"]
        # The process is gone: marker cleared, no URL or port to offer.
        assert record.process_started_at is None
        state = reviews.get(task_id)
        assert state is not None
        assert state.url is None
        assert state.port is None

    @pytest.mark.asyncio
    async def test_re_review_appends_to_one_history(
        self, review_app, tmp_path: Path, git_checkout: Path
    ) -> None:
        from ompire_daemon.registry.reviews import get_review
        from ompire_daemon.registry.tasks import get_task

        app = review_app
        reviews = app.state.reviews
        reviews.start()
        engine = app.state.engine
        task_id = _seed_project_and_task(engine, git_checkout)
        app.state.sessions.recovering(task_id, "main")
        app.state.sessions.session_recovered(task_id, "main")
        task = get_task(engine, task_id)

        async def run_once(script: Path) -> None:
            app.state.config = Config(
                **{**app.state.config.__dict__, "llmvet_command": (str(script),)}
            )
            reviews._config = app.state.config
            _add_change(git_checkout)
            await reviews.start_review(task)
            deadline = asyncio.get_event_loop().time() + 5
            while task_id in reviews._processes:
                if asyncio.get_event_loop().time() > deadline:
                    raise RuntimeError("review did not finish")
                await asyncio.sleep(0.01)
            # Let the watcher's finalization land.
            await asyncio.sleep(0.2)

        await run_once(_write_fake_llmvet(tmp_path, "", 130))
        await run_once(_write_fake_llmvet(tmp_path, "", 0))

        record = get_review(engine, task_id)
        assert record is not None
        assert record.status == "approved"
        assert [(it.seq, it.outcome) for it in record.iterations] == [
            (1, "aborted"),
            (2, "approved"),
        ]

    def test_cleanup_retains_history_and_purge_deletes_it(
        self, review_client: TestClient, auth_headers: dict[str, str], git_checkout: Path
    ) -> None:
        """Cleanup keeps the evidence that approved the ship; only purge
        deletes it (`VISION.md` principle 4, ADR-0016)."""
        from ompire_daemon.registry.reviews import (
            append_iteration,
            get_review,
            open_review,
        )

        client = review_client
        engine = client.app.state.engine
        task_id = _seed_project_and_task(engine, git_checkout)
        open_review(engine, task_id)
        append_iteration(engine, task_id, outcome="approved", status="approved")
        # The clone path must sit under the task root for cleanup to proceed.
        from ompire_daemon.db import tasks as tasks_table

        clone = client.app.state.config.task_dir_root / "demo" / "task1"
        clone.mkdir(parents=True, exist_ok=True)
        with engine.begin() as conn:
            conn.execute(
                tasks_table.update()
                .where(tasks_table.c.id == task_id)
                .values(clone_path=str(clone))
            )

        r = client.post(f"/api/tasks/{task_id}/cleanup", headers=auth_headers)
        assert r.status_code == 200, r.text
        record = get_review(engine, task_id)
        assert record is not None
        assert record.status == "approved"
        assert [it.outcome for it in record.iterations] == ["approved"]
        # An archived task carries no live process, so nothing may later read
        # this review as interrupted.
        assert record.process_started_at is None

        r = client.delete(f"/api/tasks/{task_id}", headers=auth_headers)
        assert r.status_code == 200, r.text
        assert get_review(engine, task_id) is None


class TestRestoreReviews:
    """`restore_reviews` runs before the first snapshot and closes out only
    the reviews whose llmvet process died with the daemon."""

    def test_open_review_with_live_marker_becomes_interrupted(
        self, app, git_checkout: Path
    ) -> None:
        from ompire_daemon.registry.reviews import get_review, open_review
        from ompire_daemon.review import restore_reviews

        engine = app.state.engine
        task_id = _seed_project_and_task(engine, git_checkout)
        open_review(engine, task_id)

        assert restore_reviews(engine) == [task_id]

        record = get_review(engine, task_id)
        assert record is not None
        assert record.status == "aborted"
        assert [it.outcome for it in record.iterations] == ["interrupted"]
        assert record.process_started_at is None

    def test_comments_review_is_restored_untouched(
        self, app, git_checkout: Path
    ) -> None:
        """Its reviewer already exited and its comments are with the agent:
        the review is `open` on purpose, not a restart casualty."""
        from ompire_daemon.registry.reviews import (
            append_iteration,
            clear_process_marker,
            get_review,
            open_review,
        )
        from ompire_daemon.review import restore_reviews

        engine = app.state.engine
        task_id = _seed_project_and_task(engine, git_checkout)
        open_review(engine, task_id)
        append_iteration(engine, task_id, outcome="comments", comment_count=3)
        clear_process_marker(engine, task_id)

        assert restore_reviews(engine) == []

        record = get_review(engine, task_id)
        assert record is not None
        assert record.status == "open"
        assert [it.outcome for it in record.iterations] == ["comments"]

    def test_terminal_review_is_restored_untouched(
        self, app, git_checkout: Path
    ) -> None:
        from ompire_daemon.registry.reviews import (
            append_iteration,
            clear_process_marker,
            get_review,
            open_review,
        )
        from ompire_daemon.review import restore_reviews

        engine = app.state.engine
        task_id = _seed_project_and_task(engine, git_checkout)
        open_review(engine, task_id)
        append_iteration(engine, task_id, outcome="approved", status="approved")
        clear_process_marker(engine, task_id)

        assert restore_reviews(engine) == []

        record = get_review(engine, task_id)
        assert record is not None
        assert record.status == "approved"
        assert [it.outcome for it in record.iterations] == ["approved"]

    def test_interrupted_review_can_be_re_reviewed_into_the_same_history(
        self, app, git_checkout: Path
    ) -> None:
        from ompire_daemon.registry.reviews import (
            append_iteration,
            get_review,
            open_review,
        )
        from ompire_daemon.review import restore_reviews

        engine = app.state.engine
        task_id = _seed_project_and_task(engine, git_checkout)
        open_review(engine, task_id)
        restore_reviews(engine)

        # A fresh review after the restart appends rather than starting over.
        open_review(engine, task_id)
        append_iteration(engine, task_id, outcome="approved", status="approved")

        record = get_review(engine, task_id)
        assert record is not None
        assert record.status == "approved"
        assert [(it.seq, it.outcome) for it in record.iterations] == [
            (1, "interrupted"),
            (2, "approved"),
        ]


class TestCleanupWithLiveReviewer:
    @pytest.mark.asyncio
    async def test_cleanup_lands_a_live_review_terminal(
        self, review_app, tmp_path: Path, git_checkout: Path
    ) -> None:
        """Cleanup cancels the reviewer without waiting for its watcher, so
        the review must be landed terminal here. A retained row left `open`
        with no process would show an archived task as still under review."""
        from ompire_daemon.registry.reviews import get_review
        from ompire_daemon.registry.tasks import get_task

        app = review_app
        slow = tmp_path / "slow-llmvet"
        slow.write_text("#!/bin/sh\nsleep 5\n")
        slow.chmod(0o755)
        app.state.config = Config(
            **{**app.state.config.__dict__, "llmvet_command": (str(slow),)}
        )
        reviews = app.state.reviews
        reviews._config = app.state.config
        reviews.start()

        engine = app.state.engine
        task_id = _seed_project_and_task(engine, git_checkout)
        app.state.sessions.recovering(task_id, "main")
        app.state.sessions.session_recovered(task_id, "main")
        _add_change(git_checkout)
        await reviews.start_review(get_task(engine, task_id))
        deadline = asyncio.get_event_loop().time() + 5
        while task_id not in reviews._processes:
            if asyncio.get_event_loop().time() > deadline:
                raise RuntimeError("llmvet never launched")
            await asyncio.sleep(0.01)

        await reviews.cancel_and_drop(task_id)

        record = get_review(engine, task_id)
        assert record is not None
        assert record.status == "aborted"
        assert [it.outcome for it in record.iterations] == ["aborted"]
        assert record.process_started_at is None
        state = reviews.get(task_id)
        assert state is not None
        assert state.url is None
