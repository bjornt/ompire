"""REST tests covering the `projects` capability's spec scenarios."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from ompire_daemon.registry.tasks import create_task, mark_archived


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


def _create(client: TestClient, auth_headers: dict[str, str], name: str = "ompire") -> dict:
    response = client.post(
        "/api/projects",
        headers=auth_headers,
        json={"name": name, "title": "Ompire", "upstream_url": "https://example.com/ompire.git"},
    )
    assert response.status_code == 201
    return response.json()


def _put_payload(project: dict, **overrides) -> dict:  # noqa: ANN003
    payload = {
        "title": project["title"],
        "upstream_url": project["upstream_url"],
        "fork_url": project["fork_url"],
        "checkout_path": project["checkout_path"],
    }
    payload.update(overrides)
    return payload


def _reference_task(app, tmp_path: Path, name: str, slug: str, archived: bool = False):  # noqa: ANN001, ANN202
    task = create_task(
        app.state.engine,
        project_name=name,
        slug=slug,
        branch=f"ompire/{slug}",
        clone_path=str(tmp_path / "tasks" / slug),
        prompt="fix it",
    )
    if archived:
        task = mark_archived(app.state.engine, task.id)
    return task


def test_rename_unreferenced_project(client: TestClient, auth_headers: dict[str, str], app) -> None:
    project = _create(client, auth_headers)
    queue = app.state.events.subscribe()

    renamed = client.put(
        "/api/projects/ompire",
        headers=auth_headers,
        json=_put_payload(project, new_name="ompire-ng", title="Ompire NG"),
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "ompire-ng"
    assert renamed.json()["title"] == "Ompire NG"

    gone = client.get("/api/projects/ompire", headers=auth_headers)
    assert gone.status_code == 404
    fetched = client.get("/api/projects/ompire-ng", headers=auth_headers)
    assert fetched.status_code == 200

    event = queue.get_nowait()
    assert event.type == "project_renamed"
    assert event.payload["old_name"] == "ompire"
    assert event.payload["project"]["name"] == "ompire-ng"
    app.state.events.unsubscribe(queue)


def test_plain_update_emits_project_updated_not_renamed(
    client: TestClient, auth_headers: dict[str, str], app
) -> None:
    project = _create(client, auth_headers)
    queue = app.state.events.subscribe()

    updated = client.put(
        "/api/projects/ompire",
        headers=auth_headers,
        json=_put_payload(project, title="Ompire, renamed title only"),
    )
    assert updated.status_code == 200

    event = queue.get_nowait()
    assert event.type == "project_updated"
    assert event.payload["name"] == "ompire"
    app.state.events.unsubscribe(queue)


def test_rename_blocked_by_referencing_task(
    client: TestClient, auth_headers: dict[str, str], app, tmp_path: Path
) -> None:
    project = _create(client, auth_headers)
    _reference_task(app, tmp_path, "ompire", "fix-bug")

    response = client.put(
        "/api/projects/ompire",
        headers=auth_headers,
        json=_put_payload(project, new_name="ompire-ng"),
    )
    assert response.status_code == 409
    assert "ompire/fix-bug" in response.json()["detail"]

    fetched = client.get("/api/projects/ompire", headers=auth_headers)
    assert fetched.status_code == 200


def test_rename_blocked_by_archived_task(
    client: TestClient, auth_headers: dict[str, str], app, tmp_path: Path
) -> None:
    project = _create(client, auth_headers)
    _reference_task(app, tmp_path, "ompire", "old-bug", archived=True)

    response = client.put(
        "/api/projects/ompire",
        headers=auth_headers,
        json=_put_payload(project, new_name="ompire-ng"),
    )
    assert response.status_code == 409
    assert "archived" in response.json()["detail"]


def test_rename_to_existing_name_rejected(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    project = _create(client, auth_headers)
    _create(client, auth_headers, name="taken")

    response = client.put(
        "/api/projects/ompire",
        headers=auth_headers,
        json=_put_payload(project, new_name="taken"),
    )
    assert response.status_code == 409

    listed = client.get("/api/projects", headers=auth_headers)
    assert [p["name"] for p in listed.json()] == ["ompire", "taken"]


def test_rename_to_invalid_slug_rejected(client: TestClient, auth_headers: dict[str, str]) -> None:
    project = _create(client, auth_headers)

    response = client.put(
        "/api/projects/ompire",
        headers=auth_headers,
        json=_put_payload(project, new_name="Not Valid!"),
    )
    assert response.status_code == 422

    fetched = client.get("/api/projects/ompire", headers=auth_headers)
    assert fetched.status_code == 200


# --- Template guards (templates capability; SPEC Decision 6) -----------------


def _create_template(client: TestClient, auth_headers: dict[str, str], project: str) -> dict:
    response = client.post(
        "/api/templates",
        headers=auth_headers,
        json={"name": f"{project}-tpl", "project_name": project},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_project_payloads_carry_no_spawn_defaults(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    project = _create(client, auth_headers)
    assert "base_branch" not in project
    assert "branch_pattern" not in project

    # The PUT payload needs no spawn defaults either.
    updated = client.put(
        "/api/projects/ompire",
        headers=auth_headers,
        json=_put_payload(project, title="New Title"),
    )
    assert updated.status_code == 200
    assert "base_branch" not in updated.json()
    assert "branch_pattern" not in updated.json()


def test_delete_blocked_by_referencing_template(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    _create(client, auth_headers)
    _create_template(client, auth_headers, "ompire")

    response = client.delete("/api/projects/ompire", headers=auth_headers)
    assert response.status_code == 409
    assert "ompire-tpl" in response.json()["detail"]

    # Deleting the template unblocks removal.
    client.delete("/api/templates/ompire-tpl", headers=auth_headers)
    deleted = client.delete("/api/projects/ompire", headers=auth_headers)
    assert deleted.status_code == 200


def test_rename_blocked_by_referencing_template(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    project = _create(client, auth_headers)
    _create_template(client, auth_headers, "ompire")

    response = client.put(
        "/api/projects/ompire",
        headers=auth_headers,
        json=_put_payload(project, new_name="ompire-ng"),
    )
    assert response.status_code == 409
    assert "ompire-tpl" in response.json()["detail"]

    fetched = client.get("/api/projects/ompire", headers=auth_headers)
    assert fetched.status_code == 200


def test_repointing_template_unblocks_project_delete(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    project = _create(client, auth_headers)
    _create(client, auth_headers, name="other")
    template = _create_template(client, auth_headers, "ompire")

    # Repoint the template at the other project.
    repointed = client.put(
        "/api/templates/ompire-tpl",
        headers=auth_headers,
        json={
            "project_name": "other",
            "base_branch": template["base_branch"],
            "branch_pattern": template["branch_pattern"],
            "workflow": template["workflow"],
            "workshop_additions": template["workshop_additions"],
            "model": template["model"],
            "thinking": template["thinking"],
            "preamble": template["preamble"],
        },
    )
    assert repointed.status_code == 200

    deleted = client.delete("/api/projects/ompire", headers=auth_headers)
    assert deleted.status_code == 200
