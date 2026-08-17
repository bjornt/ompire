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
        payload = message["payload"]
        assert payload.keys() == {
            "projects",
            "templates",
            "tasks",
            "sessions",
            "workflows",
            "attention",
            "reviews",
            "ships",
            "gpg",
        }
        assert payload["projects"] == []
        assert payload["templates"] == []
        assert payload["tasks"] == []
        assert payload["sessions"] == {}
        assert payload["workflows"] == {}
        assert payload["attention"] == {}
        assert payload["reviews"] == {}
        assert payload["ships"] == {}
        assert payload["gpg"]["state"] in ("cached", "locked", "unknown")


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


def test_template_events_and_snapshot(
    client: TestClient, auth_token: str, auth_headers: dict[str, str]
) -> None:
    client.post(
        "/api/projects",
        headers=auth_headers,
        json={"name": "demo", "title": "Demo", "upstream_url": "https://example.com/demo.git"},
    )

    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        snapshot = ws.receive_json()
        assert snapshot["payload"]["templates"] == []

        response = client.post(
            "/api/templates",
            headers=auth_headers,
            json={"name": "demo", "project_name": "demo"},
        )
        assert response.status_code == 201

        event = ws.receive_json()
        assert event["type"] == "template_created"
        assert event["payload"]["name"] == "demo"
        assert event["payload"]["base_branch"] == "main"
        assert event["seq"] > snapshot["seq"]

        deleted = client.delete("/api/templates/demo", headers=auth_headers)
        assert deleted.status_code == 200
        event = ws.receive_json()
        assert event["type"] == "template_deleted"
        assert event["payload"] == {"name": "demo"}

    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        snapshot = ws.receive_json()
        assert snapshot["payload"]["templates"] == []


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
    client: TestClient, auth_token: str, auth_headers: dict[str, str], demo_template: dict
) -> None:

    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        ws.receive_json()  # snapshot

        response = client.post(
            "/api/tasks",
            headers=auth_headers,
            json={"template_name": "demo", "slug": "fix-bug", "prompt": "fix it"},
        )
        assert response.status_code == 202
        task_id = response.json()["id"]

        created = ws.receive_json()
        assert created["type"] == "task_created"
        assert created["payload"]["slug"] == "fix-bug"
        assert created["payload"]["branch"] == "ompire/fix-bug"
        # Tasks carry workflow run state, never a session id (the engine owns
        # sessions now).
        assert "session_id" not in created["payload"]
        assert created["payload"]["workflow_name"] == "single-step"

        # Drain until the pipeline settles. `spawn_step` covers the four
        # workspace steps only (fetch/clone/branch/workshop); session spawn
        # and the prompt are the workflow engine's `workflow_step` events
        # afterwards, and run-state changes arrive as task_updated.
        steps = []
        while True:
            event = ws.receive_json()
            if event["type"] == "task_updated" and event["payload"]["spawn_completed_at"] is not None:
                break
            assert event["type"] in ("spawn_step", "status_changed", "task_updated", "workflow_step")
            if event["type"] == "spawn_step":
                steps.append((event["payload"]["step"], event["payload"]["status"]))
        assert ("clone", "ok") in steps
        assert ("workshop", "ok") in steps

        # The workflow engine runs the single-step `work` step on the `main`
        # session to completion once the fake omp's burst idles.
        workflow_events = []
        while True:
            event = ws.receive_json()
            if event["type"] == "workflow_step" and event["payload"]["task_id"] == task_id:
                workflow_events.append(event["payload"])
            if (
                event["type"] == "task_updated"
                and event["payload"]["id"] == task_id
                and event["payload"]["workflow_status"] == "complete"
            ):
                break
        assert [(e["step"], e["kind"], e["session"], e["status"]) for e in workflow_events] == [
            ("work", "agent", "main", "started"),
            ("work", "agent", "main", "ok"),
        ]

    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        snapshot = ws.receive_json()
        assert [t["slug"] for t in snapshot["payload"]["tasks"]] == ["fix-bug"]
        # The workflows map carries the run: name/status/step plus the
        # step-record history.
        workflow = snapshot["payload"]["workflows"][str(task_id)]
        assert workflow["name"] == "single-step"
        assert workflow["status"] == "complete"
        assert workflow["step"] is None
        assert [(s["step"], s["kind"], s["session"], s["status"]) for s in workflow["steps"]] == [
            ("work", "agent", "main", "ok")
        ]


def test_snapshot_carries_session_statuses(
    client: TestClient, auth_token: str, auth_headers: dict[str, str], demo_template: dict
) -> None:

    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        ws.receive_json()  # snapshot
        response = client.post(
            "/api/tasks",
            headers=auth_headers,
            json={"template_name": "demo", "slug": "fix-bug", "prompt": "fix it"},
        )
        assert response.status_code == 202
        task_id = response.json()["id"]
        # The fake omp's burst ends quietly: wait for the idle transition.
        while True:
            event = ws.receive_json()
            if (
                event["type"] == "status_changed"
                and event["payload"]["task_id"] == task_id
                and event["payload"]["to"] == "idle"
            ):
                assert event["payload"]["session"] == "main"
                break

    # A reconnect sees the current status without replaying events, nested
    # task → session.
    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        snapshot = ws.receive_json()
        session = snapshot["payload"]["sessions"][str(task_id)]["main"]
        assert session["status"] == "idle"
        assert "queue empty" in session["reason"]
        assert session["since"]

    assert (
        client.post(
            f"/api/tasks/{task_id}/sessions/main/agent/stop", headers=auth_headers
        ).status_code
        == 200
    )
