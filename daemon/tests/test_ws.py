"""WebSocket tests covering the `daemon-api` capability's snapshot-then-deltas scenarios."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect


def test_connect_receives_snapshot_first(client: TestClient, auth_token: str) -> None:
    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        message = ws.receive_json()
        assert message["type"] == "snapshot"
        assert message["seq"] == 0
        assert message["payload"] == {"projects": [], "tasks": []}


def test_mutation_broadcast(client: TestClient, auth_token: str, auth_headers: dict[str, str]) -> None:
    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        snapshot = ws.receive_json()

        response = client.post(
            "/api/projects",
            headers=auth_headers,
            json={"name": "ompire", "title": "Ompire", "upstream_url": "https://example.com/ompire.git"},
        )
        assert response.status_code == 201

        event = ws.receive_json()
        assert event["type"] == "project_created"
        assert event["payload"]["name"] == "ompire"
        assert event["seq"] > snapshot["seq"]


def test_reconnect_gets_fresh_snapshot(
    client: TestClient, auth_token: str, auth_headers: dict[str, str]
) -> None:
    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        ws.receive_json()

    client.post(
        "/api/projects",
        headers=auth_headers,
        json={"name": "ompire", "title": "Ompire", "upstream_url": "https://example.com/ompire.git"},
    )

    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        snapshot = ws.receive_json()
        assert snapshot["type"] == "snapshot"
        assert [p["name"] for p in snapshot["payload"]["projects"]] == ["ompire"]


def test_ws_requires_valid_token(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/ws?token=wrong"):
            pass


def test_task_events_and_snapshot(
    client: TestClient, auth_token: str, auth_headers: dict[str, str], git_checkout
) -> None:
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

    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        ws.receive_json()  # snapshot

        response = client.post(
            "/api/tasks",
            headers=auth_headers,
            json={"project_name": "demo", "slug": "fix-bug", "prompt": "fix it"},
        )
        assert response.status_code == 202

        created = ws.receive_json()
        assert created["type"] == "task_created"
        assert created["payload"]["slug"] == "fix-bug"
        assert created["payload"]["branch"] == "ompire/fix-bug"

        # Drain until the pipeline settles: spawn_step events then task_updated.
        steps = []
        while True:
            event = ws.receive_json()
            if event["type"] == "task_updated":
                assert event["payload"]["spawn_completed_at"] is not None
                break
            assert event["type"] == "spawn_step"
            steps.append((event["payload"]["step"], event["payload"]["status"]))
        assert ("clone", "ok") in steps

    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        snapshot = ws.receive_json()
        assert [t["slug"] for t in snapshot["payload"]["tasks"]] == ["fix-bug"]
