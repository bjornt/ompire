"""REST tests covering the `projects` capability's spec scenarios."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_create_and_list(client: TestClient, auth_headers: dict[str, str]) -> None:
    response = client.post(
        "/api/projects",
        headers=auth_headers,
        json={"name": "ompire", "title": "Ompire", "upstream_url": "https://example.com/ompire.git"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "ompire"
    assert body["fork_url"] is None

    listed = client.get("/api/projects", headers=auth_headers)
    assert listed.status_code == 200
    assert [p["name"] for p in listed.json()] == ["ompire"]


def test_duplicate_name_rejected(client: TestClient, auth_headers: dict[str, str]) -> None:
    payload = {"name": "ompire", "title": "Ompire", "upstream_url": "https://example.com/ompire.git"}
    first = client.post("/api/projects", headers=auth_headers, json=payload)
    assert first.status_code == 201

    second = client.post("/api/projects", headers=auth_headers, json=payload)
    assert second.status_code == 409

    listed = client.get("/api/projects", headers=auth_headers)
    assert len(listed.json()) == 1


def test_invalid_name_rejected(client: TestClient, auth_headers: dict[str, str]) -> None:
    response = client.post(
        "/api/projects",
        headers=auth_headers,
        json={"name": "Not Valid!", "title": "x", "upstream_url": "https://example.com/x.git"},
    )
    assert response.status_code == 422

    listed = client.get("/api/projects", headers=auth_headers)
    assert listed.json() == []


def test_project_without_fork_reads_null(client: TestClient, auth_headers: dict[str, str]) -> None:
    response = client.post(
        "/api/projects",
        headers=auth_headers,
        json={"name": "ompire", "title": "Ompire", "upstream_url": "https://example.com/ompire.git"},
    )
    assert response.json()["fork_url"] is None

    fetched = client.get("/api/projects/ompire", headers=auth_headers)
    assert fetched.json()["fork_url"] is None


def test_delete_unreferenced_project(client: TestClient, auth_headers: dict[str, str]) -> None:
    client.post(
        "/api/projects",
        headers=auth_headers,
        json={"name": "ompire", "title": "Ompire", "upstream_url": "https://example.com/ompire.git"},
    )

    deleted = client.delete("/api/projects/ompire", headers=auth_headers)
    assert deleted.status_code == 200

    listed = client.get("/api/projects", headers=auth_headers)
    assert listed.json() == []


def test_post_body_validation_failure_changes_nothing(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.post("/api/projects", headers=auth_headers, json={"name": "ompire"})
    assert response.status_code == 422
    assert "detail" in response.json()

    listed = client.get("/api/projects", headers=auth_headers)
    assert listed.json() == []


def test_default_checkout_path_derived_from_config(
    client: TestClient, auth_headers: dict[str, str], daemon_config
) -> None:
    response = client.post(
        "/api/projects",
        headers=auth_headers,
        json={"name": "ompire", "title": "Ompire", "upstream_url": "https://example.com/ompire.git"},
    )
    assert response.json()["checkout_path"] == str(daemon_config.checkout_root / "ompire")


def test_projects_routes_require_auth(client: TestClient) -> None:
    response = client.get("/api/projects")
    assert response.status_code == 401
