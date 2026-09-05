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
from tests.conftest import launch_body, make_execution_inputs, spawn_task


def _spawn(client: TestClient, auth_headers: dict, slug: str = "fix-bug", **kwargs) -> dict:
    response = spawn_task(client, auth_headers, slug=slug, prompt="fix it", **kwargs)
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
            "/api/tasks/preview", headers=auth_headers, json=launch_body(slug=bad)
        )
        assert response.status_code == 422, bad
    assert client.get("/api/tasks", headers=auth_headers).json() == []


def test_unknown_project_refused_and_creates_nothing(
    client: TestClient, auth_headers: dict
) -> None:
    body = launch_body(project_name="nope", slug="s", prompt="p")
    preview = client.post("/api/tasks/preview", headers=auth_headers, json=body)
    assert preview.status_code == 422
    assert "project_name" in preview.json()["detail"]
    created = client.post(
        "/api/tasks", headers=auth_headers, json={**body, "preview_token": "whatever"}
    )
    assert created.status_code == 422
    assert client.get("/api/tasks", headers=auth_headers).json() == []


def test_unknown_workflow_refused(
    client: TestClient, auth_headers: dict, demo_project: dict
) -> None:
    response = client.post(
        "/api/tasks/preview",
        headers=auth_headers,
        json=launch_body(workflow_name="no-such-workflow"),
    )
    assert response.status_code == 422
    assert "workflow_name" in response.json()["detail"]


def test_accepted_task_pins_the_reviewed_inputs(
    client: TestClient, auth_headers: dict, demo_project: dict
) -> None:
    """The task carries the decision, not a pointer to today's settings."""
    task = _spawn(client, auth_headers)
    assert task["project_name"] == "demo"
    assert task["branch"] == "ompire/fix-bug"
    assert task["needs_configuration"] is False
    inputs = task["execution_inputs"]
    assert inputs["model_profile_name"] == "demo"
    assert inputs["model_profile_source"] == "project"
    work = inputs["step_bindings"]["work"]
    assert work["role"] == "default"
    assert work["role_source"] == "workflow"
    assert work["profile_name"] == "demo"
    assert work["profile_source"] == "project"
    assert work["roles"]["default"] == {
        "model": "testing/main-model",
        "thinking": "medium",
    }
    # The whole native map travels with every consumer, not just its active
    # pair: a `/switch slow` inside the container has to land on the model
    # the operator chose.
    assert work["roles"]["slow"]["model"] == "testing/slow-model"
    judge = inputs["auxiliary_bindings"]["judge"]
    assert judge["role"] == "slow"
    assert judge["roles"]["slow"]["model"] == "testing/slow-model"
    assert inputs["workspace"]["base_branch"] == "main"
    assert inputs["checkout_path"] == demo_project["checkout_path"]
    _wait_settled(client, auth_headers, task["id"])


@pytest.mark.parametrize(
    "stale_field",
    [
        {"template_name": "demo"},
        {"model": "fable-5"},
        {"thinking": "high"},
        # A row override is a profile/role choice, never a concrete model or
        # thinking level: those belong in profile management, not a third
        # override hierarchy (ADR-0027).
        {"step_overrides": {"work": {"model": "fable-5"}}},
        {"step_overrides": {"work": {"thinking": "high"}}},
        {"auxiliary_overrides": {"judge": {"model": "fable-5"}}},
    ],
)
def test_retired_and_future_fields_are_refused_not_ignored(
    client: TestClient, auth_headers: dict, demo_project: dict, stale_field: dict
) -> None:
    """Silently dropping an unknown field would give a caller a launch it did
    not ask for — the old template name and scalar model overrides most of
    all (ADR-0026)."""
    response = client.post(
        "/api/tasks/preview", headers=auth_headers, json={**launch_body(), **stale_field}
    )
    assert response.status_code == 422
    assert "extra_forbidden" in response.text
    assert client.get("/api/tasks", headers=auth_headers).json() == []


@pytest.mark.parametrize(
    ("malformed", "expected"),
    [
        ({"step_overrides": {"work": "other"}}, "model_attributes_type"),
        ({"step_overrides": {"work": ["other"]}}, "model_attributes_type"),
        ({"auxiliary_overrides": {"judge": "other"}}, "model_attributes_type"),
        ({"step_overrides": "work"}, "dict_type"),
    ],
)
def test_malformed_row_overrides_are_refused(
    client: TestClient,
    auth_headers: dict,
    demo_project: dict,
    malformed: dict,
    expected: str,
) -> None:
    """A row override is an object with two optional named choices. A bare
    string is not a shorthand for "use this profile": guessing which
    dimension it meant would pick a model policy the operator never made."""
    response = client.post(
        "/api/tasks/preview", headers=auth_headers, json={**launch_body(), **malformed}
    )
    assert response.status_code == 422
    assert expected in response.text
    assert client.get("/api/tasks", headers=auth_headers).json() == []


def test_preview_and_acceptance_resolve_identically(
    client: TestClient, auth_headers: dict, demo_project: dict
) -> None:
    body = launch_body()
    preview = client.post("/api/tasks/preview", headers=auth_headers, json=body).json()
    accepted = client.post(
        "/api/tasks",
        headers=auth_headers,
        json={**body, "preview_token": preview["preview_token"]},
    ).json()
    inputs = accepted["execution_inputs"]
    assert preview["steps"][0]["binding"] == inputs["step_bindings"]["work"]
    assert preview["steps"][-1]["binding"] == inputs["auxiliary_bindings"]["judge"]
    assert preview["branch"] == inputs["branch"]
    assert preview["workspace"] == inputs["workspace"]
    # Every declared step is described, plus the conditional judge row.
    assert [row["step"] for row in preview["steps"]] == ["work", "judge"]
    assert preview["steps"][0]["model"] == "testing/main-model"
    assert preview["steps"][-1]["kind"] == "judge"
    assert preview["steps"][-1]["conditional"] is True
    _wait_settled(client, auth_headers, accepted["id"])


def test_stale_preview_refuses_creation_and_returns_the_new_resolution(
    client: TestClient, auth_headers: dict, demo_project: dict
) -> None:
    """A configuration change between review and submission is not retried
    under the new settings; the operator reviews again."""
    body = launch_body()
    preview = client.post("/api/tasks/preview", headers=auth_headers, json=body).json()

    changed = client.put(
        "/api/projects/demo",
        headers=auth_headers,
        json={
            "title": "Demo",
            "upstream_url": "https://example.com/demo.git",
            "checkout_path": demo_project["checkout_path"],
            "base_branch": "release",
        },
    )
    assert changed.status_code == 200, changed.text

    response = client.post(
        "/api/tasks",
        headers=auth_headers,
        json={**body, "preview_token": preview["preview_token"]},
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["reason"] == "preview_changed"
    assert detail["preview"]["workspace"]["base_branch"] == "release"
    # Nothing was created under either resolution.
    assert client.get("/api/tasks", headers=auth_headers).json() == []


def test_a_project_without_a_default_profile_needs_a_task_profile(
    client: TestClient, auth_headers: dict, demo_project: dict
) -> None:
    cleared = client.put(
        "/api/projects/demo",
        headers=auth_headers,
        json={
            "title": "Demo",
            "upstream_url": "https://example.com/demo.git",
            "checkout_path": demo_project["checkout_path"],
            "default_model_profile": None,
        },
    )
    assert cleared.status_code == 200, cleared.text

    refused = client.post("/api/tasks/preview", headers=auth_headers, json=launch_body())
    assert refused.status_code == 422
    assert "model_profile" in refused.json()["detail"]

    # An explicit task profile satisfies it; nothing is inferred.
    resolved = client.post(
        "/api/tasks/preview", headers=auth_headers, json=launch_body(model_profile="demo")
    )
    assert resolved.status_code == 200
    assert resolved.json()["model_profile_source"] == "task"


def test_task_profile_replaces_project_inheritance(
    client: TestClient, auth_headers: dict, demo_project: dict
) -> None:
    client.post(
        "/api/model-profiles",
        headers=auth_headers,
        json={
            "name": "other",
            "roles": {
                "default": {"model": "testing/other-model", "thinking": "low"},
                "smol": {"model": "testing/smol-model", "thinking": "low"},
                "slow": {"model": "testing/slow-model", "thinking": "high"},
                "plan": {"model": "testing/plan-model", "thinking": "xhigh"},
            },
        },
    )
    preview = client.post(
        "/api/tasks/preview", headers=auth_headers, json=launch_body(model_profile="other")
    ).json()
    assert preview["model_profile"] == "other"
    assert preview["model_profile_source"] == "task"
    assert preview["project_default_model_profile"] == "demo"
    assert preview["roles"]["default"]["model"] == "testing/other-model"


def test_accepted_inputs_survive_project_and_profile_edits(
    client: TestClient, auth_headers: dict, demo_project: dict
) -> None:
    """Editing reusable defaults changes the next launch, never this task."""
    task = _spawn(client, auth_headers)
    _wait_settled(client, auth_headers, task["id"])

    client.put(
        "/api/model-profiles/demo",
        headers=auth_headers,
        json={
            "roles": {
                "default": {"model": "testing/changed", "thinking": "off"},
                "smol": {"model": "testing/changed", "thinking": "off"},
                "slow": {"model": "testing/changed", "thinking": "off"},
                "plan": {"model": "testing/changed", "thinking": "off"},
            }
        },
    )
    client.put(
        "/api/projects/demo",
        headers=auth_headers,
        json={
            "title": "Demo",
            "upstream_url": "https://example.com/demo.git",
            "checkout_path": demo_project["checkout_path"],
            "base_branch": "release",
            "preamble": "new preamble",
        },
    )

    after = client.get(f"/api/tasks/{task['id']}", headers=auth_headers).json()
    inputs = after["execution_inputs"]
    assert (
        inputs["step_bindings"]["work"]["roles"]["default"]["model"]
        == "testing/main-model"
    )
    assert (
        inputs["auxiliary_bindings"]["judge"]["roles"]["slow"]["model"]
        == "testing/slow-model"
    )
    assert inputs["workspace"]["base_branch"] == "main"
    assert inputs["workspace"]["preamble"] == ""


def test_workspace_overrides_are_task_local_and_reset_by_omission(
    client: TestClient, auth_headers: dict, demo_project: dict
) -> None:
    preview = client.post(
        "/api/tasks/preview",
        headers=auth_headers,
        json=launch_body(
            workspace_overrides={"branch_pattern": "wip/<slug>", "preamble": ""}
        ),
    ).json()
    assert preview["branch"] == "wip/fix-bug"
    assert sorted(preview["workspace_overrides"]) == ["branch_pattern", "preamble"]
    # The project's own defaults are reported beside them, so the form can
    # show what "reset" would restore.
    assert preview["inherited_workspace"]["branch_pattern"] == "ompire/<slug>"

    inherited = client.post(
        "/api/tasks/preview", headers=auth_headers, json=launch_body()
    ).json()
    assert inherited["branch"] == "ompire/fix-bug"
    assert inherited["workspace_overrides"] == []


def test_null_is_not_a_reset_for_a_workspace_override(
    client: TestClient, auth_headers: dict, demo_project: dict
) -> None:
    response = client.post(
        "/api/tasks/preview",
        headers=auth_headers,
        json=launch_body(workspace_overrides={"base_branch": None}),
    )
    assert response.status_code == 422
    assert "null is not a reset" in response.json()["detail"]


def test_workflow_catalog_describes_every_declared_step(
    client: TestClient, auth_headers: dict
) -> None:
    catalog = {w["name"]: w for w in client.get("/api/workflows", headers=auth_headers).json()}
    assert set(catalog) == {"bugfix", "single-step"}
    bugfix = catalog["bugfix"]
    assert [step["name"] for step in bugfix["steps"]] == [
        "reproduce",
        "triage",
        "fix",
        "route-validate",
        "validate-script",
        "validate-agent",
        "check",
        "escalate",
    ]
    # Only agent steps name a role; a command or gate has no model.
    roles = {step["name"]: step["role"] for step in bugfix["steps"]}
    assert roles["reproduce"] == "default"
    assert roles["triage"] is None
    assert roles["validate-script"] is None
    # Everything after the first decision may be routed past.
    conditional = {step["name"]: step["conditional"] for step in bugfix["steps"]}
    assert conditional["reproduce"] is False
    assert conditional["fix"] is True
    assert bugfix["judge_session"] == "judge"
    assert bugfix["judge_role"] == "slow"


def test_duplicate_live_slug_rejected(
    client: TestClient, auth_headers: dict, demo_project: dict
) -> None:
    first = _spawn(client, auth_headers)
    _wait_settled(client, auth_headers, first["id"])

    duplicate = spawn_task(client, auth_headers, slug="fix-bug", prompt="again")
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
        execution_inputs=make_execution_inputs(
            checkout_path=demo_project["checkout_path"], branch="ompire/escapee"
        ),
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


def test_purge_reaches_a_connected_client(
    client: TestClient, auth_token: str, auth_headers: dict, demo_project: dict
) -> None:
    """`purge_task_route` is a synchronous route, so its `task_deleted` goes
    through the hub's cross-thread hand-off like the project mutations do."""
    task = _spawn(client, auth_headers)
    _wait_settled(client, auth_headers, task["id"])

    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        ws.receive_json()  # snapshot

        client.post(f"/api/tasks/{task['id']}/cleanup", headers=auth_headers)
        purge = client.delete(f"/api/tasks/{task['id']}", headers=auth_headers)
        assert purge.status_code == 200

        while True:
            event = ws.receive_json()
            if event["type"] == "task_deleted":
                assert event["payload"] == {"id": task["id"]}
                break


def test_project_delete_blocked_until_tasks_purged(
    client: TestClient, auth_headers: dict, demo_project: dict
) -> None:
    task = _spawn(client, auth_headers)
    _wait_settled(client, auth_headers, task["id"])
    client.post(f"/api/tasks/{task['id']}/cleanup", headers=auth_headers)

    blocked = client.delete("/api/projects/demo", headers=auth_headers)
    assert blocked.status_code == 409
    assert "fix-bug" in blocked.json()["detail"]

    # Purging the archived task is the only thing that unblocks it now:
    # templates are gone, so task history is the sole remaining reference.
    client.delete(f"/api/tasks/{task['id']}", headers=auth_headers)
    unblocked = client.delete("/api/projects/demo", headers=auth_headers)
    assert unblocked.status_code == 200, unblocked.text


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
            execution_inputs=make_execution_inputs(
                checkout_path=str(git_checkout), branch="ompire/interrupted"
            ),
        )
    app.state.engine.dispose()

    restarted = create_app(config, frontend_dist=tmp_path / "no-dist")
    task = get_task(restarted.state.engine, interrupted.id)
    assert task.state == "failed"
    assert "restarted" in (task.error or "")


def test_cleanup_clears_attention_entry(
    client: TestClient, auth_headers: dict, demo_project: dict, app
) -> None:
    """Regression (merge-poll dogfood): the agent exit during workshop removal
    lands `failed` (interrupt tier); cleanup must not leave that attention
    entry behind once the task is gone."""
    task = _spawn(client, auth_headers)
    _wait_settled(client, auth_headers, task["id"])

    # The workflow engine spawns the `main` session lazily after the
    # workspace steps, so wait for it to be tracked before failing it —
    # failing a never-tracked session is a no-op by design.
    deadline = time.monotonic() + 10
    while app.state.sessions.get(task["id"], "main") is None:
        assert time.monotonic() < deadline, "main session never tracked"
        time.sleep(0.05)

    # Drive the session into a notifying tier through the public tracker API.
    app.state.sessions.session_start_failed(task["id"], "main", "boom")
    # The notifier consumes the hub from the app's loop — give it a moment.
    deadline = time.monotonic() + 5
    while task["id"] not in app.state.notifications.snapshot():
        assert time.monotonic() < deadline, "notifier never recorded the entry"
        time.sleep(0.05)

    response = client.post(f"/api/tasks/{task['id']}/cleanup", headers=auth_headers)

    assert response.status_code == 200
    assert task["id"] not in app.state.notifications.snapshot()


# --- prompt file mentions (add-spawn-file-mentions) -------------------------


def _spawn_with_prompt(client: TestClient, auth_headers: dict, prompt: str):
    """Mentions are validated at acceptance, so this goes through the whole
    preview-then-accept path rather than short-circuiting it."""
    body = launch_body(prompt=prompt)
    preview = client.post("/api/tasks/preview", headers=auth_headers, json=body)
    assert preview.status_code == 200, preview.text
    return client.post(
        "/api/tasks",
        headers=auth_headers,
        json={**body, "preview_token": preview.json()["preview_token"]},
    )


def test_mention_of_a_committed_file_is_accepted(
    client: TestClient, auth_headers: dict, demo_project: dict
) -> None:
    response = _spawn_with_prompt(client, auth_headers, "look at @README.md please")

    assert response.status_code == 202
    task = response.json()
    # Stored verbatim: the literal mention is what reaches the agent.
    assert task["prompt"] == "look at @README.md please"
    _wait_settled(client, auth_headers, task["id"])


@pytest.mark.parametrize(
    ("mention", "fragment"),
    [
        ("/etc/passwd", "absolute paths"),
        ("../escape.txt", "'..' is not allowed"),
        ("no-such-file.md", "no such file"),
    ],
)
def test_bad_mention_rejected_before_anything_is_created(
    client: TestClient,
    auth_headers: dict,
    demo_project: dict,
    git_checkout: Path,
    mention: str,
    fragment: str,
) -> None:
    response = _spawn_with_prompt(client, auth_headers, f"read @{mention} now")

    assert response.status_code == 422
    assert fragment in response.json()["detail"]
    # No task row, and therefore no pipeline.
    assert client.get("/api/tasks", headers=auth_headers).json() == []


def test_uncommitted_file_mention_is_refused_with_the_base_branch_reason(
    client: TestClient, auth_headers: dict, demo_project: dict, git_checkout: Path
) -> None:
    (git_checkout / "scratch.md").write_text("not committed\n")

    response = _spawn_with_prompt(client, auth_headers, "see @scratch.md")

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "base branch" in detail
    assert "clone" in detail
    assert client.get("/api/tasks", headers=auth_headers).json() == []


def test_email_address_in_a_prompt_is_not_a_mention(
    client: TestClient, auth_headers: dict, demo_project: dict
) -> None:
    response = _spawn_with_prompt(client, auth_headers, "ask someone@example.com about it")

    assert response.status_code == 202
    _wait_settled(client, auth_headers, response.json()["id"])


def test_every_bad_mention_is_named_in_one_refusal(
    client: TestClient, auth_headers: dict, demo_project: dict
) -> None:
    response = _spawn_with_prompt(client, auth_headers, "see @gone-a.md and @gone-b.md")

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "@gone-a.md" in detail
    assert "@gone-b.md" in detail
