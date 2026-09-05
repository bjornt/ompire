"""Clone-mode project setup: the background job, its failure and retry paths,
and startup reconciliation (ADR-0022)."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ompire_daemon.config import Config
from ompire_daemon.events import EventHub
from ompire_daemon.projectsetup import ProjectSetupManager, clone_target
from ompire_daemon.registry.projects import (
    create_project,
    get_project,
    list_projects,
)

from .conftest import make_adoptable_checkout


def make_upstream(tmp_path: Path, name: str = "upstream") -> Path:
    """A bare repository with one commit, usable as a clone source."""
    bare = tmp_path / f"{name}.git"
    bare.mkdir()
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", "."],
        cwd=bare,
        check=True,
        capture_output=True,
    )
    seed = tmp_path / f"{name}-seed"
    seed.mkdir()

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
            cwd=seed,
            check=True,
            capture_output=True,
        )

    git("init", "--initial-branch=main", ".")
    (seed / "README.md").write_text("upstream\n")
    git("add", "README.md")
    git("commit", "-m", "initial")
    git("remote", "add", "origin", str(bare))
    git("push", "origin", "main")
    return bare


@pytest.fixture
def setup_manager(daemon_config: Config, app) -> ProjectSetupManager:
    return ProjectSetupManager(daemon_config, app.state.engine, EventHub())


def register_cloning(app, name: str, upstream: str, fork: str | None = None):
    root = app.state.config.checkout_root
    return create_project(
        app.state.engine,
        name=name,
        title=name.title(),
        upstream_url=upstream,
        fork_url=fork,
        checkout_path=str(clone_target(root, name).destination),
        default_checkout_root=root,
        checkout_mode="cloned",
        setup_state="cloning",
    )


# --- the job -----------------------------------------------------------------


async def test_clone_produces_a_ready_checkout(
    app, setup_manager: ProjectSetupManager, tmp_path: Path
) -> None:
    upstream = make_upstream(tmp_path)
    project = register_cloning(app, "demo", str(upstream))

    setup_manager.start(project)
    await asyncio.gather(*setup_manager._jobs.values())

    stored = get_project(app.state.engine, "demo")
    assert stored.setup_state == "ready"
    assert stored.setup_error is None
    assert (Path(stored.checkout_path) / "README.md").exists()
    assert (Path(stored.checkout_path) / ".git").is_dir()


async def test_fork_url_becomes_a_second_remote(
    app, setup_manager: ProjectSetupManager, tmp_path: Path
) -> None:
    upstream = make_upstream(tmp_path)
    project = register_cloning(
        app, "demo", str(upstream), fork="https://example.com/fork.git"
    )

    setup_manager.start(project)
    await asyncio.gather(*setup_manager._jobs.values())

    stored = get_project(app.state.engine, "demo")
    assert stored.setup_state == "ready"
    remotes = subprocess.run(
        ["git", "remote", "-v"],
        cwd=stored.checkout_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "origin" in remotes
    assert "fork\thttps://example.com/fork.git" in remotes


async def test_unreachable_upstream_fails_with_git_stderr(
    app, setup_manager: ProjectSetupManager, tmp_path: Path
) -> None:
    project = register_cloning(app, "demo", "https://example.invalid/nope.git")

    setup_manager.start(project)
    await asyncio.gather(*setup_manager._jobs.values())

    stored = get_project(app.state.engine, "demo")
    assert stored.setup_state == "failed"
    assert stored.setup_error is not None
    assert "clone" in stored.setup_error
    # Nothing was left where the checkout would have gone.
    assert not Path(stored.checkout_path).exists()
    target = clone_target(app.state.config.checkout_root, "demo")
    assert not target.staging.exists()


async def test_failed_clone_can_be_retried_successfully(
    app, setup_manager: ProjectSetupManager, tmp_path: Path
) -> None:
    project = register_cloning(app, "demo", "https://example.invalid/nope.git")
    setup_manager.start(project)
    await asyncio.gather(*setup_manager._jobs.values())
    assert get_project(app.state.engine, "demo").setup_state == "failed"

    # Point it at a real upstream and retry, as the operator would after
    # fixing the URL.
    upstream = make_upstream(tmp_path)
    from ompire_daemon.registry.projects import update_project

    update_project(
        app.state.engine,
        "demo",
        title="Demo",
        upstream_url=str(upstream),
        fork_url=None,
        checkout_path=project.checkout_path,
    )
    armed = setup_manager.retry("demo")
    assert armed.setup_state == "cloning"
    assert armed.setup_error is None
    await asyncio.gather(*setup_manager._jobs.values())

    assert get_project(app.state.engine, "demo").setup_state == "ready"


async def test_existing_destination_is_refused_without_touching_it(
    app, setup_manager: ProjectSetupManager, tmp_path: Path
) -> None:
    upstream = make_upstream(tmp_path)
    existing = make_adoptable_checkout(app.state.config.checkout_root, "demo")
    (existing / "PRECIOUS").write_text("do not clobber\n")
    project = register_cloning(app, "demo", str(upstream))

    setup_manager.start(project)
    await asyncio.gather(*setup_manager._jobs.values())

    stored = get_project(app.state.engine, "demo")
    assert stored.setup_state == "failed"
    assert "already exists" in (stored.setup_error or "")
    assert (existing / "PRECIOUS").read_text() == "do not clobber\n"


async def test_retry_is_refused_for_an_adopted_project(
    app, setup_manager: ProjectSetupManager
) -> None:
    make_adoptable_checkout(app.state.config.checkout_root, "demo")
    create_project(
        app.state.engine,
        name="demo",
        title="Demo",
        upstream_url="https://example.com/demo.git",
        default_checkout_root=app.state.config.checkout_root,
    )
    with pytest.raises(ValueError, match="nothing for Ompire to retry"):
        setup_manager.retry("demo")


# --- startup reconciliation ---------------------------------------------------


async def test_reconcile_marks_a_completed_clone_ready(
    app, setup_manager: ProjectSetupManager, tmp_path: Path
) -> None:
    """The daemon died between the rename and the row write."""
    make_adoptable_checkout(app.state.config.checkout_root, "demo")
    register_cloning(app, "demo", "https://example.com/demo.git")

    await setup_manager.reconcile_pending()

    assert get_project(app.state.engine, "demo").setup_state == "ready"


async def test_reconcile_fails_an_interrupted_clone_and_clears_staging(
    app, setup_manager: ProjectSetupManager
) -> None:
    project = register_cloning(app, "demo", "https://example.com/demo.git")
    target = clone_target(app.state.config.checkout_root, "demo")
    target.staging.mkdir(parents=True)
    (target.staging / "partial").write_text("half a clone\n")

    await setup_manager.reconcile_pending()

    stored = get_project(app.state.engine, "demo")
    assert stored.setup_state == "failed"
    assert "interrupted by daemon restart" in (stored.setup_error or "")
    assert not target.staging.exists()
    assert not Path(project.checkout_path).exists()


async def test_reconcile_leaves_ready_and_failed_projects_alone(
    app, setup_manager: ProjectSetupManager
) -> None:
    make_adoptable_checkout(app.state.config.checkout_root, "adopted")
    create_project(
        app.state.engine,
        name="adopted",
        title="Adopted",
        upstream_url="https://example.com/adopted.git",
        default_checkout_root=app.state.config.checkout_root,
    )

    await setup_manager.reconcile_pending()

    assert [p.setup_state for p in list_projects(app.state.engine)] == ["ready"]


async def test_shutdown_cancels_a_running_clone(
    app, setup_manager: ProjectSetupManager, tmp_path: Path
) -> None:
    """A cancelled job leaves the row `cloning` for reconciliation to resolve,
    and removes its own staging tree."""
    project = register_cloning(app, "demo", "https://example.invalid/slow.git")
    setup_manager.start(project)
    await asyncio.sleep(0)
    await setup_manager.shutdown()

    target = clone_target(app.state.config.checkout_root, "demo")
    assert not target.staging.exists()
    assert not Path(project.checkout_path).exists()


# --- REST surface -------------------------------------------------------------


def test_clone_mode_registration_returns_a_cloning_project(
    client: TestClient, auth_headers: dict[str, str], tmp_path: Path
) -> None:
    upstream = make_upstream(tmp_path)

    response = client.post(
        "/api/projects",
        headers=auth_headers,
        json={
            "name": "cloned",
            "title": "Cloned",
            "upstream_url": f"ssh://git@example.com/{upstream.name}",
            "checkout_mode": "clone",
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["checkout_mode"] == "cloned"
    assert body["setup_state"] == "cloning"
    assert body["fetch_remote"] == "origin"
    assert body["checkout_path"].endswith("/cloned")


def test_clone_mode_rejects_a_supplied_checkout_path(
    client: TestClient, auth_headers: dict[str, str], tmp_path: Path
) -> None:
    response = client.post(
        "/api/projects",
        headers=auth_headers,
        json={
            "name": "cloned",
            "title": "Cloned",
            "upstream_url": "https://example.com/repo.git",
            "checkout_mode": "clone",
            "checkout_path": str(tmp_path / "somewhere"),
        },
    )
    assert response.status_code == 422
    assert "derived" in response.json()["detail"]


def test_clone_mode_refuses_an_existing_destination(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    make_adoptable_checkout(client.app.state.config.checkout_root, "cloned")

    response = client.post(
        "/api/projects",
        headers=auth_headers,
        json={
            "name": "cloned",
            "title": "Cloned",
            "upstream_url": "https://example.com/repo.git",
            "checkout_mode": "clone",
        },
    )

    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]
    assert client.get("/api/projects", headers=auth_headers).json() == []


def test_retry_endpoint_schedules_the_job_through_the_route(
    client: TestClient, auth_headers: dict[str, str], app
) -> None:
    """Drive retry through HTTP, not just the manager.

    The route has to be async: scheduling the clone needs a running event
    loop, and a sync route would run in FastAPI's threadpool where there is
    none. Calling the manager directly from an async test cannot catch that.
    """
    from ompire_daemon.registry.projects import create_project

    root = app.state.config.checkout_root
    create_project(
        app.state.engine,
        name="retryable",
        title="Retryable",
        upstream_url="https://example.invalid/nope.git",
        checkout_path=str(clone_target(root, "retryable").destination),
        default_checkout_root=root,
        checkout_mode="cloned",
        setup_state="failed",
    )

    response = client.post("/api/projects/retryable/setup/retry", headers=auth_headers)

    assert response.status_code == 202, response.text
    assert response.json()["setup_state"] == "cloning"
    assert response.json()["setup_error"] is None


def test_retry_endpoint_is_404_for_unknown_and_409_for_adopted(
    client: TestClient, auth_headers: dict[str, str], git_checkout: Path
) -> None:
    assert (
        client.post("/api/projects/nope/setup/retry", headers=auth_headers).status_code
        == 404
    )
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
    response = client.post("/api/projects/demo/setup/retry", headers=auth_headers)
    assert response.status_code == 409
    assert "nothing for Ompire to retry" in response.json()["detail"]


def test_clone_mode_with_an_unknown_profile_starts_no_setup_job(
    client: TestClient, auth_headers: dict[str, str], app
) -> None:
    """The profile reference is validated on the shared registration path,
    before the row is committed and before `setup.start(project)` — so a bad
    reference cannot leave a clone job running against a project that was
    never registered."""
    response = client.post(
        "/api/projects",
        headers=auth_headers,
        json={
            "name": "cloned",
            "title": "Cloned",
            "upstream_url": "https://example.com/repo.git",
            "checkout_mode": "clone",
            "default_model_profile": "ghost",
        },
    )

    assert response.status_code == 422
    assert "unknown model profile" in response.json()["detail"]
    assert app.state.project_setup._jobs == {}
    assert not clone_target(app.state.config.checkout_root, "cloned").destination.exists()
    assert client.get("/api/projects/cloned", headers=auth_headers).status_code == 404


async def test_setup_completion_and_retry_preserve_the_default_profile(
    app, setup_manager: ProjectSetupManager, tmp_path: Path
) -> None:
    """Setup only owns `setup_state`/`setup_error`. A clone that fails and is
    then retried must still come back with the profile the operator chose at
    registration."""
    from ompire_daemon.registry.model_profiles import create_model_profile

    create_model_profile(
        app.state.engine,
        name="balanced",
        roles={
            role: {"model": "openai/o3", "thinking": "high"}
            for role in ("default", "smol", "slow", "plan")
        },
    )
    root = app.state.config.checkout_root
    project = create_project(
        app.state.engine,
        name="demo",
        title="Demo",
        upstream_url="https://example.invalid/nope.git",
        checkout_path=str(clone_target(root, "demo").destination),
        default_checkout_root=root,
        checkout_mode="cloned",
        setup_state="cloning",
        default_model_profile="balanced",
    )
    assert project.default_model_profile == "balanced"

    setup_manager.start(project)
    await asyncio.gather(*setup_manager._jobs.values())
    failed = get_project(app.state.engine, "demo")
    assert failed.setup_state == "failed"
    assert failed.default_model_profile == "balanced"

    upstream = make_upstream(tmp_path)
    from ompire_daemon.registry.projects import update_project

    update_project(
        app.state.engine,
        "demo",
        title="Demo",
        upstream_url=str(upstream),
        fork_url=None,
        checkout_path=project.checkout_path,
    )
    setup_manager.retry("demo")
    await asyncio.gather(*setup_manager._jobs.values())

    ready = get_project(app.state.engine, "demo")
    assert ready.setup_state == "ready"
    assert ready.default_model_profile == "balanced"
