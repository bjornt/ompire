"""REST and WebSocket tests for durable task results.

Covering the authorization and state boundaries a route can get wrong: the task
scope in the path, the status code each refusal picks, download headers, and
what a reconnecting client sees. The capture and retention behaviors themselves
are exercised in `test_results_capture.py` and `test_results_retention.py`.
"""

from __future__ import annotations

import io
import json
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ompire_daemon.registry.results import RESERVED_MANIFEST_NAME
from tests.conftest import spawn_task


def _spawn(client: TestClient, auth_headers: dict, slug: str = "explore") -> dict:
    response = spawn_task(
        client, auth_headers, slug=slug, prompt="explore", workflow_name="plain"
    )
    assert response.status_code == 202, response.text
    task = response.json()
    # Wait for the *run* to settle, not just the spawn: a workflow step owns
    # the workspace while its agent works, and capture deliberately refuses to
    # interrupt that (it is not a second writer's business to stop the first).
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        current = client.get(
            f"/api/tasks/{task['id']}", headers=auth_headers
        ).json()
        settled = current["spawn_completed_at"] is not None or current["state"] == "failed"
        idle = current["workflow_status"] in (None, "complete", "failed", "waiting")
        if settled and idle:
            return current
        time.sleep(0.05)
    raise AssertionError("spawn did not settle")


def _plan_files(task: dict) -> Path:
    clone = Path(task["clone_path"])
    (clone / "epics" / "demo").mkdir(parents=True, exist_ok=True)
    (clone / "epics" / "demo" / "PLAN.md").write_text("# Plan\nfirst\n")
    return clone


def _capture(
    client: TestClient, auth_headers: dict, task_id: int, **overrides
) -> dict:
    body = {"paths": ["epics/demo"], "request_id": "req-1", **overrides}
    response = client.post(
        f"/api/tasks/{task_id}/results", headers=auth_headers, json=body
    )
    assert response.status_code == 202, response.text
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        listed = client.get(
            f"/api/tasks/{task_id}/results", headers=auth_headers
        ).json()
        latest = listed["results"][0]
        if latest["state"] != "capturing":
            return latest
        time.sleep(0.05)
    raise AssertionError("capture did not settle")


@pytest.fixture
def captured(client: TestClient, auth_headers: dict, demo_project: dict):
    task = _spawn(client, auth_headers)
    _plan_files(task)
    result = _capture(client, auth_headers, task["id"])
    assert result["state"] == "ready", result
    return task, result


# --- Authorization and scope --------------------------------------------------


def test_result_routes_require_authentication(client: TestClient, captured) -> None:
    task, result = captured
    for method, url in (
        ("get", f"/api/tasks/{task['id']}/results"),
        ("get", f"/api/tasks/{task['id']}/results/{result['id']}"),
        ("get", f"/api/tasks/{task['id']}/results/{result['id']}/download"),
    ):
        assert getattr(client, method)(url).status_code == 401


def test_a_result_addressed_under_the_wrong_task_is_not_found(
    client: TestClient, auth_headers: dict, captured, demo_project: dict
) -> None:
    """The task in the path is part of the authorization, not decoration."""
    _task, result = captured
    other = _spawn(client, auth_headers, slug="other")

    response = client.get(
        f"/api/tasks/{other['id']}/results/{result['id']}", headers=auth_headers
    )

    assert response.status_code == 404


def test_unknown_task_and_revision_are_404(
    client: TestClient, auth_headers: dict, captured
) -> None:
    task, _result = captured
    assert client.get("/api/tasks/9999/results", headers=auth_headers).status_code == 404
    assert (
        client.get(
            f"/api/tasks/{task['id']}/results/res_nope", headers=auth_headers
        ).status_code
        == 404
    )


# --- Request validation --------------------------------------------------------


def test_a_malformed_selection_is_422(
    client: TestClient, auth_headers: dict, captured
) -> None:
    task, _result = captured

    for paths in ([], ["../escape"], ["/etc/passwd"], [".git/config"]):
        response = client.post(
            f"/api/tasks/{task['id']}/results",
            headers=auth_headers,
            json={"paths": paths, "request_id": "req-x"},
        )
        assert response.status_code == 422, (paths, response.text)


def test_the_listing_states_the_fixed_limits(
    client: TestClient, auth_headers: dict, captured
) -> None:
    """The UI states them before submission; it does not reproduce them."""
    task, _result = captured

    limits = client.get(
        f"/api/tasks/{task['id']}/results", headers=auth_headers
    ).json()["limits"]

    assert limits["max_files"] == 128
    assert limits["max_total_bytes"] == 8 * 1024 * 1024
    assert ".md" in limits["supported_extensions"]


def test_a_repeated_request_id_with_another_selection_is_409(
    client: TestClient, auth_headers: dict, captured
) -> None:
    task, _result = captured

    response = client.post(
        f"/api/tasks/{task['id']}/results",
        headers=auth_headers,
        json={"paths": ["epics"], "request_id": "req-1"},
    )

    assert response.status_code == 409


# --- Decisions -----------------------------------------------------------------


def test_acceptance_binds_to_the_reviewed_manifest(
    client: TestClient, auth_headers: dict, captured
) -> None:
    task, result = captured

    stale = client.post(
        f"/api/tasks/{task['id']}/results/{result['id']}/accept",
        headers=auth_headers,
        json={"expected_manifest_id": "not-the-reviewed-one"},
    )
    assert stale.status_code == 409

    accepted = client.post(
        f"/api/tasks/{task['id']}/results/{result['id']}/accept",
        headers=auth_headers,
        json={"expected_manifest_id": result["manifest_id"]},
    )
    assert accepted.status_code == 200
    assert accepted.json()["results"][0]["accepted_by"] == "operator"


def test_acceptance_changes_no_workflow_or_publication_state(
    client: TestClient, auth_headers: dict, captured
) -> None:
    task, result = captured
    before = client.get(f"/api/tasks/{task['id']}", headers=auth_headers).json()
    ship_before = client.get(f"/api/tasks/{task['id']}/ship", headers=auth_headers).json()

    client.post(
        f"/api/tasks/{task['id']}/results/{result['id']}/accept",
        headers=auth_headers,
        json={"expected_manifest_id": result["manifest_id"]},
    )

    after = client.get(f"/api/tasks/{task['id']}", headers=auth_headers).json()
    ship_after = client.get(f"/api/tasks/{task['id']}/ship", headers=auth_headers).json()
    assert after["workflow_status"] == before["workflow_status"]
    assert after["workflow_step"] == before["workflow_step"]
    assert ship_after == ship_before


def test_purge_requires_an_explicit_acknowledgement(
    client: TestClient, auth_headers: dict, captured
) -> None:
    task, result = captured
    listing = client.get(
        f"/api/tasks/{task['id']}/results", headers=auth_headers
    ).json()

    response = client.request(
        "DELETE",
        f"/api/tasks/{task['id']}/results/{result['id']}",
        headers=auth_headers,
        json={
            "expected_manifest_id": result["manifest_id"],
            "expected_version": listing["version"],
            "acknowledge_purge": False,
        },
    )

    assert response.status_code == 422
    assert "confirm" in response.json()["detail"]


def test_purge_refuses_a_stale_version_then_succeeds(
    client: TestClient, auth_headers: dict, captured
) -> None:
    task, result = captured
    listing = client.get(
        f"/api/tasks/{task['id']}/results", headers=auth_headers
    ).json()

    stale = client.request(
        "DELETE",
        f"/api/tasks/{task['id']}/results/{result['id']}",
        headers=auth_headers,
        json={
            "expected_manifest_id": result["manifest_id"],
            "expected_version": listing["version"] + 5,
            "acknowledge_purge": True,
        },
    )
    assert stale.status_code == 409

    purged = client.request(
        "DELETE",
        f"/api/tasks/{task['id']}/results/{result['id']}",
        headers=auth_headers,
        json={
            "expected_manifest_id": result["manifest_id"],
            "expected_version": listing["version"],
            "acknowledge_purge": True,
        },
    )
    assert purged.status_code == 200
    assert purged.json()["results"][0]["state"] == "purged"


def test_reading_a_purged_revision_is_410(
    client: TestClient, auth_headers: dict, captured
) -> None:
    """Gone, permanently, with a record still behind it — not simply missing."""
    task, result = captured
    listing = client.get(
        f"/api/tasks/{task['id']}/results", headers=auth_headers
    ).json()
    client.request(
        "DELETE",
        f"/api/tasks/{task['id']}/results/{result['id']}",
        headers=auth_headers,
        json={
            "expected_manifest_id": result["manifest_id"],
            "expected_version": listing["version"],
            "acknowledge_purge": True,
        },
    )

    assert (
        client.get(
            f"/api/tasks/{task['id']}/results/{result['id']}/download",
            headers=auth_headers,
        ).status_code
        == 410
    )
    # The tombstone itself stays readable.
    detail = client.get(
        f"/api/tasks/{task['id']}/results/{result['id']}", headers=auth_headers
    )
    assert detail.status_code == 200
    assert detail.json()["result"]["purged_at"] is not None


# --- Content and download --------------------------------------------------------


def test_file_preview_returns_escaped_source_as_json(
    client: TestClient, auth_headers: dict, captured
) -> None:
    task, result = captured

    response = client.get(
        f"/api/tasks/{task['id']}/results/{result['id']}/file",
        headers=auth_headers,
        params={"path": "epics/demo/PLAN.md"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["text"] == "# Plan\nfirst\n"
    assert body["media_type"] == "text/markdown"
    # JSON, so nothing invites a browser to render agent-authored Markdown.
    assert response.headers["content-type"].startswith("application/json")


def test_download_carries_safe_attachment_headers(
    client: TestClient, auth_headers: dict, captured
) -> None:
    task, result = captured

    response = client.get(
        f"/api/tasks/{task['id']}/results/{result['id']}/download",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["content-disposition"].startswith("attachment;")
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert "epics/demo/PLAN.md" in archive.namelist()
        assert archive.read("epics/demo/PLAN.md") == b"# Plan\nfirst\n"
        manifest = json.loads(archive.read(RESERVED_MANIFEST_NAME))
        assert manifest["result_id"] == result["id"]


def test_single_file_download_is_an_attachment(
    client: TestClient, auth_headers: dict, captured
) -> None:
    task, result = captured

    response = client.get(
        f"/api/tasks/{task['id']}/results/{result['id']}/download",
        headers=auth_headers,
        params={"path": "epics/demo/PLAN.md"},
    )

    assert response.content == b"# Plan\nfirst\n"
    assert 'filename="PLAN.md"' in response.headers["content-disposition"]


def test_an_unknown_file_in_a_revision_is_404(
    client: TestClient, auth_headers: dict, captured
) -> None:
    task, result = captured

    response = client.get(
        f"/api/tasks/{task['id']}/results/{result['id']}/file",
        headers=auth_headers,
        params={"path": "epics/demo/NOPE.md"},
    )

    assert response.status_code == 404


# --- Cleanup and task purge --------------------------------------------------------


def test_cleanup_keeps_results_and_leaves_the_task_reachable(
    client: TestClient, auth_headers: dict, captured
) -> None:
    task, result = captured
    client.post(
        f"/api/tasks/{task['id']}/results/{result['id']}/accept",
        headers=auth_headers,
        json={"expected_manifest_id": result["manifest_id"]},
    )

    cleanup = client.post(f"/api/tasks/{task['id']}/cleanup", headers=auth_headers)

    assert cleanup.status_code == 200
    assert cleanup.json()["state"] == "archived"
    assert not Path(task["clone_path"]).exists()
    # The accepted revision reads and downloads exactly as before.
    listed = client.get(
        f"/api/tasks/{task['id']}/results", headers=auth_headers
    ).json()
    assert listed["results"][0]["accepted_at"] is not None
    download = client.get(
        f"/api/tasks/{task['id']}/results/{result['id']}/download",
        headers=auth_headers,
        params={"path": "epics/demo/PLAN.md"},
    )
    assert download.content == b"# Plan\nfirst\n"


def test_capture_is_unavailable_after_cleanup(
    client: TestClient, auth_headers: dict, captured
) -> None:
    task, _result = captured
    client.post(f"/api/tasks/{task['id']}/cleanup", headers=auth_headers)

    response = client.post(
        f"/api/tasks/{task['id']}/results",
        headers=auth_headers,
        json={"paths": ["epics/demo"], "request_id": "req-2"},
    )

    assert response.status_code == 409


def test_cleanup_is_refused_while_a_capture_owns_the_workspace(
    client: TestClient, auth_headers: dict, demo_project: dict
) -> None:
    task = _spawn(client, auth_headers)
    _plan_files(task)
    client.app.state.workspace_guard.acquire(task["id"], "result-capture")

    response = client.post(f"/api/tasks/{task['id']}/cleanup", headers=auth_headers)

    assert response.status_code == 409
    assert "result-capture" in response.json()["detail"]


def test_task_purge_refuses_while_a_result_is_retained(
    client: TestClient, auth_headers: dict, captured
) -> None:
    task, result = captured
    client.post(f"/api/tasks/{task['id']}/cleanup", headers=auth_headers)

    response = client.request(
        "DELETE", f"/api/tasks/{task['id']}", headers=auth_headers
    )

    assert response.status_code == 409
    assert result["id"] in response.json()["detail"]
    # Nothing was deleted on the way to the refusal: the task and its history
    # are exactly as they were.
    assert client.get(f"/api/tasks/{task['id']}", headers=auth_headers).status_code == 200
    assert (
        client.get(f"/api/tasks/{task['id']}/results", headers=auth_headers)
        .json()["results"][0]["state"]
        == "ready"
    )


def test_task_purge_succeeds_once_results_are_explicitly_purged(
    client: TestClient, auth_headers: dict, captured
) -> None:
    task, result = captured
    listing = client.get(
        f"/api/tasks/{task['id']}/results", headers=auth_headers
    ).json()
    client.request(
        "DELETE",
        f"/api/tasks/{task['id']}/results/{result['id']}",
        headers=auth_headers,
        json={
            "expected_manifest_id": result["manifest_id"],
            "expected_version": listing["version"],
            "acknowledge_purge": True,
        },
    )
    client.post(f"/api/tasks/{task['id']}/cleanup", headers=auth_headers)

    response = client.request(
        "DELETE", f"/api/tasks/{task['id']}", headers=auth_headers
    )

    assert response.status_code == 200
    assert client.get(f"/api/tasks/{task['id']}", headers=auth_headers).status_code == 404


# --- Reconnectable state -----------------------------------------------------------


def test_snapshot_carries_metadata_only_result_projections(
    client: TestClient, auth_headers: dict, auth_token: str, captured
) -> None:
    task, result = captured

    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        payload = ws.receive_json()["payload"]

    projection = payload["task_results"][str(task["id"])]
    assert projection["results"][0]["id"] == result["id"]
    assert projection["results"][0]["files"][0]["path"] == "epics/demo/PLAN.md"
    # Content is fetched per selected revision, never broadcast: the file
    # entry carries its description only.
    assert set(projection["results"][0]["files"][0]) == {
        "path",
        "length",
        "sha256",
        "media_type",
    }
    assert "# Plan" not in json.dumps(payload["task_results"])
    assert payload["retained_results"][str(task["id"])]["retained"] == 1


def test_a_decision_is_broadcast_as_a_versioned_whole_document(
    client: TestClient, auth_headers: dict, auth_token: str, captured
) -> None:
    task, result = captured

    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        snapshot = ws.receive_json()["payload"]
        before = snapshot["task_results"][str(task["id"])]["version"]
        client.post(
            f"/api/tasks/{task['id']}/results/{result['id']}/accept",
            headers=auth_headers,
            json={"expected_manifest_id": result["manifest_id"]},
        )
        event = ws.receive_json()

    assert event["type"] == "task_results_updated"
    assert event["payload"]["version"] > before
    assert event["payload"]["results"][0]["accepted_at"] is not None


def test_results_are_cleared_when_the_task_is_deleted(
    client: TestClient, auth_headers: dict, auth_token: str, captured
) -> None:
    task, result = captured
    listing = client.get(
        f"/api/tasks/{task['id']}/results", headers=auth_headers
    ).json()
    client.request(
        "DELETE",
        f"/api/tasks/{task['id']}/results/{result['id']}",
        headers=auth_headers,
        json={
            "expected_manifest_id": result["manifest_id"],
            "expected_version": listing["version"],
            "acknowledge_purge": True,
        },
    )
    client.post(f"/api/tasks/{task['id']}/cleanup", headers=auth_headers)
    client.request("DELETE", f"/api/tasks/{task['id']}", headers=auth_headers)

    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        payload = ws.receive_json()["payload"]

    assert str(task["id"]) not in payload["task_results"]
