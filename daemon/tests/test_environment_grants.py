"""Environment access is reviewed, freshly checked, and atomically pinned."""

from __future__ import annotations

import json
import subprocess
from dataclasses import FrozenInstanceError, replace
from functools import partial
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ompire_daemon.application import launch as launch_module
from ompire_daemon.config import Config
from ompire_daemon.environments import EnvironmentDefinition, parse_environments
from ompire_daemon.work.inputs import (
    EnvironmentGrant,
    decode_execution_inputs,
    encode_execution_inputs,
    execution_inputs_document,
)
from ompire_daemon.work.launch import LaunchRequest, PreviewRow, launch_fingerprint
from ompire_daemon.work.tasks import get_task, list_tasks
from ompire_daemon.workflow_definitions import load_definition
from tests.conftest import (
    PLAIN_WORKFLOW_YAML,
    install_test_workflow,
    launch_body,
    make_execution_inputs,
)

WIRING = "plugs:\n  forge:\n    interface: tunnel\n    connect: forge-broker\n"


@pytest.fixture
def environment_files(tmp_path: Path) -> tuple[Path, Path, Path]:
    state = tmp_path / "environment-state"
    calls = tmp_path / "environment-checks"
    check = tmp_path / "check-environment"
    state.write_text("ready")
    check.write_text(
        "#!/bin/sh\n"
        f'printf "check\\n" >>"{calls}"\n'
        f'if [ "$(cat "{state}")" != ready ]; then\n'
        '  printf "forge broker unavailable\\n" >&2\n'
        "  exit 1\nfi\n"
    )
    check.chmod(0o755)
    return check, state, calls


@pytest.fixture
def daemon_config(daemon_config: Config, environment_files: tuple[Path, Path, Path]) -> Config:
    check, _, _ = environment_files
    return replace(
        daemon_config,
        environments=parse_environments({
            "sandbox-qa": {
                "host": "container",
                "capabilities": {"forge": "forge-broker", "model": "auth-gateway"},
                "provision": ["/bin/true"],
                "check": [str(check)],
                "wiring": WIRING,
            },
        }),
    )


@pytest.fixture
def qa_workflow(client: TestClient, demo_project: dict):
    document = PLAIN_WORKFLOW_YAML.replace(
        "name: plain", "name: qa-work\nenvironment: sandbox-qa"
    )
    return install_test_workflow(client.app.state.engine, document)


@pytest.fixture
def scheduled(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Observe the post-commit scheduling seam without running a workspace."""
    submitted: list[bool] = []

    def submit(coro):
        submitted.append(True)
        coro.close()

    monkeypatch.setattr(client.app.state.spawn_scheduler, "submit", submit)
    return submitted


def _body(**extra) -> dict:
    return launch_body(workflow_name="qa-work", **extra)


def _preview(client: TestClient, headers: dict, body: dict) -> dict:
    response = client.post("/api/tasks/preview", headers=headers, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _accept(client: TestClient, headers: dict, body: dict, preview: dict):
    return client.post(
        "/api/tasks", headers=headers,
        json={**body, "preview_token": preview["preview_token"]},
    )


def _assert_no_work(client: TestClient, scheduled: list[bool]) -> None:
    assert list_tasks(client.app.state.engine) == []
    assert scheduled == []
    assert not client.app.state.spawn_scheduler.jobs
    root = client.app.state.launch_service._config.task_dir_root
    assert not root.exists() or not list(root.iterdir())


def _change_environment(client: TestClient, **changes) -> EnvironmentDefinition:
    service = client.app.state.launch_service
    definition = replace(service._config.environments[0], **changes)
    service._config = replace(service._config, environments=(definition,))
    client.app.state.environments._definitions[definition.name] = definition
    return definition


def test_preview_freshly_probes_and_surfaces_not_ready_without_granting(
    client, auth_headers, qa_workflow, environment_files, scheduled,
):
    _, state, calls = environment_files
    state.write_text("degraded")
    before = len(calls.read_text().splitlines())
    body = _body()
    preview = _preview(client, auth_headers, body)
    environment = preview["environment"]
    assert environment == {
        "name": "sandbox-qa",
        "capabilities": {"forge": "forge-broker", "model": "auth-gateway"},
        "grantable": True,
        "status": {
            "state": "degraded",
            "blockers": [{"code": "check-failed", "message": "forge broker unavailable"}],
            "checked_at": environment["status"]["checked_at"],
        },
    }
    assert environment["status"]["checked_at"] is not None
    assert len(calls.read_text().splitlines()) == before + 1
    state.write_text("ready")
    refreshed = _preview(client, auth_headers, body)
    assert refreshed["environment"]["status"]["state"] == "ready"
    assert refreshed["preview_token"] == preview["preview_token"]
    assert len(calls.read_text().splitlines()) == before + 2
    _assert_no_work(client, scheduled)


def test_ordinary_launch_omits_environment_and_never_probes(
    client, auth_headers, demo_project, environment_files, scheduled,
):
    _, _, calls = environment_files
    before = calls.read_text()
    body = launch_body()
    preview = _preview(client, auth_headers, body)
    explicit_false = _preview(client, auth_headers, {**body, "grant_environment": False})
    assert "environment" not in preview
    assert explicit_false["preview_token"] == preview["preview_token"]
    accepted = _accept(client, auth_headers, body, preview)
    assert accepted.status_code == 202, accepted.text
    assert "environment_grant" not in accepted.json()["execution_inputs"]
    assert calls.read_text() == before
    assert scheduled == [True]


def test_unknown_declared_environment_refuses_preview_and_acceptance(
    client, auth_headers, demo_project, environment_files, scheduled,
):
    document = PLAIN_WORKFLOW_YAML.replace(
        "name: plain", "name: elsewhere\nenvironment: unavailable-here"
    )
    install_test_workflow(client.app.state.engine, document)
    body = launch_body(workflow_name="elsewhere", grant_environment=True)
    _, _, calls = environment_files
    before = calls.read_text()
    for route in ("/api/tasks/preview", "/api/tasks"):
        response = client.post(
            route, headers=auth_headers,
            json=body if route.endswith("preview") else {**body, "preview_token": "old"},
        )
        assert response.status_code == 422
        assert "workflow_name" in response.json()["detail"]
        assert "unavailable-here" in response.json()["detail"]
    assert calls.read_text() == before
    _assert_no_work(client, scheduled)


@pytest.mark.parametrize("required", [True, False])
def test_missing_and_unrequested_grants_refuse_without_partial_work(
    client, auth_headers, qa_workflow, required, scheduled,
):
    body = _body() if required else launch_body(grant_environment=True)
    preview = _preview(client, auth_headers, body)
    response = _accept(client, auth_headers, body, preview)
    assert response.status_code == 422
    assert "grant_environment" in response.json()["detail"]
    expected = "sandbox-qa" if required else "no environment"
    assert expected in response.json()["detail"]
    _assert_no_work(client, scheduled)


def test_acceptance_rechecks_status_instead_of_trusting_ready_preview(
    client, auth_headers, qa_workflow, environment_files, scheduled,
):
    body = _body(grant_environment=True)
    preview = _preview(client, auth_headers, body)
    assert preview["environment"]["status"]["state"] == "ready"
    _, state, calls = environment_files
    before = len(calls.read_text().splitlines())
    state.write_text("degraded")
    response = _accept(client, auth_headers, body, preview)
    assert response.status_code == 422
    assert response.json()["detail"] == {
        "environment": "sandbox-qa",
        "blockers": [{"code": "check-failed", "message": "forge broker unavailable"}],
    }
    assert len(calls.read_text().splitlines()) == before + 1
    _assert_no_work(client, scheduled)


def test_repaired_environment_can_accept_the_reviewed_access_snapshot(
    client, auth_headers, qa_workflow, environment_files, scheduled,
):
    _, state, _ = environment_files
    state.write_text("degraded")
    body = _body(grant_environment=True)
    preview = _preview(client, auth_headers, body)
    state.write_text("ready")
    response = _accept(client, auth_headers, body, preview)
    assert response.status_code == 202, response.text
    assert scheduled == [True]


def test_grant_toggle_changes_review_and_cannot_reuse_ungranted_token(
    client, auth_headers, qa_workflow, scheduled,
):
    off = _body()
    on = _body(grant_environment=True)
    off_preview = _preview(client, auth_headers, off)
    on_preview = _preview(client, auth_headers, on)
    assert off_preview["preview_token"] != on_preview["preview_token"]
    response = _accept(client, auth_headers, on, off_preview)
    assert response.status_code == 409
    _assert_no_work(client, scheduled)


@pytest.mark.parametrize("change", [
    {"capabilities": {"forge": "different-broker"}},
    {"wiring": WIRING.replace("forge-broker", "new-broker")},
])
def test_changed_access_snapshot_invalidates_review(
    client, auth_headers, qa_workflow, scheduled, change,
):
    body = _body(grant_environment=True)
    preview = _preview(client, auth_headers, body)
    _change_environment(client, **change)
    response = _accept(client, auth_headers, body, preview)
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "preview_changed"
    _assert_no_work(client, scheduled)


def test_final_transaction_rechecks_access_after_slow_probe(
    client, auth_headers, qa_workflow, monkeypatch, scheduled,
):
    body = _body(grant_environment=True)
    preview = _preview(client, auth_headers, body)
    real_probe = client.app.state.environments.probe

    async def probe_then_edit(name):
        result = await real_probe(name)
        _change_environment(client, capabilities={"forge": "wider-broker", "browser": "chrome"})
        return result

    monkeypatch.setattr(client.app.state.environments, "probe", probe_then_edit)
    response = _accept(client, auth_headers, body, preview)
    assert response.status_code == 409
    _assert_no_work(client, scheduled)


def test_workflow_requirement_change_during_preflight_never_auto_widens_grant(
    client, auth_headers, qa_workflow, monkeypatch, scheduled,
):
    body = _body(grant_environment=True)
    preview = _preview(client, auth_headers, body)
    real_validate = launch_module.validate_mentions

    async def validate_then_edit(*args, **kwargs):
        result = await real_validate(*args, **kwargs)
        install_test_workflow(
            client.app.state.engine,
            PLAIN_WORKFLOW_YAML.replace("name: plain", "name: qa-work"),
        )
        return result

    monkeypatch.setattr(launch_module, "validate_mentions", validate_then_edit)
    response = _accept(client, auth_headers, body, preview)
    assert response.status_code == 409
    _assert_no_work(client, scheduled)


def test_grant_is_atomic_durable_and_exposed_without_live_rederivation(
    client, auth_headers, qa_workflow, scheduled, daemon_config, tmp_path,
):
    from ompire_daemon.app import create_app

    body = _body(grant_environment=True)
    preview = _preview(client, auth_headers, body)
    response = _accept(client, auth_headers, body, preview)
    assert response.status_code == 202, response.text
    payload = response.json()
    inputs = payload["execution_inputs"]
    grant = inputs["environment_grant"]
    assert grant == {
        "name": "sandbox-qa",
        "capabilities": {"forge": "forge-broker", "model": "auth-gateway"},
        "wiring": WIRING,
        "granted_at": inputs["accepted_at"],
        "actor": "operator",
    }
    assert grant["granted_at"] is not None
    stored = get_task(client.app.state.engine, payload["id"])
    assert stored.execution_inputs.environment_grant.name == "sandbox-qa"
    assert execution_inputs_document(stored.execution_inputs)["environment_grant"] == grant
    _change_environment(client, capabilities={"forge": "changed-after-accept"}, wiring=None)
    assert client.get(f"/api/tasks/{payload['id']}", headers=auth_headers).json()["execution_inputs"]["environment_grant"] == grant
    restarted = create_app(replace(daemon_config, environments=()), frontend_dist=tmp_path / "no-dist")
    restored = get_task(restarted.state.engine, payload["id"])
    assert execution_inputs_document(restored.execution_inputs)["environment_grant"] == grant
    restarted.state.engine.dispose()
    assert scheduled == [True]


def test_failure_inside_acceptance_rolls_back_task_and_grant(
    client, auth_headers, qa_workflow, monkeypatch, scheduled,
):
    body = _body(grant_environment=True)
    preview = _preview(client, auth_headers, body)

    def fail(*args, **kwargs):
        raise RuntimeError("transaction failed after task insert")

    monkeypatch.setattr(launch_module, "insert_references_on", fail)
    with pytest.raises(RuntimeError, match="transaction failed"):
        _accept(client, auth_headers, body, preview)
    _assert_no_work(client, scheduled)


@pytest.mark.parametrize("content", [None, b"", b" ", b"# selected additions\n"])
def test_global_additions_conflict_depends_only_on_selected_file_content(
    client, auth_headers, qa_workflow, monkeypatch, tmp_path, content, scheduled,
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "operator-config"))
    source = tmp_path / "operator-config" / "my-workshop" / "my.yaml"
    if content is not None:
        source.parent.mkdir(parents=True)
        source.write_bytes(content)
    body = _body(grant_environment=True, workspace_overrides={"workshop_additions": "global"})
    preview = _preview(client, auth_headers, body)
    response = _accept(client, auth_headers, body, preview)
    if content:
        assert response.status_code == 422
        assert "grant_environment" in response.json()["detail"]
        assert "global Workshop additions" in response.json()["detail"]
        assert "sandbox-qa" in response.json()["detail"]
        _assert_no_work(client, scheduled)
    else:
        assert response.status_code == 202, response.text
        assert scheduled == [True]


def test_unreadable_selected_global_additions_refuses_before_creation(
    client, auth_headers, qa_workflow, monkeypatch, tmp_path, scheduled,
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "operator-config"))
    source = tmp_path / "operator-config" / "my-workshop" / "my.yaml"
    source.mkdir(parents=True)
    body = _body(grant_environment=True, workspace_overrides={"workshop_additions": "global"})
    response = _accept(client, auth_headers, body, _preview(client, auth_headers, body))
    assert response.status_code == 422
    assert "cannot read global Workshop additions" in response.text
    _assert_no_work(client, scheduled)


@pytest.mark.parametrize(
    "committed", [None, b"", b"# committed additions\n", b"x" * 2_000_000],
    ids=["absent", "empty", "comments", "large"],
)
def test_project_guard_reads_accepted_base_not_dirty_checkout(
    client, auth_headers, qa_workflow, git_checkout, committed, monkeypatch, scheduled,
):
    source = git_checkout / "workshop.my.yaml"
    if committed is not None:
        source.write_bytes(committed)
        subprocess.run(["git", "add", "workshop.my.yaml"], cwd=git_checkout, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", "additions"],
            cwd=git_checkout, check=True, capture_output=True,
        )
    # Deliberately invert the working-copy content. It is not the source that
    # the clone will contain and must have no authority over this guard.
    source.write_bytes(b"" if committed else b"dirty untracked additions\n")
    body = _body(grant_environment=True)
    preview = _preview(client, auth_headers, body)
    assert preview["source_commit"]
    commands: list[tuple] = []
    real_exec = launch_module.asyncio.create_subprocess_exec

    async def record_exec(*args, **kwargs):
        commands.append(args)
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(launch_module.asyncio, "create_subprocess_exec", record_exec)
    response = _accept(client, auth_headers, body, preview)
    assert any("--batch-check=%(objecttype) %(objectsize)" in argv for argv in commands)
    assert not any("-p" in argv for argv in commands)
    if committed:
        assert response.status_code == 422
        assert "project Workshop additions" in response.text
        _assert_no_work(client, scheduled)
    else:
        assert response.status_code == 202, response.text
        assert response.json()["execution_inputs"]["source_commit"] == preview["source_commit"]
        assert scheduled == [True]


def test_unselected_global_additions_do_not_conflict_with_project_grant(
    client, auth_headers, qa_workflow, monkeypatch, tmp_path, scheduled,
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "operator-config"))
    source = tmp_path / "operator-config" / "my-workshop" / "my.yaml"
    source.parent.mkdir(parents=True)
    source.write_text("# global source is not selected\n")
    body = _body(grant_environment=True)
    response = _accept(client, auth_headers, body, _preview(client, auth_headers, body))
    assert response.status_code == 202, response.text
    assert scheduled == [True]


def test_observation_only_environment_still_requires_and_records_grant_without_guard(
    client, auth_headers, qa_workflow, git_checkout, scheduled,
):
    _change_environment(client, wiring=None)
    (git_checkout / "workshop.my.yaml").write_text("# no environment wiring to compose\n")
    subprocess.run(["git", "add", "workshop.my.yaml"], cwd=git_checkout, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", "observation additions"],
        cwd=git_checkout, check=True, capture_output=True,
    )
    ungranted = _body()
    refusal = _accept(client, auth_headers, ungranted, _preview(client, auth_headers, ungranted))
    assert refusal.status_code == 422
    assert "grant_environment" in refusal.text
    _assert_no_work(client, scheduled)
    body = _body(grant_environment=True)
    preview = _preview(client, auth_headers, body)
    assert preview["environment"]["grantable"] is False
    assert preview["source_commit"] is None
    response = _accept(client, auth_headers, body, preview)
    assert response.status_code == 202, response.text
    assert response.json()["execution_inputs"]["environment_grant"]["wiring"] is None
    assert scheduled == [True]


def test_direct_service_call_enforces_grant_rules(client, qa_workflow, scheduled):
    request = LaunchRequest(
        project_name="demo", workflow_name="qa-work", slug="direct-grant",
        prompt="work", model_profile=None, profile_explicit=False, workspace_overrides={},
    )
    service = client.app.state.launch_service
    portal = client.portal
    resolved = portal.call(service.preview, request)
    assert resolved.inputs.environment_grant is None
    with pytest.raises(launch_module.LaunchInputError) as caught:
        portal.call(partial(service.accept, request, preview_token=resolved.fingerprint))
    assert caught.value.field == "grant_environment"
    _assert_no_work(client, scheduled)


def test_execution_grant_round_trip_immutable_snapshot_and_legacy_absence():
    capabilities = {"forge": "forge-broker"}
    grant = EnvironmentGrant("sandbox-qa", capabilities, WIRING, "accepted-time")
    capabilities["browser"] = "chrome"
    assert dict(grant.capabilities) == {"forge": "forge-broker"}
    with pytest.raises(TypeError):
        grant.capabilities["forge"] = "other"
    with pytest.raises(FrozenInstanceError):
        grant.actor = "agent"
    inputs = make_execution_inputs(checkout_path="/checkout")
    legacy = execution_inputs_document(inputs)
    assert "environment_grant" not in legacy
    assert decode_execution_inputs(json.dumps(legacy)).environment_grant is None
    assert decode_execution_inputs(encode_execution_inputs(replace(inputs, environment_grant=grant))).environment_grant == grant
    legacy["environment_grant"] = None
    assert decode_execution_inputs(json.dumps(legacy)).environment_grant is None


def test_ordinary_fingerprint_matches_literal_pregrant_test_vector():
    # Derived from the pre-grant JSON material, not from a second invocation
    # of the implementation under test. False/absent must add no new keys.
    revision = replace(load_definition(PLAIN_WORKFLOW_YAML), revision="legacy-revision")
    inputs = make_execution_inputs(checkout_path="/checkout", revision=revision)
    request = LaunchRequest(
        project_name="demo", workflow_name="plain", slug="fix-bug", prompt="do the thing",
        model_profile=None, profile_explicit=False, workspace_overrides={},
    )
    rows = (PreviewRow(
        step="work", kind="agent", session="main", conditional=False,
        declared_role="default", binding=inputs.step_bindings["work"],
    ),)
    assert launch_fingerprint(request, inputs, revision, rows) == "bff04736c2dd38a8aa38d44d95c731d6"
    assert launch_fingerprint(replace(request, grant_environment=False), inputs, revision, rows) == "bff04736c2dd38a8aa38d44d95c731d6"


def test_grant_timestamp_is_not_review_fingerprint_material():
    revision = load_definition(PLAIN_WORKFLOW_YAML)
    inputs = make_execution_inputs(checkout_path="/checkout", revision=revision)
    environment = EnvironmentDefinition(
        name="sandbox-qa", host="container", provision=("true",), check=("true",),
        capabilities={"forge": "forge-broker"}, wiring=WIRING,
    )
    request = LaunchRequest(
        project_name="demo", workflow_name="plain", slug="fix-bug", prompt="work",
        model_profile=None, profile_explicit=False, workspace_overrides={}, grant_environment=True,
    )
    first = replace(inputs, environment_grant=EnvironmentGrant(
        "sandbox-qa", environment.capabilities, WIRING, "first-time",
    ))
    later = replace(inputs, environment_grant=EnvironmentGrant(
        "sandbox-qa", environment.capabilities, WIRING, "later-time",
    ))
    assert launch_fingerprint(request, first, revision, (), environment) == launch_fingerprint(request, later, revision, (), environment)


def test_missing_check_program_reports_fresh_named_blocker(
    client, auth_headers, qa_workflow, environment_files, scheduled,
):
    body = _body(grant_environment=True)
    preview = _preview(client, auth_headers, body)
    check, _, _ = environment_files
    check.unlink()
    response = _accept(client, auth_headers, body, preview)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["environment"] == "sandbox-qa"
    assert detail["blockers"][0]["code"] == "check-program-missing"
    _assert_no_work(client, scheduled)


def test_slow_grant_checks_run_before_reservation_and_grant_insert_is_inside_it(
    client, auth_headers, qa_workflow, monkeypatch, scheduled,
):
    from contextlib import contextmanager

    body = _body(grant_environment=True)
    preview = _preview(client, auth_headers, body)
    reserved = False
    observed: list[str] = []
    original_reserve = launch_module.reserved_write
    original_probe = client.app.state.environments.probe
    original_guard = client.app.state.launch_service._refuse_additions_conflict
    original_create = launch_module.create_task

    @contextmanager
    def reserve(engine):
        nonlocal reserved
        with original_reserve(engine) as conn:
            reserved = True
            try:
                yield conn
            finally:
                reserved = False

    async def probe(name):
        assert not reserved
        observed.append("probe")
        return await original_probe(name)

    async def guard(resolved):
        assert not reserved
        observed.append("guard")
        return await original_guard(resolved)

    def create(*args, **kwargs):
        assert reserved
        assert kwargs["conn"].in_transaction()
        assert kwargs["execution_inputs"].environment_grant is not None
        observed.append("insert")
        return original_create(*args, **kwargs)

    monkeypatch.setattr(launch_module, "reserved_write", reserve)
    monkeypatch.setattr(client.app.state.environments, "probe", probe)
    monkeypatch.setattr(client.app.state.launch_service, "_refuse_additions_conflict", guard)
    monkeypatch.setattr(launch_module, "create_task", create)
    response = _accept(client, auth_headers, body, preview)
    assert response.status_code == 202, response.text
    assert observed == ["probe", "guard", "insert"]
    assert scheduled == [True]


def test_project_base_move_invalidates_review_before_access_is_accepted(
    client, auth_headers, qa_workflow, git_checkout, scheduled,
):
    body = _body(grant_environment=True)
    preview = _preview(client, auth_headers, body)
    (git_checkout / "README.md").write_text("base changed\n")
    subprocess.run(["git", "add", "README.md"], cwd=git_checkout, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-m", "move base"],
        cwd=git_checkout, check=True, capture_output=True,
    )
    response = _accept(client, auth_headers, body, preview)
    assert response.status_code == 409
    assert response.json()["detail"]["preview"]["source_commit"] != preview["source_commit"]
    _assert_no_work(client, scheduled)
