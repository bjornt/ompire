"""REST tests covering the `templates` capability's spec scenarios:
registry entity (CRUD, validation, defaults, events) and guarded removal."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from ompire_daemon.registry.tasks import create_task, mark_archived


def _create_project(client: TestClient, auth_headers: dict[str, str], name: str = "demo") -> dict:
    response = client.post(
        "/api/projects",
        headers=auth_headers,
        json={"name": name, "title": name.title(), "upstream_url": f"https://example.com/{name}.git"},
    )
    assert response.status_code == 201
    return response.json()


def _create_template(
    client: TestClient, auth_headers: dict[str, str], name: str = "demo", **overrides
) -> dict:
    payload = {"name": name, "project_name": "demo"}
    payload.update(overrides)
    response = client.post("/api/templates", headers=auth_headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _put_payload(template: dict, **overrides) -> dict:  # noqa: ANN003
    payload = {
        "project_name": template["project_name"],
        "base_branch": template["base_branch"],
        "branch_pattern": template["branch_pattern"],
        "workflow": template["workflow"],
        "workshop_additions": template["workshop_additions"],
        "model": template["model"],
        "thinking": template["thinking"],
        "preamble": template["preamble"],
    }
    payload.update(overrides)
    return payload


# --- Template registry entity ----------------------------------------------


def test_create_and_list(client: TestClient, auth_headers: dict[str, str], app) -> None:
    _create_project(client, auth_headers)
    queue = app.state.events.subscribe()

    template = _create_template(client, auth_headers)
    assert template["name"] == "demo"
    assert template["project_name"] == "demo"

    listed = client.get("/api/templates", headers=auth_headers)
    assert listed.status_code == 200
    assert [t["name"] for t in listed.json()] == ["demo"]

    event = queue.get_nowait()
    assert event.type == "template_created"
    assert event.payload["name"] == "demo"
    app.state.events.unsubscribe(queue)


def test_defaults_applied_on_create(
    client: TestClient, auth_headers: dict[str, str], daemon_config
) -> None:
    _create_project(client, auth_headers)

    template = _create_template(client, auth_headers)
    assert template["base_branch"] == "main"
    assert template["branch_pattern"] == daemon_config.default_branch_pattern
    assert template["workflow"] == "single-step"
    assert template["workshop_additions"] == "project"
    assert template["model"] is None
    assert template["thinking"] is None
    assert template["preamble"] == ""

    # GET reads return the same defaults.
    fetched = client.get("/api/templates/demo", headers=auth_headers)
    assert fetched.status_code == 200
    assert fetched.json() == template


def test_full_payload_round_trip(client: TestClient, auth_headers: dict[str, str]) -> None:
    _create_project(client, auth_headers)

    template = _create_template(
        client,
        auth_headers,
        base_branch="trunk",
        branch_pattern="feat/<slug>",
        workshop_additions="global",
        model="fable-5",
        thinking="high",
        preamble="You are on team omega.",
    )
    assert template["base_branch"] == "trunk"
    assert template["branch_pattern"] == "feat/<slug>"
    assert template["workshop_additions"] == "global"
    assert template["model"] == "fable-5"
    assert template["thinking"] == "high"
    assert template["preamble"] == "You are on team omega."
    assert template["created_at"] and template["updated_at"]


def test_duplicate_name_rejected(client: TestClient, auth_headers: dict[str, str]) -> None:
    _create_project(client, auth_headers)
    _create_template(client, auth_headers)

    response = client.post(
        "/api/templates",
        headers=auth_headers,
        json={"name": "demo", "project_name": "demo"},
    )
    assert response.status_code == 409

    listed = client.get("/api/templates", headers=auth_headers)
    assert len(listed.json()) == 1


def test_invalid_name_rejected(client: TestClient, auth_headers: dict[str, str]) -> None:
    _create_project(client, auth_headers)
    for bad in ["Not Valid!", "UPPER", "-leading", "dots.are.bad", "a/b"]:
        response = client.post(
            "/api/templates",
            headers=auth_headers,
            json={"name": bad, "project_name": "demo"},
        )
        assert response.status_code == 422, bad

    assert client.get("/api/templates", headers=auth_headers).json() == []


def test_unknown_project_rejected_on_create(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.post(
        "/api/templates",
        headers=auth_headers,
        json={"name": "demo", "project_name": "nope"},
    )
    assert response.status_code == 422
    assert "nope" in response.json()["detail"]

    assert client.get("/api/templates", headers=auth_headers).json() == []


def test_unknown_project_rejected_on_update(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    _create_project(client, auth_headers)
    template = _create_template(client, auth_headers)

    response = client.put(
        "/api/templates/demo",
        headers=auth_headers,
        json=_put_payload(template, project_name="nope"),
    )
    assert response.status_code == 422
    assert "nope" in response.json()["detail"]

    fetched = client.get("/api/templates/demo", headers=auth_headers)
    assert fetched.json()["project_name"] == "demo"


def test_invalid_branch_patterns_rejected(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    _create_project(client, auth_headers)
    for bad in ["no-placeholder", "<slug>+<slug>", "bad char/<slug>", "br@nch/<slug>"]:
        response = client.post(
            "/api/templates",
            headers=auth_headers,
            json={"name": f"t{len(bad)}x", "project_name": "demo", "branch_pattern": bad},
        )
        assert response.status_code == 422, bad

    # Update path validates too.
    template = _create_template(client, auth_headers)
    response = client.put(
        "/api/templates/demo",
        headers=auth_headers,
        json=_put_payload(template, branch_pattern="no-placeholder"),
    )
    assert response.status_code == 422
    assert client.get("/api/templates/demo", headers=auth_headers).json()["branch_pattern"] == (
        template["branch_pattern"]
    )


def test_unregistered_workflow_rejected(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    _create_project(client, auth_headers)

    response = client.post(
        "/api/templates",
        headers=auth_headers,
        json={"name": "demo", "project_name": "demo", "workflow": "bugfix"},
    )
    assert response.status_code == 422
    assert "workflow" in response.json()["detail"]

    template = _create_template(client, auth_headers)
    response = client.put(
        "/api/templates/demo",
        headers=auth_headers,
        json=_put_payload(template, workflow="bugfix"),
    )
    assert response.status_code == 422


def test_thinking_outside_vocabulary_rejected(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    _create_project(client, auth_headers)

    response = client.post(
        "/api/templates",
        headers=auth_headers,
        json={"name": "demo", "project_name": "demo", "thinking": "galaxy"},
    )
    assert response.status_code == 422

    template = _create_template(client, auth_headers)
    response = client.put(
        "/api/templates/demo",
        headers=auth_headers,
        json=_put_payload(template, thinking="galaxy"),
    )
    assert response.status_code == 422

    # Every omp thinking level is accepted.
    for level in ("off", "minimal", "low", "medium", "high", "xhigh", "max", "auto"):
        ok = client.put(
            "/api/templates/demo",
            headers=auth_headers,
            json=_put_payload(template, thinking=level),
        )
        assert ok.status_code == 200, level


def test_invalid_workshop_additions_rejected(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    _create_project(client, auth_headers)

    response = client.post(
        "/api/templates",
        headers=auth_headers,
        json={"name": "demo", "project_name": "demo", "workshop_additions": "elsewhere"},
    )
    assert response.status_code == 422


def test_update_is_unguarded_and_broadcasts(
    client: TestClient, auth_headers: dict[str, str], app, tmp_path: Path
) -> None:
    """Edits affect only future spawns: a live referencing task does not
    block template update (design D-4)."""
    _create_project(client, auth_headers)
    template = _create_template(client, auth_headers)
    task = create_task(
        app.state.engine,
        project_name="demo",
        template_name="demo",
        slug="fix-bug",
        branch="ompire/fix-bug",
        clone_path=str(tmp_path / "tasks" / "fix-bug"),
        prompt="fix it",
    )
    assert task.template_name == "demo"
    queue = app.state.events.subscribe()

    updated = client.put(
        "/api/templates/demo",
        headers=auth_headers,
        json=_put_payload(template, preamble="New preamble", model="zephyr-9"),
    )
    assert updated.status_code == 200
    assert updated.json()["preamble"] == "New preamble"
    assert updated.json()["model"] == "zephyr-9"

    event = queue.get_nowait()
    assert event.type == "template_updated"
    assert event.payload["preamble"] == "New preamble"
    app.state.events.unsubscribe(queue)


def test_update_unknown_template_404(client: TestClient, auth_headers: dict[str, str]) -> None:
    response = client.put(
        "/api/templates/nope",
        headers=auth_headers,
        json={
            "project_name": "demo",
            "base_branch": "main",
            "branch_pattern": "ompire/<slug>",
            "workflow": "single-step",
            "workshop_additions": "project",
            "preamble": "",
        },
    )
    assert response.status_code == 404


def test_get_unknown_template_404(client: TestClient, auth_headers: dict[str, str]) -> None:
    response = client.get("/api/templates/nope", headers=auth_headers)
    assert response.status_code == 404


def test_templates_routes_require_auth(client: TestClient) -> None:
    assert client.get("/api/templates").status_code == 401


# --- Guarded template removal -----------------------------------------------


def test_delete_unreferenced_template(
    client: TestClient, auth_headers: dict[str, str], app
) -> None:
    _create_project(client, auth_headers)
    _create_template(client, auth_headers)
    queue = app.state.events.subscribe()

    deleted = client.delete("/api/templates/demo", headers=auth_headers)
    assert deleted.status_code == 200

    assert client.get("/api/templates", headers=auth_headers).json() == []
    event = queue.get_nowait()
    assert event.type == "template_deleted"
    assert event.payload == {"name": "demo"}
    app.state.events.unsubscribe(queue)


def test_delete_blocked_by_live_task(
    client: TestClient, auth_headers: dict[str, str], app, tmp_path: Path
) -> None:
    _create_project(client, auth_headers)
    _create_template(client, auth_headers)
    create_task(
        app.state.engine,
        project_name="demo",
        template_name="demo",
        slug="fix-bug",
        branch="ompire/fix-bug",
        clone_path=str(tmp_path / "tasks" / "fix-bug"),
        prompt="fix it",
    )

    response = client.delete("/api/templates/demo", headers=auth_headers)
    assert response.status_code == 409
    assert "demo/fix-bug" in response.json()["detail"]
    assert "(created)" in response.json()["detail"]

    # The template is retained.
    assert client.get("/api/templates/demo", headers=auth_headers).status_code == 200


def test_delete_blocked_by_failed_task(
    client: TestClient, auth_headers: dict[str, str], app, tmp_path: Path
) -> None:
    """Any non-archived state blocks — including `failed`."""
    _create_project(client, auth_headers)
    _create_template(client, auth_headers)
    task = create_task(
        app.state.engine,
        project_name="demo",
        template_name="demo",
        slug="fix-bug",
        branch="ompire/fix-bug",
        clone_path=str(tmp_path / "tasks" / "fix-bug"),
        prompt="fix it",
    )
    from ompire_daemon.registry.tasks import mark_failed

    mark_failed(app.state.engine, task.id, "boom")

    response = client.delete("/api/templates/demo", headers=auth_headers)
    assert response.status_code == 409
    assert "(failed)" in response.json()["detail"]


def test_archived_tasks_do_not_block(
    client: TestClient, auth_headers: dict[str, str], app, tmp_path: Path
) -> None:
    _create_project(client, auth_headers)
    _create_template(client, auth_headers)
    task = create_task(
        app.state.engine,
        project_name="demo",
        template_name="demo",
        slug="old-fix",
        branch="ompire/old-fix",
        clone_path=str(tmp_path / "tasks" / "old-fix"),
        prompt="fix it",
    )
    mark_archived(app.state.engine, task.id)

    deleted = client.delete("/api/templates/demo", headers=auth_headers)
    assert deleted.status_code == 200

    # The archived row keeps the name as historical annotation.
    from ompire_daemon.registry.tasks import get_task

    assert get_task(app.state.engine, task.id).template_name == "demo"
    assert client.get("/api/templates", headers=auth_headers).json() == []


def test_delete_unknown_template_404(client: TestClient, auth_headers: dict[str, str]) -> None:
    response = client.delete("/api/templates/nope", headers=auth_headers)
    assert response.status_code == 404
