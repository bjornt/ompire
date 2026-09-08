"""WebSocket tests covering the `daemon-api` capability's snapshot-then-deltas scenarios."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tests.conftest import spawn_task

from .conftest import make_adoptable_checkout


@pytest.fixture(autouse=True)
def _adoptable_checkouts(app) -> None:
    """Real checkouts for the project names this module registers (ADR-0022)."""
    # Not "demo": the `git_checkout` fixture owns that path.
    for name in ("ompire", "ompire-ng"):
        make_adoptable_checkout(app.state.config.checkout_root, name)



def test_connect_receives_snapshot_first(client: TestClient, auth_token: str) -> None:
    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        message = ws.receive_json()
        assert message["type"] == "snapshot"
        assert message["seq"] == 0
        payload = message["payload"]
        assert payload.keys() == {
            "projects",
            "model_profiles",
            "workflow_library",
            "workflow_catalog",
            "tasks",
            "sessions",
            "workflows",
            "attention",
            "reviews",
            "ships",
            "gpg",
            "gh",
            "settings",
        }
        assert payload["projects"] == []
        assert payload["model_profiles"] == []
        # The library is what exists; the catalog is what a launch may select
        # (ADR-0031). Both come from one read, so a fresh install shows the two
        # packaged built-ins in both, each carrying its current revision.
        assert [w["name"] for w in payload["workflow_library"]] == [
            "bugfix",
            "single-step",
        ]
        assert {w["origin"] for w in payload["workflow_library"]} == {"builtin"}
        assert all(w["available"] for w in payload["workflow_library"])
        assert [w["name"] for w in payload["workflow_catalog"]] == [
            "bugfix",
            "single-step",
        ]
        assert payload["tasks"] == []
        assert payload["sessions"] == {}
        assert payload["workflows"] == {}
        assert payload["attention"] == {}
        assert payload["reviews"] == {}
        assert payload["ships"] == {}
        assert payload["gpg"]["state"] in (
            "ready",
            "locked",
            "ambiguous",
            "no_key",
            "missing",
            "agent_unavailable",
            "unknown",
            "error",
        )
        assert payload["gh"]["identity"]["state"] in (
            "unknown",
            "missing",
            "unauthenticated",
            "ready",
            "error",
        )


def test_mutation_broadcast(
    client: TestClient, auth_token: str, auth_headers: dict[str, str]
) -> None:
    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        snapshot = ws.receive_json()

        response = client.post(
            "/api/projects",
            headers=auth_headers,
            json={
                "name": "ompire",
                "title": "Ompire",
                "upstream_url": "https://example.com/ompire.git",
            },
        )
        assert response.status_code == 201

        event = ws.receive_json()
        assert event["type"] == "project_created"
        assert event["payload"]["name"] == "ompire"
        assert event["seq"] > snapshot["seq"]


def test_every_project_mutation_reaches_a_connected_client(
    client: TestClient, auth_token: str, auth_headers: dict[str, str]
) -> None:
    """Create, plain update, rename, and delete each deliver their event to an
    already-connected client, in order, with no reconnect in between.

    All four are synchronous routes publishing from FastAPI's threadpool, so
    they exercise the hub's cross-thread hand-off (`EventHub.publish`).
    """
    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        ws.receive_json()  # snapshot

        created = client.post(
            "/api/projects",
            headers=auth_headers,
            json={
                "name": "ompire",
                "title": "Ompire",
                "upstream_url": "https://example.com/ompire.git",
            },
        )
        assert created.status_code == 201
        event = ws.receive_json()
        assert event["type"] == "project_created"
        assert event["payload"]["name"] == "ompire"

        stored = created.json()
        updated = client.put(
            "/api/projects/ompire",
            headers=auth_headers,
            json={
                "title": "Ompire, retitled",
                "upstream_url": stored["upstream_url"],
                "fork_url": stored["fork_url"],
                "checkout_path": stored["checkout_path"],
            },
        )
        assert updated.status_code == 200
        event = ws.receive_json()
        assert event["type"] == "project_updated"
        assert event["payload"]["title"] == "Ompire, retitled"

        renamed = client.put(
            "/api/projects/ompire",
            headers=auth_headers,
            json={
                "title": "Ompire, retitled",
                "upstream_url": stored["upstream_url"],
                "fork_url": stored["fork_url"],
                "checkout_path": stored["checkout_path"],
                "new_name": "ompire-ng",
            },
        )
        assert renamed.status_code == 200
        event = ws.receive_json()
        assert event["type"] == "project_renamed"
        assert event["payload"]["old_name"] == "ompire"
        assert event["payload"]["project"]["name"] == "ompire-ng"

        deleted = client.delete("/api/projects/ompire-ng", headers=auth_headers)
        assert deleted.status_code == 200
        event = ws.receive_json()
        assert event["type"] == "project_deleted"
        assert event["payload"] == {"name": "ompire-ng"}


def test_workflow_catalog_rides_the_snapshot_with_no_change_event(
    client: TestClient,
    auth_token: str,
    auth_headers: dict[str, str],
    git_checkout: Path,
) -> None:
    """A reconnecting client gets the catalog authoritatively; nothing
    publishes catalog deltas because there are none to publish."""
    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        snapshot = ws.receive_json()
        catalog = {w["name"]: w for w in snapshot["payload"]["workflow_catalog"]}
        assert catalog["single-step"]["primary_session"] == "main"
        assert catalog["single-step"]["sessions"] == ["main"]
        # A client can see, before launching anything, exactly which
        # privileged effects a workflow can perform.
        assert catalog["single-step"]["actions"] == ["commit", "push", "pr"]
        assert catalog["single-step"]["reviews"] is True
        # A delivery step names its effect and the gate that can authorize it,
        # and is always conditional: it runs only if a person says so.
        steps = {step["name"]: step for step in catalog["single-step"]["steps"]}
        assert steps["work"]["action"] is None
        assert steps["commit-local"]["action"] == "commit"
        assert steps["commit-local"]["approval"] == "approve"
        assert steps["commit-local"]["conditional"] is True
        # The catalog names the revision a new launch of this name would pin,
        # so a client can tell "the same workflow" from "the same name"
        # (ADR-0028).
        assert catalog["single-step"]["revision"].startswith("sha256:")
        assert catalog["single-step"]["format"] == 3


def test_reconnect_gets_fresh_snapshot(
    client: TestClient, auth_token: str, auth_headers: dict[str, str]
) -> None:
    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        ws.receive_json()

    client.post(
        "/api/projects",
        headers=auth_headers,
        json={
            "name": "ompire",
            "title": "Ompire",
            "upstream_url": "https://example.com/ompire.git",
        },
    )

    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        snapshot = ws.receive_json()
        assert snapshot["type"] == "snapshot"
        assert [p["name"] for p in snapshot["payload"]["projects"]] == ["ompire"]


def test_ws_requires_valid_token(client: TestClient) -> None:
    with (
        pytest.raises(WebSocketDisconnect),
        client.websocket_connect("/api/ws?token=wrong"),
    ):
        pass


def test_task_events_and_snapshot(
    client: TestClient,
    auth_token: str,
    auth_headers: dict[str, str],
    demo_project: dict,
) -> None:

    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        ws.receive_json()  # snapshot

        response = spawn_task(client, auth_headers, slug="fix-bug", prompt="fix it")
        assert response.status_code == 202
        task_id = response.json()["id"]

        created = ws.receive_json()
        assert created["type"] == "task_created"
        assert created["payload"]["slug"] == "fix-bug"
        assert created["payload"]["branch"] == "ompire/fix-bug"
        # Tasks carry workflow run state, never a session id (the engine owns
        # sessions now).
        assert "session_id" not in created["payload"]
        assert created["payload"]["workflow_name"] == "plain"

        # Drain until the pipeline settles. `spawn_step` covers the four
        # workspace steps only (fetch/clone/branch/workshop); session spawn
        # and the prompt are the workflow engine's `workflow_step` events
        # afterwards, and run-state changes arrive as task_updated.
        steps = []
        while True:
            event = ws.receive_json()
            if (
                event["type"] == "task_updated"
                and event["payload"]["spawn_completed_at"] is not None
            ):
                break
            assert event["type"] in (
                "spawn_step",
                "status_changed",
                "task_updated",
                "workflow_step",
                "workshop_additions",
                "session_model",
            )
            if event["type"] == "spawn_step":
                steps.append((event["payload"]["step"], event["payload"]["status"]))
            if event["type"] == "workshop_additions":
                # Which additions source applied is disclosed, including when
                # the selected one is simply absent (ADR-0026).
                additions = event["payload"]
        assert ("clone", "ok") in steps
        assert ("workshop", "ok") in steps
        assert additions["source"] == "project"
        assert "no additions" in additions["detail"]

        # The workflow engine runs the single-step `work` step on the `main`
        # session to completion once the fake omp's burst idles.
        workflow_events = []
        while True:
            event = ws.receive_json()
            if (
                event["type"] == "workflow_step"
                and event["payload"]["task_id"] == task_id
            ):
                workflow_events.append(event["payload"])
            if (
                event["type"] == "task_updated"
                and event["payload"]["id"] == task_id
                and event["payload"]["workflow_status"] == "complete"
            ):
                break
        assert [
            (e["step"], e["kind"], e["session"], e["status"]) for e in workflow_events
        ] == [
            ("work", "agent", "main", "started"),
            ("work", "agent", "main", "ok"),
        ]

    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        snapshot = ws.receive_json()
        assert [t["slug"] for t in snapshot["payload"]["tasks"]] == ["fix-bug"]
        # The workflows map carries the run: name/status/step plus the
        # step-record history.
        workflow = snapshot["payload"]["workflows"][str(task_id)]
        assert workflow["name"] == "plain"
        assert workflow["status"] == "complete"
        assert workflow["step"] is None
        assert [
            (s["step"], s["kind"], s["session"], s["status"]) for s in workflow["steps"]
        ] == [("work", "agent", "main", "ok")]


def test_snapshot_carries_session_statuses(
    client: TestClient,
    auth_token: str,
    auth_headers: dict[str, str],
    demo_project: dict,
) -> None:

    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        ws.receive_json()  # snapshot
        response = spawn_task(client, auth_headers, slug="fix-bug", prompt="fix it")
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


def test_snapshot_serves_durable_review_history_with_no_live_process(
    client: TestClient,
    auth_token: str,
    auth_headers: dict[str, str],
    demo_project: dict,
) -> None:
    """The `reviews` map is composed from durable rows, so a reconnect after
    a restart serves the restored history — with `url`/`port` null, because
    the reviewer process did not survive (review capability; ADR-0016)."""
    from ompire_daemon.registry.reviews import append_iteration, open_review

    response = spawn_task(client, auth_headers, slug="fix-bug", prompt="fix it")
    assert response.status_code == 202
    task_id = response.json()["id"]

    engine = client.app.state.engine
    open_review(engine, task_id)
    append_iteration(engine, task_id, outcome="comments", comment_count=2)
    append_iteration(engine, task_id, outcome="approved", status="approved")

    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        snapshot = ws.receive_json()
        review = snapshot["payload"]["reviews"][str(task_id)]

    assert review["status"] == "approved"
    assert review["url"] is None
    assert review["port"] is None
    assert [it["outcome"] for it in review["iterations"]] == ["comments", "approved"]
    assert review["iterations"][0]["comment_count"] == 2


MINIMAL_WORKFLOW = """
format: 1
name: custom
sessions: [main]
primary: main
steps:
  - name: work
    kind: agent
    session: main
    prompt:
      parts:
        - text: "do it"
"""


def _library_events(ws, count: int) -> list[dict]:
    events = []
    while len(events) < count:
        message = ws.receive_json()
        if message["type"] == "workflow_library_updated":
            events.append(message["payload"])
    return events


def test_every_library_mutation_reaches_a_connected_client(
    client: TestClient, auth_token: str, auth_headers: dict[str, str]
) -> None:
    """The library is editable now (ADR-0031), so it needs deltas.

    One full-entry upsert per committed mutation, each carrying a higher edit
    version than the last, and each saying whether the entry is still a launch
    choice — the receiver derives the catalog from that rather than from a
    second event.
    """
    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        ws.receive_json()  # snapshot

        created = client.post(
            "/api/workflow-library",
            headers=auth_headers,
            json={"name": "custom", "yaml": MINIMAL_WORKFLOW},
        )
        assert created.status_code == 201, created.text
        version = created.json()["entry"]["version"]

        saved = client.post(
            "/api/workflow-library/custom/revisions",
            headers=auth_headers,
            json={"yaml": MINIMAL_WORKFLOW, "expected_version": version},
        )
        assert saved.status_code == 200, saved.text

        archived = client.post(
            "/api/workflow-library/custom/archive",
            headers=auth_headers,
            json={"expected_version": saved.json()["entry"]["version"]},
        )
        assert archived.status_code == 200, archived.text

        create_event, save_event, archive_event = _library_events(ws, 3)

    # A draft cannot launch, so the first upsert offers no descriptor.
    assert create_event["name"] == "custom"
    assert create_event["descriptor"] is None
    assert create_event["unavailable_reason"] == "draft_only"
    # The executable save makes it eligible, and the payload carries the shape
    # a launch form would render.
    assert save_event["descriptor"]["name"] == "custom"
    assert save_event["available"] is True
    # Archiving withdraws it in the same event that reports the change.
    assert archive_event["descriptor"] is None
    assert archive_event["archived"] is True
    versions = [e["version"] for e in (create_event, save_event, archive_event)]
    assert versions == sorted(versions) and len(set(versions)) == 3


def test_a_mutation_during_snapshot_delivery_is_not_lost(
    client: TestClient, auth_token: str, auth_headers: dict[str, str]
) -> None:
    """A save committing while a snapshot is being assembled must still reach
    the client.

    The socket subscribes before it reads, so the worst case is an entry
    delivered twice — which the entry's edit version makes idempotent — rather
    than an entry delivered never.
    """
    client.post(
        "/api/workflow-library",
        headers=auth_headers,
        json={"name": "custom", "yaml": MINIMAL_WORKFLOW},
    )
    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        snapshot = ws.receive_json()["payload"]
        names = [e["name"] for e in snapshot["workflow_library"]]
        assert names == ["bugfix", "custom", "single-step"]

        entry = next(e for e in snapshot["workflow_library"] if e["name"] == "custom")
        client.post(
            "/api/workflow-library/custom/revisions",
            headers=auth_headers,
            json={"yaml": MINIMAL_WORKFLOW, "expected_version": entry["version"]},
        )
        [event] = _library_events(ws, 1)
        assert event["version"] > entry["version"]
        assert event["available"] is True


async def test_a_snapshot_overlap_forwards_only_what_a_client_can_order() -> None:
    """What subscribing before the snapshot read is allowed to deliver.

    A library entry carries an edit version, so re-delivering one the snapshot
    already holds is a no-op the client drops — which is why it is safe to
    forward, and why a genuinely newer one is not lost. Every other delta is
    unversioned: one published *before* the snapshot was read and delivered
    after it would move the client backwards, which is worse than the missed
    update it replaces. Those are dropped here exactly as the pre-subscription
    gap dropped them, because the snapshot is already newer than all of them.
    """
    import asyncio
    import itertools

    from ompire_daemon.api.ws import _drain_snapshot_overlap
    from ompire_daemon.events import Event

    queue: asyncio.Queue = asyncio.Queue()
    for event in (
        Event("task_updated", {"id": 1}),
        Event("workflow_library_updated", {"name": "custom", "version": 3}),
        Event("settings_changed", {"settings": {}}),
        Event("workflow_library_updated", {"name": "custom", "version": 4}),
    ):
        queue.put_nowait(event)

    sent: list[tuple[str, object]] = []

    class _Recorder:
        async def send_json(self, envelope: dict) -> None:
            sent.append((envelope["type"], envelope["payload"]))

    await _drain_snapshot_overlap(_Recorder(), queue, itertools.count())

    assert [t for t, _ in sent] == [
        "workflow_library_updated",
        "workflow_library_updated",
    ]
    # In publication order, so the client's version check sees the newer last.
    assert [p["version"] for _, p in sent] == [3, 4]
    assert queue.empty()


def test_a_reconnect_replaces_the_library_projection(
    client: TestClient, auth_token: str, auth_headers: dict[str, str]
) -> None:
    client.post(
        "/api/workflow-library",
        headers=auth_headers,
        json={"name": "custom", "yaml": MINIMAL_WORKFLOW},
    )
    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        first = ws.receive_json()["payload"]["workflow_library"]
    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        again = ws.receive_json()["payload"]["workflow_library"]
    assert first == again
