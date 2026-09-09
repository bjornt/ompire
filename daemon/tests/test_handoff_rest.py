"""Launching a task from a pinned result, over the real REST surface (ADR-0035).

The journey these cover is the one the epic is about: a task captures planning
files, the operator accepts them, a *second* task is launched with that exact
revision, and the bytes it starts from are the bytes that were reviewed.

Everything here goes through preview → acceptance, because the whole point of
the launch boundary is that those two agree. A test that only called the
resolver would prove the rules and miss the contract.
"""

from __future__ import annotations

import hashlib
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ompire_daemon.registry.results import (
    ResultFile,
    build_manifest,
    finish_capture,
    normalize_selection,
    open_capture,
)
from tests.conftest import launch_body, spawn_task

PLAN = b"# Plan\n\nDo the thing.\n"
PLAN_PATH = "epics/demo/PLAN.md"


def _retain(app, task_id: int, *, path: str = PLAN_PATH, body: bytes = PLAN,
            merge_base: str | None = None) -> dict:
    """Retain one bundle for `task_id` exactly as capture would.

    Written straight to the registry rather than through the capture service:
    these tests are about what a *consumer* may do with an accepted revision,
    and driving a real filesystem capture here would only re-test the producing
    side that `test_results_capture` already covers.
    """
    engine = app.state.engine
    selection = normalize_selection([path.rsplit("/", 1)[0]])
    result, _created = open_capture(
        engine, task_id=task_id, request_id=f"req-{task_id}-{path}", selection=selection
    )
    entry = ResultFile(
        path=path,
        length=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
        media_type="text/markdown",
    )
    manifest = build_manifest(
        result_id=result.id,
        task_id=task_id,
        project_name="demo",
        selection=selection,
        files=[entry],
        predecessor_id=result.predecessor_id,
        provenance={
            "capture_actor": "operator",
            "producing_step": "unknown",
            "capture_merge_base": merge_base,
        },
        captured_at=result.started_at,
    )
    return {
        "result": finish_capture(
            engine, result.id, manifest=manifest, contents={path: body}
        ),
        "body": body,
        "path": path,
    }


def _accept(client: TestClient, auth: dict, task_id: int, result) -> None:
    response = client.post(
        f"/api/tasks/{task_id}/results/{result.id}/accept",
        headers=auth,
        json={"expected_manifest_id": result.manifest_id},
    )
    assert response.status_code == 200, response.text


def _wait_settled(client: TestClient, auth: dict, task_id: int, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = client.get(f"/api/tasks/{task_id}", headers=auth).json()
        if task["spawn_completed_at"] is not None or task["state"] == "failed":
            return task
        time.sleep(0.05)
    raise AssertionError("spawn pipeline did not settle in time")


@pytest.fixture
def producer(client: TestClient, auth_headers: dict, demo_project: dict) -> dict:
    response = spawn_task(client, auth_headers, slug="plan-it", prompt="write a plan")
    assert response.status_code == 202, response.text
    task = response.json()
    _wait_settled(client, auth_headers, task["id"])
    return task


def _attach(result, producer_task_id: int) -> list[dict]:
    return [
        {
            "producer_task_id": producer_task_id,
            "result_id": result.id,
            "expected_manifest_id": result.manifest_id,
        }
    ]


def _head(checkout: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()


# --- Selection ---------------------------------------------------------------


def test_an_unaccepted_revision_cannot_be_attached(
    client: TestClient, auth_headers: dict, producer: dict
) -> None:
    """Retention is not acceptance. A complete bundle nobody read is a
    downloadable result, not an input a task may be launched with."""
    retained = _retain(client.app, producer["id"])
    response = client.post(
        "/api/tasks/preview",
        headers=auth_headers,
        json=launch_body(slug="use-it", result_attachments=_attach(retained["result"], producer["id"])),
    )
    assert response.status_code == 422
    assert "has not been accepted" in response.text


def test_a_successor_capture_does_not_retarget_an_explicit_older_selection(
    client: TestClient, auth_headers: dict, producer: dict, git_checkout: Path
) -> None:
    """The manifest id makes a selection name a revision. A later accepted
    capture on the same task is a different revision, and the one the operator
    chose keeps resolving to exactly its own bytes."""
    first = _retain(client.app, producer["id"], merge_base=_head(git_checkout))
    _accept(client, auth_headers, producer["id"], first["result"])
    second = _retain(
        client.app, producer["id"], path="epics/demo/SPEC.md", body=b"# Spec\n",
        merge_base=_head(git_checkout),
    )
    _accept(client, auth_headers, producer["id"], second["result"])

    response = client.post(
        "/api/tasks/preview",
        headers=auth_headers,
        json=launch_body(slug="use-it", result_attachments=_attach(first["result"], producer["id"])),
    )
    assert response.status_code == 200, response.text
    attachments = response.json()["result_attachments"]
    assert [entry["result_id"] for entry in attachments] == [first["result"].id]
    assert attachments[0]["destinations"] == [PLAN_PATH]


def test_a_stale_manifest_id_is_refused_rather_than_upgraded(
    client: TestClient, auth_headers: dict, producer: dict, git_checkout: Path
) -> None:
    retained = _retain(client.app, producer["id"], merge_base=_head(git_checkout))
    _accept(client, auth_headers, producer["id"], retained["result"])
    selection = _attach(retained["result"], producer["id"])
    selection[0]["expected_manifest_id"] = "no-longer-this-revision"
    response = client.post(
        "/api/tasks/preview",
        headers=auth_headers,
        json=launch_body(slug="use-it", result_attachments=selection),
    )
    assert response.status_code == 422
    assert "no longer matches the reviewed revision" in response.text


def test_a_launch_with_no_attachments_is_unchanged(
    client: TestClient, auth_headers: dict, demo_project: dict
) -> None:
    """The regression that matters most: an ordinary launch must resolve no
    commit, read no tree, and gain no way to be refused."""
    response = client.post(
        "/api/tasks/preview", headers=auth_headers, json=launch_body(slug="ordinary")
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["result_attachments"] == []
    assert body["source_commit"] is None
    assert body["needs_base_acknowledgement"] is False


# --- Base acknowledgement ----------------------------------------------------


def test_an_unknown_producer_base_blocks_the_launch_until_acknowledged(
    client: TestClient, auth_headers: dict, producer: dict
) -> None:
    """A capture that recorded no base observation cannot be compared, and an
    absent comparison is not agreement. The operator says out loud that the
    plan was not validated against this target."""
    retained = _retain(client.app, producer["id"], merge_base=None)
    _accept(client, auth_headers, producer["id"], retained["result"])
    body = launch_body(
        slug="use-it", result_attachments=_attach(retained["result"], producer["id"])
    )

    preview = client.post("/api/tasks/preview", headers=auth_headers, json=body)
    assert preview.status_code == 422
    assert "acknowledge_result_base_difference" in preview.text

    acknowledged = client.post(
        "/api/tasks/preview",
        headers=auth_headers,
        json={**body, "acknowledge_result_base_difference": True},
    )
    assert acknowledged.status_code == 200, acknowledged.text
    resolved = acknowledged.json()
    assert resolved["base_comparisons"][0]["state"] == "unknown"
    assert resolved["acknowledged_base_difference"] is True


def test_a_matching_base_needs_no_acknowledgement_and_pins_that_commit(
    client: TestClient, auth_headers: dict, producer: dict, git_checkout: Path
) -> None:
    head = _head(git_checkout)
    retained = _retain(client.app, producer["id"], merge_base=head)
    _accept(client, auth_headers, producer["id"], retained["result"])

    preview = client.post(
        "/api/tasks/preview",
        headers=auth_headers,
        json=launch_body(slug="use-it", result_attachments=_attach(retained["result"], producer["id"])),
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["needs_base_acknowledgement"] is False
    assert body["base_comparisons"][0]["state"] == "match"
    # The exact commit, not a branch name: this is what the clone is built from.
    assert body["source_commit"] == head


def test_an_acknowledgement_does_not_survive_a_changed_selection(
    client: TestClient, auth_headers: dict, producer: dict, git_checkout: Path
) -> None:
    """The token binds the acknowledgement to this attachment set. Reviewing
    one bundle and submitting another is a different launch."""
    head = _head(git_checkout)
    first = _retain(client.app, producer["id"], merge_base=None)
    _accept(client, auth_headers, producer["id"], first["result"])
    second = _retain(
        client.app, producer["id"], path="epics/demo/SPEC.md", body=b"# Spec\n",
        merge_base=head,
    )
    _accept(client, auth_headers, producer["id"], second["result"])

    reviewed = client.post(
        "/api/tasks/preview",
        headers=auth_headers,
        json=launch_body(
            slug="use-it",
            result_attachments=_attach(first["result"], producer["id"]),
            acknowledge_result_base_difference=True,
        ),
    )
    assert reviewed.status_code == 200
    token = reviewed.json()["preview_token"]

    swapped = client.post(
        "/api/tasks",
        headers=auth_headers,
        json=launch_body(
            slug="use-it",
            result_attachments=_attach(second["result"], producer["id"]),
            acknowledge_result_base_difference=True,
            preview_token=token,
        ),
    )
    assert swapped.status_code == 409
    assert "preview_changed" in swapped.text


# --- Destination conflicts ---------------------------------------------------


def test_a_destination_already_tracked_on_the_target_base_blocks_the_launch(
    client: TestClient, auth_headers: dict, producer: dict, git_checkout: Path
) -> None:
    """Even with identical bytes. A tracked path is one the recipient's
    ordinary Git result would carry, which is exactly what a handoff must never
    become."""
    tracked = git_checkout / "docs"
    tracked.mkdir(parents=True, exist_ok=True)
    (tracked / "PLAN.md").write_bytes(PLAN)
    subprocess.run(["git", "-C", str(git_checkout), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(git_checkout), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "track a plan"],
        check=True,
    )
    retained = _retain(
        client.app, producer["id"], path="docs/PLAN.md", merge_base=_head(git_checkout)
    )
    _accept(client, auth_headers, producer["id"], retained["result"])

    response = client.post(
        "/api/tasks/preview",
        headers=auth_headers,
        json=launch_body(slug="use-it", result_attachments=_attach(retained["result"], producer["id"])),
    )
    assert response.status_code == 422
    assert "already tracked on the target base" in response.text


# --- Materialization ---------------------------------------------------------


def test_a_consumer_starts_from_exactly_the_accepted_bytes(
    client: TestClient, auth_headers: dict, producer: dict, git_checkout: Path
) -> None:
    """The whole point. The bytes in the recipient's clone are the bytes the
    operator accepted, and the file is an ordinary non-executable regular
    file — not a link back to the producer's workspace or the daemon's store."""
    head = _head(git_checkout)
    retained = _retain(client.app, producer["id"], merge_base=head)
    _accept(client, auth_headers, producer["id"], retained["result"])

    response = spawn_task(
        client,
        auth_headers,
        slug="use-it",
        prompt="implement the plan",
        result_attachments=_attach(retained["result"], producer["id"]),
    )
    assert response.status_code == 202, response.text
    consumer = _wait_settled(client, auth_headers, response.json()["id"])
    assert consumer["state"] == "created", consumer.get("error")

    installed = Path(consumer["clone_path"]) / PLAN_PATH
    assert installed.read_bytes() == PLAN
    assert installed.is_file() and not installed.is_symlink()
    # And the task says what it ran with, in its own pinned document.
    attachments = consumer["execution_inputs"]["result_attachments"]
    assert [entry["result_id"] for entry in attachments] == [retained["result"].id]
    assert attachments[0]["classification"] == "handoff-input"
    assert consumer["execution_inputs"]["source_commit"] == head


def test_the_installed_handoff_is_excluded_from_ordinary_git_status(
    client: TestClient, auth_headers: dict, producer: dict, git_checkout: Path
) -> None:
    """A convenience, and stated as one: the exclude keeps the file out of
    `git add --all`. The guarantee lives at the delivery boundary, which reads
    the proposed tree rather than this file."""
    retained = _retain(client.app, producer["id"], merge_base=_head(git_checkout))
    _accept(client, auth_headers, producer["id"], retained["result"])
    response = spawn_task(
        client, auth_headers, slug="use-it", prompt="go",
        result_attachments=_attach(retained["result"], producer["id"]),
    )
    consumer = _wait_settled(client, auth_headers, response.json()["id"])

    status = subprocess.run(
        ["git", "-C", consumer["clone_path"], "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert PLAN_PATH not in status


def test_a_purge_is_refused_while_the_consumer_exists_and_names_it(
    client: TestClient, auth_headers: dict, producer: dict, git_checkout: Path
) -> None:
    retained = _retain(client.app, producer["id"], merge_base=_head(git_checkout))
    _accept(client, auth_headers, producer["id"], retained["result"])
    response = spawn_task(
        client, auth_headers, slug="use-it", prompt="go",
        result_attachments=_attach(retained["result"], producer["id"]),
    )
    consumer = _wait_settled(client, auth_headers, response.json()["id"])

    projection = client.get(
        f"/api/tasks/{producer['id']}/results", headers=auth_headers
    ).json()
    revision = projection["results"][0]
    assert revision["consumer_task_ids"] == [consumer["id"]]

    refused = client.request(
        "DELETE",
        f"/api/tasks/{producer['id']}/results/{revision['id']}",
        headers=auth_headers,
        json={
            "expected_manifest_id": revision["manifest_id"],
            "expected_version": projection["version"],
            "acknowledge_purge": True,
        },
    )
    assert refused.status_code == 409
    assert str(consumer["id"]) in refused.text
    # Nothing was deleted on the way to that refusal: the retained bytes are
    # still exactly readable.
    readable = client.get(
        f"/api/tasks/{producer['id']}/results/{revision['id']}/file",
        headers=auth_headers,
        params={"path": PLAN_PATH},
    )
    assert readable.status_code == 200, readable.text
    assert readable.json()["text"] == PLAN.decode("utf-8")


def test_a_prompt_may_mention_an_attached_destination(
    client: TestClient, auth_headers: dict, producer: dict, git_checkout: Path
) -> None:
    """The file is not on the base branch and does not exist anywhere yet — but
    the clone will hold it before the agent runs, so the mention resolves."""
    retained = _retain(client.app, producer["id"], merge_base=_head(git_checkout))
    _accept(client, auth_headers, producer["id"], retained["result"])
    response = spawn_task(
        client, auth_headers, slug="use-it", prompt=f"implement @{PLAN_PATH}",
        result_attachments=_attach(retained["result"], producer["id"]),
    )
    assert response.status_code == 202, response.text


def test_a_mention_of_neither_the_base_nor_an_attachment_is_still_refused(
    client: TestClient, auth_headers: dict, producer: dict, git_checkout: Path
) -> None:
    retained = _retain(client.app, producer["id"], merge_base=_head(git_checkout))
    _accept(client, auth_headers, producer["id"], retained["result"])
    body = launch_body(
        slug="use-it",
        prompt="implement @epics/demo/NOPE.md",
        result_attachments=_attach(retained["result"], producer["id"]),
    )
    preview = client.post("/api/tasks/preview", headers=auth_headers, json=body)
    assert preview.status_code == 200
    response = client.post(
        "/api/tasks",
        headers=auth_headers,
        json={**body, "preview_token": preview.json()["preview_token"]},
    )
    assert response.status_code == 422
    assert "mention rejected" in response.text
