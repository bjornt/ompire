"""Tests for the daemon-settings capability: settings store REST surface,
daemon info, token show/rotate, and WebSocket snapshot/reset."""

from __future__ import annotations

import asyncio
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
    # No selection stored: the probe auto-detects (ADR-0021).
    "gpg_signing_key": None,
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


def test_put_accepts_positive_sub_minute_stall_threshold(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.put(
        "/api/settings", headers=auth_headers, json={"stall_threshold": 2}
    )
    assert response.status_code == 200
    assert response.json()["settings"]["stall_threshold"] == 2


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


def test_put_applies_live_settings_on_event_loop(
    client: TestClient, auth_headers: dict[str, str], app, monkeypatch: pytest.MonkeyPatch
) -> None:
    applied = False

    def require_running_loop(settings: dict[str, object]) -> None:
        nonlocal applied
        asyncio.get_running_loop()
        applied = True

    monkeypatch.setattr(app.state.notifications, "apply_settings", require_running_loop)
    response = client.put(
        "/api/settings", headers=auth_headers, json={"stall_threshold": 2}
    )
    assert response.status_code == 200
    assert applied


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


# --- signing-key selection (ADR-0021) --------------------------------------


def _seed_candidates(app, *fingerprints: str) -> None:
    """Give the probe a keyring without running gpg."""
    from ompire_daemon.gpg import GpgCandidate, GpgStatus

    app.state.gpg._status = GpgStatus(
        state="ambiguous",
        candidates=tuple(
            GpgCandidate(
                fingerprint=fpr,
                key_id=fpr[-16:],
                uid=f"Key {index} <k{index}@example.com>",
                keygrip=f"{index:040d}",
                created_at=None,
                expires_at=None,
                primary_fingerprint=fpr,
            )
            for index, fpr in enumerate(fingerprints)
        ),
    )


_FPR_A = "A" * 40
_FPR_B = "B" * 40


def test_selecting_a_key_in_the_keyring_persists_as_an_override(
    client: TestClient, auth_headers: dict[str, str], app, monkeypatch
) -> None:
    _seed_candidates(app, _FPR_A, _FPR_B)
    probes: list[int] = []

    async def fake_probe():
        probes.append(1)
        return app.state.gpg.current()

    monkeypatch.setattr(app.state.gpg, "probe", fake_probe)

    response = client.put(
        "/api/settings", json={"gpg_signing_key": _FPR_B}, headers=auth_headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["settings"]["gpg_signing_key"] == _FPR_B
    assert body["provenance"]["gpg_signing_key"] == "override"
    # Selecting a key re-probes immediately so the chip and ship gate follow.
    assert probes == [1]


def test_selecting_a_key_not_in_the_keyring_is_refused(
    client: TestClient, auth_headers: dict[str, str], app
) -> None:
    _seed_candidates(app, _FPR_A)

    response = client.put(
        "/api/settings", json={"gpg_signing_key": _FPR_B}, headers=auth_headers
    )

    assert response.status_code == 422
    assert _FPR_B in response.json()["detail"]
    # Nothing was persisted.
    body = client.get("/api/settings", headers=auth_headers).json()
    assert body["settings"]["gpg_signing_key"] is None


@pytest.mark.parametrize(
    "value", ["865639DBB930B899", "not-a-fingerprint", 42, None]
)
def test_selection_must_be_a_full_fingerprint(
    client: TestClient, auth_headers: dict[str, str], app, value
) -> None:
    _seed_candidates(app, _FPR_A)

    response = client.put(
        "/api/settings", json={"gpg_signing_key": value}, headers=auth_headers
    )

    assert response.status_code == 422


def test_a_rejected_selection_does_not_persist_its_batch(
    client: TestClient, auth_headers: dict[str, str], app
) -> None:
    """Validation stays all-or-nothing across a multi-key update."""
    _seed_candidates(app, _FPR_A)

    response = client.put(
        "/api/settings",
        json={"stall_threshold": 600, "gpg_signing_key": _FPR_B},
        headers=auth_headers,
    )

    assert response.status_code == 422
    body = client.get("/api/settings", headers=auth_headers).json()
    assert body["settings"]["stall_threshold"] == 300


def test_clearing_the_selection_returns_to_auto_detection(
    client: TestClient, auth_headers: dict[str, str], app, monkeypatch
) -> None:
    _seed_candidates(app, _FPR_A)
    probes: list[str] = []

    async def fake_probe():
        probes.append("probe")
        return app.state.gpg.current()

    monkeypatch.setattr(app.state.gpg, "probe", fake_probe)
    client.put(
        "/api/settings", json={"gpg_signing_key": _FPR_A}, headers=auth_headers
    )

    response = client.delete("/api/settings/gpg_signing_key", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["settings"]["gpg_signing_key"] is None
    assert body["provenance"]["gpg_signing_key"] == "default"
    assert len(probes) == 2
