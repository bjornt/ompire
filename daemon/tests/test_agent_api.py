"""End-to-end tests for the provisional agent REST surface and the per-agent
WebSocket channel, driven through the app against the fake omp fixture."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ompire_daemon import agent as agent_module
from ompire_daemon.registry.projects import create_project
from ompire_daemon.registry.tasks import create_task

from tests.test_rpc import fake_omp_argv


@pytest.fixture
def scenario(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Point supervisor spawns at the fake omp and skip the container
    preflight; tests flip `name` to exercise failure paths."""
    current = {"name": "happy"}
    monkeypatch.setattr(
        agent_module, "build_agent_argv", lambda clone, env: fake_omp_argv(current["name"])
    )

    async def no_preflight(clone_path: str) -> None:
        return None

    monkeypatch.setattr(agent_module, "verify_ask_timeout", no_preflight)
    return current


@pytest.fixture
def task_id(app, tmp_path: Path) -> int:
    """A registered task, created directly in the registry so no spawn
    pipeline (and no real clone) is involved."""
    create_project(
        app.state.engine,
        name="demo",
        title="Demo",
        upstream_url="https://example.com/demo.git",
        fork_url=None,
        checkout_path=str(tmp_path / "checkout"),
        base_branch="main",
        branch_pattern=None,
        default_branch_pattern="ompire/<slug>",
        default_checkout_root=tmp_path,
    )
    task = create_task(
        app.state.engine,
        project_name="demo",
        slug="agent-test",
        branch="ompire/agent-test",
        clone_path=str(tmp_path / "clone"),
        prompt="do things",
    )
    return task.id


def drain_channel_until(ws, event_type: str) -> list[dict]:
    """Read envelopes off an agent channel until `event_type` (inclusive)."""
    seen = []
    while True:
        envelope = ws.receive_json()
        seen.append(envelope)
        if envelope["type"] == event_type:
            return seen


def test_agent_routes_404_for_unknown_task(client: TestClient, auth_headers: dict) -> None:
    assert client.post("/api/tasks/999/agent/start", headers=auth_headers).status_code == 404
    assert (
        client.post(
            "/api/tasks/999/agent/prompt", headers=auth_headers, json={"message": "hi"}
        ).status_code
        == 404
    )
    assert client.post("/api/tasks/999/agent/stop", headers=auth_headers).status_code == 404


def test_prompt_and_stop_409_without_live_agent(
    client: TestClient, auth_headers: dict, task_id: int
) -> None:
    response = client.post(
        f"/api/tasks/{task_id}/agent/prompt", headers=auth_headers, json={"message": "hi"}
    )
    assert response.status_code == 409
    assert client.post(f"/api/tasks/{task_id}/agent/stop", headers=auth_headers).status_code == 409


def test_start_prompt_events_stop_end_to_end(
    client: TestClient, auth_headers: dict, auth_token: str, task_id: int, scenario: dict
) -> None:
    with client.websocket_connect(f"/api/ws?token={auth_token}") as main_ws:
        main_ws.receive_json()  # snapshot

        response = client.post(f"/api/tasks/{task_id}/agent/start", headers=auth_headers)
        assert response.status_code == 200
        assert response.json() == {"task_id": task_id, "agent": "running"}

        # Double start is rejected and the live agent is unaffected.
        assert (
            client.post(f"/api/tasks/{task_id}/agent/start", headers=auth_headers).status_code
            == 409
        )

        with client.websocket_connect(f"/api/ws/agents/{task_id}?token={auth_token}") as agent_ws:
            response = client.post(
                f"/api/tasks/{task_id}/agent/prompt", headers=auth_headers, json={"message": "hi"}
            )
            assert response.status_code == 200
            assert response.json()["success"] is True

            envelopes = drain_channel_until(agent_ws, "agent_end")
            types = [envelope["type"] for envelope in envelopes]
            assert "extension_ui_request" in types
            assert types.index("agent_start") < types.index("agent_end")
            # Envelope form: monotonic seq, opaque frame as payload.
            assert [envelope["seq"] for envelope in envelopes] == list(range(len(envelopes)))
            assert envelopes[0]["payload"]["type"] == envelopes[0]["type"]
            # No main-socket registry events leak onto the agent channel.
            assert "snapshot" not in types

            assert (
                client.post(f"/api/tasks/{task_id}/agent/stop", headers=auth_headers).status_code
                == 200
            )
            # Channel closes after the exit flush.
            with pytest.raises(WebSocketDisconnect):
                while True:
                    agent_ws.receive_json()

        exited = main_ws.receive_json()
        assert exited["type"] == "agent_exited"
        assert exited["payload"]["task_id"] == task_id
        assert exited["payload"]["exit_code"] != 0  # killed

    # The agent is gone: prompting again conflicts.
    response = client.post(
        f"/api/tasks/{task_id}/agent/prompt", headers=auth_headers, json={"message": "hi"}
    )
    assert response.status_code == 409


def test_start_failure_surfaces_stderr(
    client: TestClient, auth_headers: dict, task_id: int, scenario: dict
) -> None:
    scenario["name"] = "crash"
    response = client.post(f"/api/tasks/{task_id}/agent/start", headers=auth_headers)
    assert response.status_code == 502
    assert "No models available" in response.json()["detail"]
    # No live agent was registered for the task.
    assert client.post(f"/api/tasks/{task_id}/agent/stop", headers=auth_headers).status_code == 409


def test_replay_then_live_on_late_connect(
    client: TestClient, auth_headers: dict, auth_token: str, task_id: int, scenario: dict
) -> None:
    assert client.post(f"/api/tasks/{task_id}/agent/start", headers=auth_headers).status_code == 200
    client.post(f"/api/tasks/{task_id}/agent/prompt", headers=auth_headers, json={"message": "hi"})

    # First client sees the events live.
    with client.websocket_connect(f"/api/ws/agents/{task_id}?token={auth_token}") as ws:
        live_types = [envelope["type"] for envelope in drain_channel_until(ws, "agent_end")]

    # A late connect replays the buffered history in original order.
    with client.websocket_connect(f"/api/ws/agents/{task_id}?token={auth_token}") as ws:
        replay_types = [envelope["type"] for envelope in drain_channel_until(ws, "agent_end")]

    assert replay_types == live_types
    assert client.post(f"/api/tasks/{task_id}/agent/stop", headers=auth_headers).status_code == 200


def test_agent_ws_rejects_bad_token(client: TestClient, task_id: int) -> None:
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/api/ws/agents/{task_id}?token=wrong"):
            pass


def test_agent_ws_rejects_missing_agent(client: TestClient, auth_token: str, task_id: int) -> None:
    with client.websocket_connect(f"/api/ws/agents/{task_id}?token={auth_token}") as ws:
        with pytest.raises(WebSocketDisconnect) as excinfo:
            ws.receive_json()
    assert excinfo.value.code == 4404
