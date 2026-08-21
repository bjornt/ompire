"""End-to-end tests for the agent REST surface (stop only — start/prompt are
the spawn pipeline's job now) and the per-agent WebSocket channel, driven
through the app against the fake omp behind the fake workshop CLI."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ompire_daemon.registry.projects import create_project
from ompire_daemon.registry.tasks import create_task


@pytest.fixture
def registry_task_id(app, tmp_path: Path) -> int:
    """A registered task with no pipeline run and no live agent."""
    create_project(
        app.state.engine,
        name="demo",
        title="Demo",
        upstream_url="https://example.com/demo.git",
        fork_url=None,
        checkout_path=str(tmp_path / "checkout"),
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


def _spawn_live_task(
    client: TestClient, auth_headers: dict, auth_token: str, slug: str = "agent-live", prompt: str = "hi"
) -> int:
    """Spawn through the real pipeline (fake omp) and wait for the task's
    first turn to idle. The spawn pipeline covers the workspace steps only;
    the workflow engine then lazily spawns the `main` session and sends the
    prompt, so readiness is the session's debounced idle transition (agent
    live, burst flushed to the ring buffer), not `spawn_completed_at`."""
    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        ws.receive_json()  # snapshot
        response = client.post(
            "/api/tasks",
            headers=auth_headers,
            json={"template_name": "demo", "slug": slug, "prompt": prompt},
        )
        assert response.status_code == 202, response.text
        task_id = response.json()["id"]
        while True:
            event = ws.receive_json()
            if (
                event["type"] == "status_changed"
                and event["payload"]["task_id"] == task_id
                and event["payload"]["session"] == "main"
                and event["payload"]["to"] == "idle"
            ):
                return task_id


def _wait_for_question_posted(ws, task_id: int) -> dict:
    """Drain the main WS until `question_posted` for `task_id` carries a
    fully resolved question, returning the normalized payload. An `ask` is
    posted provisionally first (empty `questions` — confirmed by dogfooding
    2026-07-20: real omp emits `extension_ui_request` before
    `tool_execution_start`) and then upgraded in place once the structured
    args arrive; an `approval` never gets a second post."""
    while True:
        event = ws.receive_json()
        if event["type"] == "question_posted" and event["payload"]["task_id"] == task_id:
            question = event["payload"]["question"]
            if question["kind"] == "approval" or question["questions"]:
                return question


def drain_channel_until(ws, event_type: str) -> list[dict]:
    """Read envelopes off an agent channel until `event_type` (inclusive)."""
    seen = []
    while True:
        envelope = ws.receive_json()
        seen.append(envelope)
        if envelope["type"] == event_type:
            return seen


def test_start_and_prompt_routes_are_gone(
    client: TestClient, auth_headers: dict, registry_task_id: int
) -> None:
    start = client.post(f"/api/tasks/{registry_task_id}/agent/start", headers=auth_headers)
    prompt = client.post(
        f"/api/tasks/{registry_task_id}/agent/prompt", headers=auth_headers, json={"message": "hi"}
    )
    assert start.status_code in (404, 405)
    assert prompt.status_code in (404, 405)


def test_stop_404_for_unknown_task(client: TestClient, auth_headers: dict) -> None:
    assert client.post("/api/tasks/999/sessions/main/agent/stop", headers=auth_headers).status_code == 404


def test_stop_409_without_live_agent(
    client: TestClient, auth_headers: dict, registry_task_id: int
) -> None:
    assert (
        client.post(
            f"/api/tasks/{registry_task_id}/sessions/main/agent/stop", headers=auth_headers
        ).status_code
        == 409
    )


def test_spawned_agent_events_and_stop_end_to_end(
    client: TestClient, auth_headers: dict, auth_token: str, demo_template: dict
) -> None:
    task_id = _spawn_live_task(client, auth_headers, auth_token)

    with client.websocket_connect(f"/api/ws/agents/{task_id}/main?token={auth_token}") as agent_ws:
        # The pipeline's prompt already ran: the ring buffer replays its burst.
        envelopes = drain_channel_until(agent_ws, "agent_end")
        types = [envelope["type"] for envelope in envelopes]
        assert types.index("agent_start") < types.index("agent_end")
        # Envelope form: monotonic seq, opaque frame as payload.
        assert [envelope["seq"] for envelope in envelopes] == list(range(len(envelopes)))
        assert envelopes[0]["payload"]["type"] == envelopes[0]["type"]
        # No main-socket registry events leak onto the agent channel.
        assert "snapshot" not in types

        with client.websocket_connect(f"/api/ws?token={auth_token}") as main_ws:
            main_ws.receive_json()  # snapshot
            assert (
                client.post(f"/api/tasks/{task_id}/sessions/main/agent/stop", headers=auth_headers).status_code
                == 200
            )
            # Interpretation precedes the raw exit fact on the main hub.
            statuses = []
            while True:
                event = main_ws.receive_json()
                if event["type"] == "status_changed":
                    statuses.append(event["payload"])
                if event["type"] == "agent_exited":
                    assert event["payload"]["task_id"] == task_id
                    assert event["payload"]["session"] == "main"
                    assert event["payload"]["exit_code"] != 0  # killed
                    break
            assert statuses[-1]["session"] == "main"
            assert statuses[-1]["to"] == "failed"
            assert statuses[-1]["reason"] == "stopped by operator"

        # Channel closes after the exit flush.
        with pytest.raises(WebSocketDisconnect):
            while True:
                agent_ws.receive_json()

    # The agent is gone: stopping again conflicts.
    assert client.post(f"/api/tasks/{task_id}/sessions/main/agent/stop", headers=auth_headers).status_code == 409


def test_replay_then_live_on_late_connect(
    client: TestClient, auth_headers: dict, auth_token: str, demo_template: dict
) -> None:
    task_id = _spawn_live_task(client, auth_headers, auth_token)

    # Two connects replay the same buffered history in original order.
    with client.websocket_connect(f"/api/ws/agents/{task_id}/main?token={auth_token}") as ws:
        first_types = [envelope["type"] for envelope in drain_channel_until(ws, "agent_end")]
    with client.websocket_connect(f"/api/ws/agents/{task_id}/main?token={auth_token}") as ws:
        replay_types = [envelope["type"] for envelope in drain_channel_until(ws, "agent_end")]

    assert replay_types == first_types
    assert client.post(f"/api/tasks/{task_id}/sessions/main/agent/stop", headers=auth_headers).status_code == 200


@pytest.mark.parametrize(
    ("path", "command"),
    [
        ("steer", "steer"),
        ("follow-up", "follow_up"),
        ("interrupt", "abort_and_prompt"),
    ],
)
def test_composer_action_reaches_live_agent(
    client: TestClient,
    auth_headers: dict,
    auth_token: str,
    demo_template: dict,
    path: str,
    command: str,
) -> None:
    task_id = _spawn_live_task(client, auth_headers, auth_token)
    response = client.post(
        f"/api/tasks/{task_id}/sessions/main/agent/{path}",
        headers=auth_headers,
        json={"message": "keep going"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["command"] == command
    assert response.json()["success"] is True
    assert client.post(f"/api/tasks/{task_id}/sessions/main/agent/stop", headers=auth_headers).status_code == 200


def test_composer_action_surfaces_agent_rejection(
    client: TestClient, auth_headers: dict, auth_token: str, demo_template: dict
) -> None:
    task_id = _spawn_live_task(client, auth_headers, auth_token)
    # `fail` makes the fake agent answer success: false → 502 upstream error.
    response = client.post(
        f"/api/tasks/{task_id}/sessions/main/agent/steer",
        headers=auth_headers,
        json={"message": "fail"},
    )
    assert response.status_code == 502, response.text
    assert "boom" in response.json()["detail"]
    assert client.post(f"/api/tasks/{task_id}/sessions/main/agent/stop", headers=auth_headers).status_code == 200


def test_state_and_stats_pass_through(
    client: TestClient, auth_headers: dict, auth_token: str, demo_template: dict
) -> None:
    task_id = _spawn_live_task(client, auth_headers, auth_token)
    state = client.get(f"/api/tasks/{task_id}/sessions/main/agent/state", headers=auth_headers)
    assert state.status_code == 200, state.text
    assert state.json()["isStreaming"] is False
    assert "queuedMessageCount" in state.json()

    stats = client.get(f"/api/tasks/{task_id}/sessions/main/agent/stats", headers=auth_headers)
    assert stats.status_code == 200, stats.text
    assert stats.json()["outputTokens"] == 340
    assert stats.json()["totalCostUsd"] == pytest.approx(0.0123)
    assert client.post(f"/api/tasks/{task_id}/sessions/main/agent/stop", headers=auth_headers).status_code == 200


@pytest.mark.parametrize("path", ["steer", "follow-up", "interrupt"])
def test_composer_action_404_for_unknown_task(
    client: TestClient, auth_headers: dict, path: str
) -> None:
    response = client.post(
        f"/api/tasks/999/sessions/main/agent/{path}", headers=auth_headers, json={"message": "x"}
    )
    assert response.status_code == 404


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "steer"),
        ("post", "follow-up"),
        ("post", "interrupt"),
        ("get", "state"),
        ("get", "stats"),
    ],
)
def test_agent_interaction_409_without_live_agent(
    client: TestClient, auth_headers: dict, registry_task_id: int, method: str, path: str
) -> None:
    url = f"/api/tasks/{registry_task_id}/sessions/main/agent/{path}"
    if method == "post":
        response = client.post(url, headers=auth_headers, json={"message": "x"})
    else:
        response = client.get(url, headers=auth_headers)
    assert response.status_code == 409


def test_session_scoped_routes_404_for_undeclared_session(
    client: TestClient, auth_headers: dict, registry_task_id: int
) -> None:
    """The task's workflow (`single-step`) declares exactly `main`; any other
    session name is a 404 — checked before the live-agent check, so a session
    the workflow doesn't declare never surfaces as a 409."""
    assert (
        client.get(
            f"/api/tasks/{registry_task_id}/sessions/nope/agent/state", headers=auth_headers
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/tasks/{registry_task_id}/sessions/nope/agent/stop", headers=auth_headers
        ).status_code
        == 404
    )


def test_agent_ws_rejects_bad_token(client: TestClient, registry_task_id: int) -> None:
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(f"/api/ws/agents/{registry_task_id}/main?token=wrong"):
        pass


def test_agent_ws_rejects_missing_agent(
    client: TestClient, auth_token: str, registry_task_id: int
) -> None:
    with (
        client.websocket_connect(f"/api/ws/agents/{registry_task_id}/main?token={auth_token}") as ws,
        pytest.raises(WebSocketDisconnect) as excinfo,
    ):
        ws.receive_json()
    assert excinfo.value.code == 4404


# --- Ask/approval answer route (ask-approvals capability) -------------------


def test_answer_resolves_ask_and_returns_to_working(
    client: TestClient, auth_headers: dict, auth_token: str, demo_template: dict
) -> None:
    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        ws.receive_json()  # snapshot
        response = client.post(
            "/api/tasks",
            headers=auth_headers,
            json={"template_name": "demo", "slug": "agent-ask", "prompt": "ask"},
        )
        assert response.status_code == 202, response.text
        task_id = response.json()["id"]

        question = _wait_for_question_posted(ws, task_id)
        assert question["kind"] == "ask"
        assert question["questions"][0]["recommended"] == "Yes, both loops (Recommended)"
        assert question["questions"][0]["options"][0]["value"] == "Yes, both loops (Recommended)"

        answer = client.post(
            f"/api/tasks/{task_id}/sessions/main/agent/answer",
            headers=auth_headers,
            json={"question_id": question["id"], "selections": [question["questions"][0]["options"][0]["value"]]},
        )
        assert answer.status_code == 200, answer.text
        assert answer.json() == {
            "task_id": task_id,
            "session": "main",
            "question_id": question["id"],
            "answered": True,
        }

        seen_resolved = seen_working = False
        while not (seen_resolved and seen_working):
            event = ws.receive_json()
            if event["type"] == "question_resolved" and event["payload"]["task_id"] == task_id:
                assert event["payload"]["session"] == "main"
                assert event["payload"]["question_id"] == question["id"]
                seen_resolved = True
            if (
                event["type"] == "status_changed"
                and event["payload"]["task_id"] == task_id
                and event["payload"]["to"] == "working"
            ):
                assert event["payload"]["session"] == "main"
                assert event["payload"]["from"] == "waiting-input"
                seen_working = True

        assert client.post(f"/api/tasks/{task_id}/sessions/main/agent/stop", headers=auth_headers).status_code == 200


def test_answer_resolves_approval(
    client: TestClient, auth_headers: dict, auth_token: str, demo_template: dict
) -> None:
    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        ws.receive_json()  # snapshot
        response = client.post(
            "/api/tasks",
            headers=auth_headers,
            json={"template_name": "demo", "slug": "agent-approve", "prompt": "approve"},
        )
        assert response.status_code == 202, response.text
        task_id = response.json()["id"]

        question = _wait_for_question_posted(ws, task_id)
        assert question["kind"] == "approval"

        answer = client.post(
            f"/api/tasks/{task_id}/sessions/main/agent/answer",
            headers=auth_headers,
            json={"question_id": question["id"], "approved": True},
        )
        assert answer.status_code == 200, answer.text

        assert client.post(f"/api/tasks/{task_id}/sessions/main/agent/stop", headers=auth_headers).status_code == 200


def test_answer_stale_question_id_conflicts(
    client: TestClient, auth_headers: dict, auth_token: str, demo_template: dict
) -> None:
    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        ws.receive_json()  # snapshot
        response = client.post(
            "/api/tasks",
            headers=auth_headers,
            json={"template_name": "demo", "slug": "agent-ask-stale", "prompt": "ask"},
        )
        task_id = response.json()["id"]
        _wait_for_question_posted(ws, task_id)

        answer = client.post(
            f"/api/tasks/{task_id}/sessions/main/agent/answer",
            headers=auth_headers,
            json={"question_id": "not-the-pending-one", "selections": ["Yes, both loops"]},
        )
        assert answer.status_code == 409

    assert client.post(f"/api/tasks/{task_id}/sessions/main/agent/stop", headers=auth_headers).status_code == 200


def test_answer_409_without_live_agent(
    client: TestClient, auth_headers: dict, registry_task_id: int
) -> None:
    response = client.post(
        f"/api/tasks/{registry_task_id}/sessions/main/agent/answer",
        headers=auth_headers,
        json={"question_id": "whatever"},
    )
    assert response.status_code == 409


def test_answer_404_for_unknown_task(client: TestClient, auth_headers: dict) -> None:
    response = client.post(
        "/api/tasks/999/sessions/main/agent/answer",
        headers=auth_headers,
        json={"question_id": "whatever"},
    )
    assert response.status_code == 404


def test_interrupt_clears_pending_question(
    client: TestClient, auth_headers: dict, auth_token: str, demo_template: dict
) -> None:
    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        ws.receive_json()  # snapshot
        response = client.post(
            "/api/tasks",
            headers=auth_headers,
            json={"template_name": "demo", "slug": "agent-ask-interrupt", "prompt": "ask"},
        )
        task_id = response.json()["id"]
        question = _wait_for_question_posted(ws, task_id)

        interrupt = client.post(
            f"/api/tasks/{task_id}/sessions/main/agent/interrupt",
            headers=auth_headers,
            json={"message": "never mind, stop"},
        )
        assert interrupt.status_code == 200, interrupt.text

        # The pending question is gone; answering it now is a stale-id conflict.
        answer = client.post(
            f"/api/tasks/{task_id}/sessions/main/agent/answer",
            headers=auth_headers,
            json={"question_id": question["id"], "selections": ["Yes, both loops"]},
        )
        assert answer.status_code == 409

    assert client.post(f"/api/tasks/{task_id}/sessions/main/agent/stop", headers=auth_headers).status_code == 200


def test_judge_session_admitted_on_session_routes(
    client: TestClient, auth_headers: dict, auth_token: str, demo_template: dict
) -> None:
    """The engine-reserved judge session is reachable on session-scoped routes
    (its transcript is the audit trail), while undeclared names still 404.

    Drives a real bugfix run through the pipeline: fake omp never writes an
    outcome file, so the reproduce step's judge fires, then triage's judge —
    both come back empty and the run parks at the synthesized gate with the
    judge session live."""
    import time

    created = client.post(
        "/api/templates",
        headers=auth_headers,
        json={"name": "qa-bugfix", "project_name": "demo", "workflow": "bugfix"},
    )
    assert created.status_code == 201, created.text

    response = client.post(
        "/api/tasks",
        headers=auth_headers,
        json={"template_name": "qa-bugfix", "slug": "judge-route", "prompt": "fix the bug"},
    )
    assert response.status_code == 202, response.text
    task_id = response.json()["id"]

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        task = client.get(f"/api/tasks/{task_id}", headers=auth_headers).json()
        if task["workflow_status"] == "waiting":
            break
        time.sleep(0.2)
    assert task["workflow_status"] == "waiting", task

    state = client.get(
        f"/api/tasks/{task_id}/sessions/judge/agent/state", headers=auth_headers
    )
    assert state.status_code == 200, state.text
    ghost = client.get(
        f"/api/tasks/{task_id}/sessions/ghost/agent/state", headers=auth_headers
    )
    assert ghost.status_code == 404
