"""Cached REST reads, fresh rechecks, startup observations and socket fan-out."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ompire_daemon import environments as environments_module
from ompire_daemon.app import create_app
from ompire_daemon.config import Config
from ompire_daemon.environments import parse_environments


@pytest.fixture
def environment_files(tmp_path: Path) -> tuple[Path, Path, Path]:
    control = tmp_path / "environment-state"
    calls = tmp_path / "checks"
    check = tmp_path / "environment-check"
    control.write_text("ready", encoding="utf-8")
    check.write_text(
        "#!/bin/sh\n"
        f'printf "check\\n" >>"{calls}"\n'
        f'if [ "$(cat "{control}")" != ready ]; then\n'
        '  printf "gateway not answering\\nprivate later line\\n" >&2\n'
        '  exit 1\nfi\n',
        encoding="utf-8",
    )
    check.chmod(0o755)
    return check, control, calls


@pytest.fixture
def daemon_config(
    daemon_config: Config, environment_files: tuple[Path, Path, Path]
) -> Config:
    check, _control, _calls = environment_files
    return replace(
        daemon_config,
        environments=parse_environments(
            {
                "qa-env": {
                    "host": "container",
                    "capabilities": {"model": "auth-gateway", "browser": "chrome"},
                    "provision": ["/bin/true"],
                    "check": [str(check)],
                }
            }
        ),
    )


def test_get_is_cached_and_post_returns_fresh_full_environment(
    client: TestClient,
    auth_headers: dict[str, str],
    environment_files: tuple[Path, Path, Path],
) -> None:
    check, control, calls = environment_files
    initial = client.get("/api/environments", headers=auth_headers)
    assert initial.status_code == 200
    info = initial.json()[0]
    assert info == {
        "name": "qa-env",
        "host": "container",
        "capabilities": {"model": "auth-gateway", "browser": "chrome"},
        "provision": ["/bin/true"],
        "check": [str(check)],
        "status": {
            "state": "ready",
            "blockers": [],
            "checked_at": info["status"]["checked_at"],
        },
    }
    assert info["status"]["checked_at"] is not None
    assert calls.read_text().splitlines() == ["check"]

    control.write_text("degraded", encoding="utf-8")
    assert client.get("/api/environments", headers=auth_headers).json() == [info]
    assert calls.read_text().splitlines() == ["check"]
    fresh = client.post("/api/environments/qa-env/recheck", headers=auth_headers)
    assert fresh.status_code == 200
    degraded = fresh.json()
    assert degraded["name"] == "qa-env"
    assert degraded["capabilities"] == info["capabilities"]
    assert degraded["status"]["state"] == "degraded"
    assert degraded["status"]["blockers"] == [
        {"code": "check-failed", "message": "gateway not answering"}
    ]
    assert degraded["status"]["checked_at"] != info["status"]["checked_at"]
    assert client.get("/api/environments", headers=auth_headers).json() == [degraded]
    assert calls.read_text().splitlines() == ["check", "check"]

    check.unlink()
    missing = client.post("/api/environments/qa-env/recheck", headers=auth_headers).json()
    assert missing["status"]["state"] == "missing"
    assert missing["status"]["blockers"][0]["code"] == "check-program-missing"


def test_recheck_unknown_environment_is_404_and_does_not_probe(
    client: TestClient,
    auth_headers: dict[str, str],
    environment_files: tuple[Path, Path, Path],
) -> None:
    _check, _control, calls = environment_files
    response = client.post("/api/environments/unknown/recheck", headers=auth_headers)
    assert response.status_code == 404
    assert calls.read_text().splitlines() == ["check"]


def test_environment_routes_require_authentication(client: TestClient) -> None:
    assert client.get("/api/environments").status_code == 401
    assert client.post("/api/environments/qa-env/recheck").status_code == 401


def test_snapshot_and_recheck_fanout_carry_identical_full_info(
    client: TestClient,
    auth_token: str,
    auth_headers: dict[str, str],
    environment_files: tuple[Path, Path, Path],
) -> None:
    _check, control, _calls = environment_files
    with (
        client.websocket_connect(f"/api/ws?token={auth_token}") as first,
        client.websocket_connect(f"/api/ws?token={auth_token}") as second,
    ):
        initial = client.get("/api/environments", headers=auth_headers).json()
        snapshots = [first.receive_json(), second.receive_json()]
        for snapshot in snapshots:
            assert snapshot["type"] == "snapshot"
            assert snapshot["payload"]["environments"] == initial
        control.write_text("degraded", encoding="utf-8")
        response = client.post("/api/environments/qa-env/recheck", headers=auth_headers)
        assert response.status_code == 200
        for websocket, snapshot in zip((first, second), snapshots, strict=True):
            event = websocket.receive_json()
            assert event["type"] == "environment_status"
            assert event["seq"] > snapshot["seq"]
            assert event["payload"] == {"environment": response.json()}
    with client.websocket_connect(f"/api/ws?token={auth_token}") as reconnected:
        snapshot = reconnected.receive_json()
        assert snapshot["payload"]["environments"] == [response.json()]


@pytest.mark.parametrize(
    ("outcome", "state", "code"),
    [
        ("failed", "degraded", "check-failed"),
        ("missing-check", "missing", "check-program-missing"),
        ("missing-provision", "missing", "provision-script-missing"),
        ("nonexec", "error", "check-error"),
        ("timeout", "degraded", "check-timed-out"),
    ],
)
def test_broken_environment_is_classified_at_startup_without_preventing_service(
    daemon_config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    environment_files: tuple[Path, Path, Path],
    outcome: str,
    state: str,
    code: str,
) -> None:
    check, control, _calls = environment_files
    if outcome == "failed":
        control.write_text("degraded", encoding="utf-8")
    elif outcome == "missing-check":
        check.unlink()
    elif outcome == "nonexec":
        check.chmod(0o644)
    elif outcome == "timeout":
        check.write_text("#!/bin/sh\nsleep 60\n", encoding="utf-8")
        monkeypatch.setattr(environments_module, "PROBE_TIMEOUT", 0.1)
    elif outcome == "missing-provision":
        daemon_config = replace(
            daemon_config,
            environments=parse_environments({
                "qa-env": {
                    "host": "host",
                    "check": [str(check)],
                    "provision": [str(tmp_path / "absent-provision")],
                }
            }),
        )
    app = create_app(daemon_config, frontend_dist=tmp_path / "no-dist")
    assert app.state.environments.current()[0]["status"]["state"] == "unknown"
    headers = {"Authorization": f"Bearer {app.state.auth_token}"}
    with TestClient(app) as client:
        response = client.get("/api/environments", headers=headers)
        assert response.status_code == 200
        status = response.json()[0]["status"]
        assert status["state"] == state
        assert status["checked_at"] is not None
        assert status["blockers"][0]["code"] == code
        assert client.get("/api/projects", headers=headers).status_code == 200


def test_undeclared_environments_are_empty_in_rest_and_snapshot(
    daemon_config: Config, tmp_path: Path
) -> None:
    app = create_app(replace(daemon_config, environments=()), frontend_dist=tmp_path / "no-dist")
    headers = {"Authorization": f"Bearer {app.state.auth_token}"}
    with TestClient(app) as client:
        assert client.get("/api/environments", headers=headers).json() == []
        with client.websocket_connect(f"/api/ws?token={app.state.auth_token}") as ws:
            assert ws.receive_json()["payload"]["environments"] == []
