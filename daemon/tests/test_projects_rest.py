"""REST tests covering the `projects` capability's spec scenarios."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ompire_daemon.registry.tasks import create_task, mark_archived

from .conftest import make_adoptable_checkout


@pytest.fixture(autouse=True)
def _adoptable_checkouts(app) -> None:
    """Give every project name this module registers a real checkout.

    Registration adopts and therefore validates (ADR-0022); these tests are
    about registry and event semantics, not about a missing work tree.
    """
    # Not "demo": the `git_checkout` fixture owns that path.
    for name in ("ompire", "ompire-ng", "taken", "other"):
        make_adoptable_checkout(app.state.config.checkout_root, name)


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


def _put_payload(project: dict, **overrides) -> dict:
    payload = {
        "title": project["title"],
        "upstream_url": project["upstream_url"],
        "fork_url": project["fork_url"],
        "checkout_path": project["checkout_path"],
    }
    payload.update(overrides)
    return payload


def _reference_task(app, tmp_path: Path, name: str, slug: str, archived: bool = False):
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
    _create(client, auth_headers)
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


# --- file search (add-spawn-file-mentions) ----------------------------------


def _register(client: TestClient, auth_headers: dict[str, str], checkout: str) -> None:
    response = client.post(
        "/api/projects",
        headers=auth_headers,
        json={
            "name": "demo",
            "title": "Demo",
            "upstream_url": "https://example.com/demo.git",
            "checkout_path": checkout,
        },
    )
    assert response.status_code == 201


def _register_unchecked(client: TestClient, checkout: str) -> None:
    """Register straight through the registry, skipping adoption validation.

    File search has to answer honestly for a checkout that disappeared *after*
    registration; REST would (correctly) refuse to register one that was
    already broken.
    """
    from ompire_daemon.registry.projects import create_project

    create_project(
        client.app.state.engine,
        name="demo",
        title="Demo",
        upstream_url="https://example.com/demo.git",
        checkout_path=checkout,
        default_checkout_root=Path(checkout).parent,
    )


def test_file_search_requires_the_bearer_token(
    client: TestClient, auth_headers: dict[str, str], git_checkout: Path
) -> None:
    _register(client, auth_headers, str(git_checkout))

    assert client.get("/api/projects/demo/files").status_code == 401


def test_file_search_lists_repository_relative_paths(
    client: TestClient, auth_headers: dict[str, str], git_checkout: Path
) -> None:
    _register(client, auth_headers, str(git_checkout))

    response = client.get("/api/projects/demo/files", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert "README.md" in body["paths"]
    assert body["truncated"] is False
    # Names only: no contents, no absolute paths, nothing under .git.
    assert set(body) == {"paths", "truncated"}
    assert all(not path.startswith("/") for path in body["paths"])
    assert all(not path.startswith(".git/") for path in body["paths"])


def test_file_search_filters_by_query(
    client: TestClient, auth_headers: dict[str, str], git_checkout: Path
) -> None:
    _register(client, auth_headers, str(git_checkout))

    response = client.get("/api/projects/demo/files?q=READ", headers=auth_headers)

    assert response.json()["paths"] == ["README.md"]


def test_file_search_caps_a_client_supplied_limit(
    client: TestClient, auth_headers: dict[str, str], git_checkout: Path
) -> None:
    for index in range(5):
        (git_checkout / f"extra{index}.txt").write_text("x\n")
    _register(client, auth_headers, str(git_checkout))

    response = client.get("/api/projects/demo/files?limit=2", headers=auth_headers)

    body = response.json()
    assert len(body["paths"]) == 2
    assert body["truncated"] is True


def test_file_search_unknown_project_404(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    assert client.get("/api/projects/nope/files", headers=auth_headers).status_code == 404


def test_file_search_missing_checkout_is_409_not_an_empty_list(
    client: TestClient, auth_headers: dict[str, str], tmp_path: Path
) -> None:
    _register_unchecked(client, str(tmp_path / "gone"))

    response = client.get("/api/projects/demo/files", headers=auth_headers)

    assert response.status_code == 409
    assert "does not exist" in response.json()["detail"]


def test_file_search_non_git_checkout_is_409(
    client: TestClient, auth_headers: dict[str, str], tmp_path: Path
) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    _register_unchecked(client, str(plain))

    response = client.get("/api/projects/demo/files", headers=auth_headers)

    assert response.status_code == 409
    assert "not a git repository" in response.json()["detail"]


# --- adoption (ADR-0022) -----------------------------------------------------


def test_adopted_project_reports_its_onboarding_facts(
    client: TestClient, auth_headers: dict[str, str], git_checkout: Path
) -> None:
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

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["checkout_mode"] == "adopted"
    assert body["fetch_remote"] == "origin"
    assert body["setup_state"] == "ready"
    assert body["setup_error"] is None


def test_adopting_a_missing_checkout_is_refused(
    client: TestClient, auth_headers: dict[str, str], tmp_path: Path
) -> None:
    response = client.post(
        "/api/projects",
        headers=auth_headers,
        json={
            "name": "demo",
            "title": "Demo",
            "upstream_url": "https://example.com/demo.git",
            "checkout_path": str(tmp_path / "gone"),
        },
    )

    assert response.status_code == 422
    assert "does not exist" in response.json()["detail"]
    assert client.get("/api/projects", headers=auth_headers).json() == []


def test_adopting_a_checkout_without_the_named_remote_is_refused(
    client: TestClient, auth_headers: dict[str, str], git_checkout: Path
) -> None:
    response = client.post(
        "/api/projects",
        headers=auth_headers,
        json={
            "name": "demo",
            "title": "Demo",
            "upstream_url": "https://example.com/demo.git",
            "checkout_path": str(git_checkout),
            "fetch_remote": "upstream",
        },
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "upstream" in detail and "origin" in detail


def test_adopting_a_checkout_with_a_non_origin_remote_succeeds(
    client: TestClient, auth_headers: dict[str, str], app
) -> None:
    checkout = make_adoptable_checkout(
        app.state.config.checkout_root, "forked", remote="upstream"
    )

    response = client.post(
        "/api/projects",
        headers=auth_headers,
        json={
            "name": "forked",
            "title": "Forked",
            "upstream_url": "https://example.com/forked.git",
            "checkout_path": str(checkout),
            "fetch_remote": "upstream",
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["fetch_remote"] == "upstream"


def test_registration_never_writes_to_the_adopted_checkout(
    client: TestClient, auth_headers: dict[str, str], git_checkout: Path
) -> None:
    import hashlib
    import os

    def fingerprint(root: Path) -> dict:
        out = {}
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                full = Path(dirpath) / name
                try:
                    out[str(full)] = hashlib.sha256(full.read_bytes()).hexdigest()
                except OSError:
                    pass
        return out

    before = fingerprint(git_checkout)
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
    assert fingerprint(git_checkout) == before


@pytest.mark.parametrize(
    "url", ["ext::sh -c touch", "--upload-pack=x", "/etc/passwd", "git://h/r.git"]
)
def test_hostile_upstream_urls_are_refused(
    client: TestClient, auth_headers: dict[str, str], git_checkout: Path, url: str
) -> None:
    response = client.post(
        "/api/projects",
        headers=auth_headers,
        json={
            "name": "demo",
            "title": "Demo",
            "upstream_url": url,
            "checkout_path": str(git_checkout),
        },
    )
    assert response.status_code == 422
    assert client.get("/api/projects", headers=auth_headers).json() == []


def test_invalid_fetch_remote_name_is_refused(
    client: TestClient, auth_headers: dict[str, str], git_checkout: Path
) -> None:
    response = client.post(
        "/api/projects",
        headers=auth_headers,
        json={
            "name": "demo",
            "title": "Demo",
            "upstream_url": "https://example.com/demo.git",
            "checkout_path": str(git_checkout),
            "fetch_remote": "a b",
        },
    )
    assert response.status_code == 422


# --- checkout inspection ------------------------------------------------------


def test_checkout_inspect_reports_remotes_and_suggestions(
    client: TestClient, auth_headers: dict[str, str], git_checkout: Path
) -> None:
    response = client.post(
        "/api/projects/checkout-inspect",
        headers=auth_headers,
        json={"checkout_path": str(git_checkout)},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert [r["name"] for r in body["remotes"]] == ["origin"]
    assert body["suggested_upstream"] == body["remotes"][0]["url"]


def test_checkout_inspect_explains_a_bad_path_without_erroring(
    client: TestClient, auth_headers: dict[str, str], tmp_path: Path
) -> None:
    response = client.post(
        "/api/projects/checkout-inspect",
        headers=auth_headers,
        json={"checkout_path": str(tmp_path / "gone")},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["reason"] == "missing"
    assert "does not exist" in body["detail"]
    assert body["remotes"] == []


# --- guards -------------------------------------------------------------------


def test_delete_is_refused_while_a_clone_is_running(
    client: TestClient, auth_headers: dict[str, str], app
) -> None:
    from ompire_daemon.registry.projects import create_project

    create_project(
        app.state.engine,
        name="cloning-now",
        title="Cloning",
        upstream_url="https://example.com/x.git",
        default_checkout_root=app.state.config.checkout_root,
        checkout_mode="cloned",
        setup_state="cloning",
    )

    response = client.delete("/api/projects/cloning-now", headers=auth_headers)

    assert response.status_code == 409
    assert "still being set up" in response.json()["detail"]


def test_template_cannot_point_at_an_unready_project(
    client: TestClient, auth_headers: dict[str, str], app
) -> None:
    from ompire_daemon.registry.projects import create_project

    create_project(
        app.state.engine,
        name="cloning-now",
        title="Cloning",
        upstream_url="https://example.com/x.git",
        default_checkout_root=app.state.config.checkout_root,
        checkout_mode="cloned",
        setup_state="cloning",
    )

    response = client.post(
        "/api/templates",
        headers=auth_headers,
        json={"name": "t", "project_name": "cloning-now"},
    )

    assert response.status_code == 409
    assert "not ready" in response.json()["detail"]


def test_update_revalidates_a_changed_adopted_checkout(
    client: TestClient, auth_headers: dict[str, str], git_checkout: Path, tmp_path: Path
) -> None:
    project = client.post(
        "/api/projects",
        headers=auth_headers,
        json={
            "name": "demo",
            "title": "Demo",
            "upstream_url": "https://example.com/demo.git",
            "checkout_path": str(git_checkout),
        },
    ).json()

    response = client.put(
        "/api/projects/demo",
        headers=auth_headers,
        json={**_put_payload(project), "checkout_path": str(tmp_path / "gone")},
    )

    assert response.status_code == 422
    stored = client.get("/api/projects/demo", headers=auth_headers).json()
    assert stored["checkout_path"] == str(git_checkout)
