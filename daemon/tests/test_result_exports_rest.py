"""REST tests for checkout export.

The behavior itself lives in `test_result_exports.py`. What is covered here is
what a route can get wrong on its own: the task and revision scope in the path,
the status code each refusal picks, the explicit acknowledgement, and the fact
that a preview response is never cached.

The destination is always the fixture's throwaway `git_checkout`.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import spawn_task


def _spawn(client: TestClient, auth_headers: dict, slug: str = "explore") -> dict:
    response = spawn_task(
        client, auth_headers, slug=slug, prompt="explore", workflow_name="plain"
    )
    assert response.status_code == 202, response.text
    task = response.json()
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        current = client.get(f"/api/tasks/{task['id']}", headers=auth_headers).json()
        settled = (
            current["spawn_completed_at"] is not None or current["state"] == "failed"
        )
        idle = current["workflow_status"] in (None, "complete", "failed", "waiting")
        if settled and idle:
            return current
        time.sleep(0.05)
    raise AssertionError("spawn did not settle")


@pytest.fixture
def accepted(client: TestClient, auth_headers: dict, demo_project: dict):
    """A task with one accepted revision holding `epics/demo/PLAN.md`."""
    task = _spawn(client, auth_headers)
    clone = Path(task["clone_path"])
    (clone / "epics" / "demo").mkdir(parents=True, exist_ok=True)
    (clone / "epics" / "demo" / "PLAN.md").write_text("# Plan\nfirst\n")
    response = client.post(
        f"/api/tasks/{task['id']}/results",
        headers=auth_headers,
        json={"paths": ["epics/demo"], "request_id": "req-1"},
    )
    assert response.status_code == 202, response.text
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        listed = client.get(
            f"/api/tasks/{task['id']}/results", headers=auth_headers
        ).json()
        result = listed["results"][0]
        if result["state"] != "capturing":
            break
        time.sleep(0.05)
    assert result["state"] == "ready", result
    accepted = client.post(
        f"/api/tasks/{task['id']}/results/{result['id']}/accept",
        headers=auth_headers,
        json={"expected_manifest_id": result["manifest_id"]},
    )
    assert accepted.status_code == 200, accepted.text
    return task, accepted.json()["results"][0]


def _preview(client: TestClient, auth_headers: dict, task, result, **overrides):
    body = {
        "expected_manifest_id": result["manifest_id"],
        "paths": ["epics/demo/PLAN.md"],
        "prefix": "",
        **overrides,
    }
    return client.post(
        f"/api/tasks/{task['id']}/results/{result['id']}/exports/preview",
        headers=auth_headers,
        json=body,
    )


def _confirm(client: TestClient, auth_headers: dict, task, result, preview, **overrides):
    body = {
        "expected_manifest_id": result["manifest_id"],
        "paths": ["epics/demo/PLAN.md"],
        "prefix": "",
        "preview_token": preview["preview_token"],
        "request_id": "exp-1",
        "acknowledge_export": True,
        **overrides,
    }
    return client.post(
        f"/api/tasks/{task['id']}/results/{result['id']}/exports",
        headers=auth_headers,
        json=body,
    )


def _settled(client: TestClient, auth_headers: dict, task, result, export_id: str):
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        detail = client.get(
            f"/api/tasks/{task['id']}/results/{result['id']}/exports/{export_id}",
            headers=auth_headers,
        ).json()
        if detail["export"]["state"] != "running":
            return detail
        time.sleep(0.05)
    raise AssertionError("export did not settle")


# --- Authorization and scope ------------------------------------------------


def test_export_routes_require_authentication(client: TestClient, accepted) -> None:
    task, result = accepted
    base = f"/api/tasks/{task['id']}/results/{result['id']}/exports"

    assert client.post(f"{base}/preview", json={}).status_code == 401
    assert client.post(base, json={}).status_code == 401
    assert client.get(f"{base}/exp_nope").status_code == 401


def test_an_export_addressed_under_the_wrong_task_is_not_found(
    client: TestClient, auth_headers: dict, accepted, demo_project: dict
) -> None:
    task, result = accepted
    preview = _preview(client, auth_headers, task, result).json()
    started = _confirm(client, auth_headers, task, result, preview)
    assert started.status_code == 202, started.text
    export_id = started.json()["export_id"]
    other = _spawn(client, auth_headers, slug="other")

    response = client.get(
        f"/api/tasks/{other['id']}/results/{result['id']}/exports/{export_id}",
        headers=auth_headers,
    )

    assert response.status_code == 404


def test_an_export_addressed_under_the_wrong_revision_is_not_found(
    client: TestClient, auth_headers: dict, accepted
) -> None:
    """The revision in the path identifies what was exported. A real export id
    under someone else's revision is still not that revision's history."""
    task, result = accepted
    preview = _preview(client, auth_headers, task, result).json()
    export_id = _confirm(client, auth_headers, task, result, preview).json()["export_id"]

    response = client.get(
        f"/api/tasks/{task['id']}/results/res_other/exports/{export_id}",
        headers=auth_headers,
    )

    assert response.status_code == 404


# --- Request validation -----------------------------------------------------


def test_an_unsafe_prefix_or_unknown_path_is_422(
    client: TestClient, auth_headers: dict, accepted
) -> None:
    task, result = accepted

    assert _preview(
        client, auth_headers, task, result, prefix="../escape"
    ).status_code == 422
    assert _preview(
        client, auth_headers, task, result, paths=["epics/demo/NOPE.md"]
    ).status_code == 422
    assert _preview(client, auth_headers, task, result, paths=[]).status_code == 422


def test_confirmation_requires_the_explicit_acknowledgement(
    client: TestClient, auth_headers: dict, accepted
) -> None:
    task, result = accepted
    preview = _preview(client, auth_headers, task, result).json()

    response = _confirm(
        client, auth_headers, task, result, preview, acknowledge_export=False
    )

    assert response.status_code == 422
    assert "confirm the reviewed preview" in response.json()["detail"]


def test_a_stale_preview_token_is_409(
    client: TestClient, auth_headers: dict, accepted
) -> None:
    task, result = accepted
    preview = _preview(client, auth_headers, task, result).json()

    response = _confirm(
        client,
        auth_headers,
        task,
        result,
        {**preview, "preview_token": "0" * 64},
    )

    assert response.status_code == 409
    assert "preview again" in response.json()["detail"]


def test_a_repeated_request_id_with_a_different_confirmation_is_409(
    client: TestClient, auth_headers: dict, accepted
) -> None:
    task, result = accepted
    preview = _preview(client, auth_headers, task, result).json()
    first = _confirm(client, auth_headers, task, result, preview)
    assert first.status_code == 202, first.text

    response = _confirm(
        client,
        auth_headers,
        task,
        result,
        {**preview, "preview_token": "1" * 64},
    )

    assert response.status_code == 409
    assert "use a new request id" in response.json()["detail"]


# --- The successful shape ---------------------------------------------------


def test_a_preview_is_read_only_and_never_cached(
    client: TestClient, auth_headers: dict, accepted, git_checkout: Path
) -> None:
    task, result = accepted
    before = sorted(str(p.relative_to(git_checkout)) for p in git_checkout.rglob("*"))

    response = _preview(client, auth_headers, task, result)

    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    body = response.json()
    assert body["blocked"] is False
    assert body["preview"]["checkout_path"] == str(git_checkout)
    assert body["preview"]["files"][0]["classification"] == "create"
    assert body["source"]["epics/demo/PLAN.md"] == "# Plan\nfirst\n"
    after = sorted(str(p.relative_to(git_checkout)) for p in git_checkout.rglob("*"))
    assert after == before


def test_a_confirmed_export_installs_the_file_and_appears_in_the_projection(
    client: TestClient, auth_headers: dict, accepted, git_checkout: Path
) -> None:
    task, result = accepted
    preview = _preview(client, auth_headers, task, result).json()

    started = _confirm(client, auth_headers, task, result, preview)

    assert started.status_code == 202, started.text
    export_id = started.json()["export_id"]
    detail = _settled(client, auth_headers, task, result, export_id)
    assert detail["export"]["state"] == "completed"
    assert detail["export"]["created_count"] == 1
    assert (git_checkout / "epics" / "demo" / "PLAN.md").read_text() == "# Plan\nfirst\n"

    listed = client.get(
        f"/api/tasks/{task['id']}/results", headers=auth_headers
    ).json()
    exports = listed["results"][0]["exports"]
    assert [entry["id"] for entry in exports] == [export_id]
    # Metadata only: the projection never carries destination text or diffs.
    assert "diff" not in exports[0]
    assert "source" not in exports[0]


def test_a_replayed_confirmation_returns_the_same_operation(
    client: TestClient, auth_headers: dict, accepted
) -> None:
    task, result = accepted
    preview = _preview(client, auth_headers, task, result).json()
    first = _confirm(client, auth_headers, task, result, preview).json()["export_id"]
    _settled(client, auth_headers, task, result, first)

    again = _confirm(client, auth_headers, task, result, preview)

    assert again.status_code == 202
    assert again.json()["export_id"] == first
    listed = client.get(
        f"/api/tasks/{task['id']}/results", headers=auth_headers
    ).json()
    assert len(listed["results"][0]["exports"]) == 1


def test_reconcile_refuses_a_stale_projection_version(
    client: TestClient, auth_headers: dict, accepted
) -> None:
    task, result = accepted
    preview = _preview(client, auth_headers, task, result).json()
    export_id = _confirm(client, auth_headers, task, result, preview).json()["export_id"]
    _settled(client, auth_headers, task, result, export_id)

    response = client.post(
        f"/api/tasks/{task['id']}/results/{result['id']}/exports/{export_id}/reconcile",
        headers=auth_headers,
        json={"expected_version": 0},
    )

    assert response.status_code == 409
    assert "reload before reconciling" in response.json()["detail"]


def test_acknowledging_a_settled_export_is_refused(
    client: TestClient, auth_headers: dict, accepted
) -> None:
    """Acknowledgement exists for uncertainty. A completed export has none, and
    saying otherwise would put a decision on a record that never needed one."""
    task, result = accepted
    preview = _preview(client, auth_headers, task, result).json()
    export_id = _confirm(client, auth_headers, task, result, preview).json()["export_id"]
    detail = _settled(client, auth_headers, task, result, export_id)

    response = client.post(
        f"/api/tasks/{task['id']}/results/{result['id']}/exports/{export_id}/acknowledge",
        headers=auth_headers,
        json={
            "expected_version": detail["version"],
            "acknowledge_unknown_outcome": True,
        },
    )

    assert response.status_code == 409
    assert "only an unresolved export can be acknowledged" in response.json()["detail"]


def test_acknowledgement_requires_the_explicit_flag(
    client: TestClient, auth_headers: dict, accepted
) -> None:
    task, result = accepted
    preview = _preview(client, auth_headers, task, result).json()
    export_id = _confirm(client, auth_headers, task, result, preview).json()["export_id"]
    detail = _settled(client, auth_headers, task, result, export_id)

    response = client.post(
        f"/api/tasks/{task['id']}/results/{result['id']}/exports/{export_id}/acknowledge",
        headers=auth_headers,
        json={
            "expected_version": detail["version"],
            "acknowledge_unknown_outcome": False,
        },
    )

    assert response.status_code == 422
