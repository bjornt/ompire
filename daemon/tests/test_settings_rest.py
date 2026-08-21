"""Tests for the daemon-settings capability: settings store REST surface,
daemon info, token show/rotate, and WebSocket snapshot/reset."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

EXPECTED_DEFAULTS = {
    "tier.interrupt.desktop": True,
    "tier.interrupt.sound": True,
    "tier.interrupt.badge": True,
    "tier.notify.desktop": True,
    "tier.notify.sound": False,
    "tier.notify.badge": True,
    "tier.badge.desktop": False,
    "tier.badge.sound": False,
    "tier.badge.badge": True,
    "tier.silent.desktop": False,
    "tier.silent.sound": False,
    "tier.silent.badge": False,
    "renotify_interval": 300,
    "stall_threshold": 300,
    "context_advisory_threshold": 80,
}


def test_get_settings_defaults(client: TestClient, auth_headers: dict[str, str]) -> None:
    response = client.get("/api/settings", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["settings"] == EXPECTED_DEFAULTS
    assert all(prov == "default" for prov in body["provenance"].values())


def test_toml_seeds_config_layer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("stall_threshold = 600\nrenotify_interval = 120\n")

    from ompire_daemon.app import create_app
    from ompire_daemon.config import load_config

    config = load_config(config_path)
    app = create_app(config, frontend_dist=tmp_path / "no-dist")
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {app.state.auth_token}"}
        body = client.get("/api/settings", headers=headers).json()
        assert body["settings"]["stall_threshold"] == 600
        assert body["provenance"]["stall_threshold"] == "config"
        assert body["provenance"]["renotify_interval"] == "config"
        assert body["provenance"]["context_advisory_threshold"] == "default"


def test_put_override_wins_over_toml(client: TestClient, auth_headers: dict[str, str]) -> None:
    body = client.put(
        "/api/settings",
        headers=auth_headers,
        json={"stall_threshold": 900},
    ).json()
    assert body["settings"]["stall_threshold"] == 900
    assert body["provenance"]["stall_threshold"] == "override"

    get_body = client.get("/api/settings", headers=auth_headers).json()
    assert get_body["settings"]["stall_threshold"] == 900


def test_put_validates_unknown_key(client: TestClient, auth_headers: dict[str, str]) -> None:
    response = client.put(
        "/api/settings",
        headers=auth_headers,
        json={"bogus.key": True},
    )
    assert response.status_code == 422
    assert "bogus.key" in response.json()["detail"]


def test_put_validates_value_range(client: TestClient, auth_headers: dict[str, str]) -> None:
    response = client.put(
        "/api/settings",
        headers=auth_headers,
        json={"context_advisory_threshold": 150},
    )
    assert response.status_code == 422
    assert "context_advisory_threshold" in response.json()["detail"]


def test_put_is_atomic(client: TestClient, auth_headers: dict[str, str]) -> None:
    response = client.put(
        "/api/settings",
        headers=auth_headers,
        json={"stall_threshold": 600, "context_advisory_threshold": 150},
    )
    assert response.status_code == 422
    body = client.get("/api/settings", headers=auth_headers).json()
    assert body["settings"]["stall_threshold"] == EXPECTED_DEFAULTS["stall_threshold"]


def test_delete_reverts_to_lower_layer(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    client.put("/api/settings", headers=auth_headers, json={"renotify_interval": 180})
    body = client.delete("/api/settings/renotify_interval", headers=auth_headers).json()
    assert body["settings"]["renotify_interval"] == EXPECTED_DEFAULTS["renotify_interval"]
    assert body["provenance"]["renotify_interval"] == "default"


def test_delete_unknown_key_is_404(client: TestClient, auth_headers: dict[str, str]) -> None:
    assert client.delete("/api/settings/bogus.key", headers=auth_headers).status_code == 404


def test_settings_changed_broadcast(client: TestClient, auth_token: str) -> None:
    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        ws.receive_json()  # snapshot
        client.put(
            "/api/settings",
            headers={"Authorization": f"Bearer {auth_token}"},
            json={"renotify_interval": 180},
        )
        message = ws.receive_json()
        assert message["type"] == "settings_changed"
        assert message["payload"]["settings"]["renotify_interval"] == 180


def test_snapshot_carries_settings(client: TestClient, auth_token: str) -> None:
    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        message = ws.receive_json()
        assert message["payload"]["settings"] == EXPECTED_DEFAULTS


def test_daemon_info(client: TestClient, auth_headers: dict[str, str], tmp_path: Path) -> None:
    response = client.get("/api/daemon/info", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["bind"] == "127.0.0.1"
    assert body["port"] == 4173
    assert isinstance(body["version"], str)
    assert body["audit_log_path"] is None


def test_get_token_requires_auth(client: TestClient) -> None:
    assert client.get("/api/settings/token").status_code == 401


def test_get_token_returns_current_token(client: TestClient, auth_token: str) -> None:
    response = client.get(
        "/api/settings/token",
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert response.status_code == 200
    assert response.json()["token"] == auth_token


def test_rotate_token_rewrites_file_and_swaps_auth(
    client: TestClient, app, auth_token: str
) -> None:
    from ompire_daemon.auth import token_path_for

    response = client.post(
        "/api/settings/token/rotate",
        headers={"Authorization": f"Bearer {auth_token}"},
    )
    assert response.status_code == 200
    new_token = response.json()["token"]
    assert new_token != auth_token
    assert app.state.auth_token == new_token

    path = token_path_for(app.state.config.data_dir)
    assert path.read_text().strip() == new_token
    assert (path.stat().st_mode & 0o777) == 0o600

    assert (
        client.get("/api/settings", headers={"Authorization": f"Bearer {auth_token}"}).status_code
        == 401
    )
    assert (
        client.get("/api/settings", headers={"Authorization": f"Bearer {new_token}"}).status_code
        == 200
    )


def test_rotate_closes_open_websocket(client: TestClient, auth_token: str) -> None:
    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        ws.receive_json()  # snapshot
        response = client.post(
            "/api/settings/token/rotate",
            headers={"Authorization": f"Bearer {auth_token}"},
        )
        assert response.status_code == 200
        # The server closed the socket; starlette's test client raises a
        # WebSocketDisconnect from receive_json().
        with pytest.raises(Exception):  # noqa: B017 — any disconnect wrapper
            ws.receive_json()
