"""REST tests covering the `tasks` and `task-spawn` capability spec scenarios."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ompire_daemon.app import create_app
from ompire_daemon.config import Config
from ompire_daemon.registry.tasks import (
    ClonePathOutsideRootError,
    clone_path_for,
    create_task,
    get_task,
)


@pytest.fixture
def demo_project(client: TestClient, auth_headers: dict[str, str], git_checkout: Path) -> dict:
    response = client.post(
        "/api/projects",
        headers=auth_headers,
        json={
            "name": "demo",
            "title": "Demo",
            "upstream_url": "https://example.com/demo.git",
            "checkout_path": str(git_checkout),
        },
    )
    assert response.status_code == 201
    return response.json()


def _spawn(client: TestClient, auth_headers: dict, slug: str = "fix-bug") -> dict:
    response = client.post(
        "/api/tasks",
        headers=auth_headers,
        json={"project_name": "demo", "slug": slug, "prompt": "fix it"},
    )
    assert response.status_code == 202, response.text
    return response.json()


def _wait_settled(client: TestClient, auth_headers: dict, task_id: int, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = client.get(f"/api/tasks/{task_id}", headers=auth_headers).json()
        if task["spawn_completed_at"] is not None or task["state"] == "failed":
            return task
        time.sleep(0.05)
    raise AssertionError("spawn pipeline did not settle in time")


def test_spawn_creates_clone_and_branch(
    client: TestClient, auth_headers: dict, demo_project: dict
) -> None:
    task = _spawn(client, auth_headers)
    assert task["state"] == "created"
    assert task["branch"] == "ompire/fix-bug"

    settled = _wait_settled(client, auth_headers, task["id"])
    assert settled["state"] == "created"
    assert (Path(settled["clone_path"]) / ".git").is_dir()


def test_invalid_slug_rejected(client: TestClient, auth_headers: dict, demo_project: dict) -> None:
    for bad in ["Fix-Bug", "../escape", "a/b", "dots.are.bad", "-leading", "x" * 65]:
        response = client.post(
            "/api/tasks",
            headers=auth_headers,
            json={"project_name": "demo", "slug": bad, "prompt": "p"},
        )
        assert response.status_code == 422, bad
    assert client.get("/api/tasks", headers=auth_headers).json() == []


def test_unknown_project_404(client: TestClient, auth_headers: dict) -> None:
    response = client.post(
        "/api/tasks",
        headers=auth_headers,
        json={"project_name": "nope", "slug": "s", "prompt": "p"},
    )
    assert response.status_code == 404


def test_duplicate_live_slug_rejected(
    client: TestClient, auth_headers: dict, demo_project: dict
) -> None:
    first = _spawn(client, auth_headers)
    _wait_settled(client, auth_headers, first["id"])

    duplicate = client.post(
        "/api/tasks",
        headers=auth_headers,
        json={"project_name": "demo", "slug": "fix-bug", "prompt": "again"},
    )
    assert duplicate.status_code == 409


def test_slug_reusable_after_archive(
    client: TestClient, auth_headers: dict, demo_project: dict
) -> None:
    first = _spawn(client, auth_headers)
    _wait_settled(client, auth_headers, first["id"])

    cleanup = client.post(f"/api/tasks/{first['id']}/cleanup", headers=auth_headers)
    assert cleanup.status_code == 200
    assert cleanup.json()["state"] == "archived"

    second = _spawn(client, auth_headers)
    _wait_settled(client, auth_headers, second["id"])


def test_cleanup_deletes_clone_and_is_idempotent(
    client: TestClient, auth_headers: dict, demo_project: dict
) -> None:
    task = _spawn(client, auth_headers)
    settled = _wait_settled(client, auth_headers, task["id"])
    clone = Path(settled["clone_path"])
    assert clone.is_dir()

    first = client.post(f"/api/tasks/{task['id']}/cleanup", headers=auth_headers)
    assert first.status_code == 200
    assert not clone.exists()

    second = client.post(f"/api/tasks/{task['id']}/cleanup", headers=auth_headers)
    assert second.status_code == 200
    assert second.json()["state"] == "archived"


def test_spawn_records_workshop_id(
    client: TestClient, auth_headers: dict, demo_project: dict
) -> None:
    task = _spawn(client, auth_headers)
    settled = _wait_settled(client, auth_headers, task["id"])
    assert settled["workshop_id"] == "ws-test"


def test_detail_reports_workshop_status(
    client: TestClient, auth_headers: dict, demo_project: dict
) -> None:
    task = _spawn(client, auth_headers)
    _wait_settled(client, auth_headers, task["id"])

    detail = client.get(f"/api/tasks/{task['id']}", headers=auth_headers).json()
    # The autouse fake workshop CLI exits 0 for `info`.
    assert detail["workshop_status"] == "present"


def test_cleanup_aborts_when_workshop_remove_fails(
    client: TestClient, auth_headers: dict, demo_project: dict, fake_workshop_cli: Path
) -> None:
    task = _spawn(client, auth_headers)
    settled = _wait_settled(client, auth_headers, task["id"])
    clone = Path(settled["clone_path"])

    fake_workshop_cli.write_text('#!/bin/sh\necho "lxd exploded" >&2\nexit 1\n')
    response = client.post(f"/api/tasks/{task['id']}/cleanup", headers=auth_headers)
    assert response.status_code == 502
    assert "lxd exploded" in response.json()["detail"]
    assert clone.is_dir()
    refreshed = client.get(f"/api/tasks/{task['id']}", headers=auth_headers).json()
    assert refreshed["state"] == "created"

    # Repairing the tool lets cleanup complete.
    fake_workshop_cli.write_text("#!/bin/sh\nexit 0\n")
    retried = client.post(f"/api/tasks/{task['id']}/cleanup", headers=auth_headers)
    assert retried.status_code == 200
    assert retried.json()["state"] == "archived"
    assert not clone.exists()


def test_cleanup_refuses_path_outside_task_root(
    app, client: TestClient, auth_headers: dict, demo_project: dict, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    task = create_task(
        app.state.engine,
        project_name="demo",
        slug="escapee",
        branch="ompire/escapee",
        clone_path=str(outside),
        prompt="p",
    )
    response = client.post(f"/api/tasks/{task.id}/cleanup", headers=auth_headers)
    assert response.status_code == 409
    assert outside.exists()


def test_purge_requires_archived(
    app, client: TestClient, auth_headers: dict, demo_project: dict
) -> None:
    task = _spawn(client, auth_headers)
    _wait_settled(client, auth_headers, task["id"])

    premature = client.delete(f"/api/tasks/{task['id']}", headers=auth_headers)
    assert premature.status_code == 409

    client.post(f"/api/tasks/{task['id']}/cleanup", headers=auth_headers)
    purge = client.delete(f"/api/tasks/{task['id']}", headers=auth_headers)
    assert purge.status_code == 200
    assert client.get(f"/api/tasks/{task['id']}", headers=auth_headers).status_code == 404


def test_project_delete_blocked_until_tasks_purged(
    client: TestClient, auth_headers: dict, demo_project: dict
) -> None:
    task = _spawn(client, auth_headers)
    _wait_settled(client, auth_headers, task["id"])
    client.post(f"/api/tasks/{task['id']}/cleanup", headers=auth_headers)

    blocked = client.delete("/api/projects/demo", headers=auth_headers)
    assert blocked.status_code == 409
    assert "fix-bug" in blocked.json()["detail"]

    client.delete(f"/api/tasks/{task['id']}", headers=auth_headers)
    unblocked = client.delete("/api/projects/demo", headers=auth_headers)
    assert unblocked.status_code == 200


def test_clone_path_confinement_unit() -> None:
    with pytest.raises(ClonePathOutsideRootError):
        clone_path_for(Path("/tmp/tasks"), "..", "..")


def test_reconciliation_on_restart(tmp_path: Path, git_checkout: Path) -> None:
    config = Config(
        data_dir=tmp_path / "data",
        task_dir_root=tmp_path / "tasks",
        checkout_root=tmp_path / "proj",
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
        # A task whose pipeline never completed, as if the daemon died mid-spawn.
        interrupted = create_task(
            app.state.engine,
            project_name="demo",
            slug="interrupted",
            branch="ompire/interrupted",
            clone_path=str(tmp_path / "tasks" / "demo" / "interrupted"),
            prompt="p",
        )
    app.state.engine.dispose()

    restarted = create_app(config, frontend_dist=tmp_path / "no-dist")
    task = get_task(restarted.state.engine, interrupted.id)
    assert task.state == "failed"
    assert "restarted" in (task.error or "")
