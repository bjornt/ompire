"""Tests for retained inspection, acceptance, download, and result purge.

The theme is that everything after capture addresses *retained bytes* and
checks them: a damaged store is reported as damaged rather than silently
substituted, a decision names one exact revision, and a purge is a deliberate,
bounded, idempotent removal that leaves a readable record behind.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest
from sqlalchemy import text

from ompire_daemon.config import Config
from ompire_daemon.db import (
    db_path_for,
    ensure_db_dir,
    make_engine,
    task_result_files,
)
from ompire_daemon.events import EventHub
from ompire_daemon.isolation import WorkspaceGuard
from ompire_daemon.migrate import upgrade_head
from ompire_daemon.registry.results import (
    RESERVED_MANIFEST_NAME,
    STATE_PURGED,
    ResultPurgedError,
    ResultStateError,
    StaleRevisionError,
    get_result,
    list_results,
    results_version,
)
from ompire_daemon.results import ResultManager, ResultUnavailableError
from ompire_daemon.work.projects import create_project
from ompire_daemon.work.tasks import create_task
from tests.conftest import make_execution_inputs


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    clone = tmp_path / "tasks" / "demo" / "task-1"
    (clone / "epics" / "demo").mkdir(parents=True)
    (clone / "epics" / "demo" / "PLAN.md").write_text("# Plan\nfirst\n")
    return clone


@pytest.fixture
def engine(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_path_for(data_dir)
    ensure_db_dir(db_path)
    upgrade_head(db_path)
    return make_engine(db_path)


@pytest.fixture
def task(engine, tmp_path: Path, workspace: Path):
    checkout = tmp_path / "proj" / "demo"
    checkout.mkdir(parents=True, exist_ok=True)
    create_project(
        engine,
        name="demo",
        title="Demo",
        upstream_url="https://github.com/owner/repo",
        fork_url=None,
        checkout_path=str(checkout),
        default_checkout_root=tmp_path / "proj",
    )
    return create_task(
        engine,
        project_name="demo",
        slug="task-1",
        branch="ompire/task-1",
        clone_path=str(workspace),
        prompt="explore",
        execution_inputs=make_execution_inputs(
            checkout_path=str(checkout),
            project_name="demo",
            branch="ompire/task-1",
        ),
    )


@pytest.fixture
def manager(engine, tmp_path: Path):
    config = Config(data_dir=tmp_path / "data", task_dir_root=tmp_path / "tasks")
    return ResultManager(config, engine, EventHub(), WorkspaceGuard())


async def _capture(manager, task, paths=("epics/demo",), request_id="req-1"):
    await manager.capture(task.id, paths=list(paths), request_id=request_id)
    for job in list(manager._jobs):
        await asyncio.gather(job, return_exceptions=True)
    return list_results(manager._engine, task.id)[0]


# --- Content is the retained bytes, verified ---------------------------------


@pytest.mark.asyncio
async def test_content_comes_from_the_store_not_the_workspace(
    manager, task, workspace
) -> None:
    result = await _capture(manager, task)
    (workspace / "epics" / "demo" / "PLAN.md").write_text("# Edited later\n")

    data, entry = manager.read_file(result, "epics/demo/PLAN.md")

    assert data == b"# Plan\nfirst\n"
    assert entry.sha256 == hashlib.sha256(b"# Plan\nfirst\n").hexdigest()


@pytest.mark.asyncio
async def test_a_corrupted_file_makes_the_revision_unavailable(
    manager, engine, task
) -> None:
    """Damage is classified and recorded, and the revision keeps its history —
    it is never silently repaired from the current workspace."""
    result = await _capture(manager, task)
    manager.accept(result, expected_manifest_id=result.manifest_id or "")
    with engine.begin() as conn:
        # Same length, different bytes: the checksum is what catches this, and
        # a length-only check would serve the substitution as genuine.
        conn.execute(
            task_result_files.update()
            .where(task_result_files.c.result_id == result.id)
            .values(content=b"# Plan\nWRONG\n")
        )

    with pytest.raises(ResultUnavailableError) as exc:
        manager.read_file(get_result(engine, result.id), "epics/demo/PLAN.md")

    assert "checksum" in exc.value.reason
    damaged = get_result(engine, result.id)
    assert not damaged.available
    assert damaged.accepted_at is not None


@pytest.mark.asyncio
async def test_a_missing_file_makes_the_revision_unavailable(
    manager, engine, task
) -> None:
    result = await _capture(manager, task)
    with engine.begin() as conn:
        conn.execute(
            task_result_files.delete().where(
                task_result_files.c.result_id == result.id
            )
        )

    with pytest.raises(ResultUnavailableError):
        manager.read_bundle(get_result(engine, result.id))


@pytest.mark.asyncio
async def test_a_file_not_in_the_manifest_is_not_served(manager, engine, task) -> None:
    """The manifest is the allowlist. A row that appeared beside it is an
    integrity failure, not an extra file to hand out."""
    result = await _capture(manager, task)
    with engine.begin() as conn:
        conn.execute(
            task_result_files.insert().values(
                result_id=result.id,
                relative_path="epics/demo/EXTRA.md",
                content=b"not described",
            )
        )

    with pytest.raises(ResultUnavailableError):
        manager.read_bundle(get_result(engine, result.id))


# --- Acceptance ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_acceptance_verifies_the_whole_bundle_first(
    manager, engine, task
) -> None:
    result = await _capture(manager, task)
    with engine.begin() as conn:
        conn.execute(
            task_result_files.update()
            .where(task_result_files.c.result_id == result.id)
            .values(content=b"tampered")
        )

    with pytest.raises(ResultUnavailableError):
        manager.accept(
            get_result(engine, result.id),
            expected_manifest_id=result.manifest_id or "",
        )

    assert get_result(engine, result.id).accepted_at is None


@pytest.mark.asyncio
async def test_acceptance_of_a_stale_revision_is_refused(manager, task) -> None:
    result = await _capture(manager, task)

    with pytest.raises(StaleRevisionError):
        manager.accept(result, expected_manifest_id="stale-identity")


# --- Comparison ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_successor_reports_added_changed_and_omitted_paths(
    manager, task, workspace
) -> None:
    await _capture(manager, task, request_id="req-1")
    (workspace / "epics" / "demo" / "PLAN.md").write_text("# Plan\nsecond\n")
    (workspace / "epics" / "demo" / "NOTES.md").write_text("# Notes\n")
    successor = await _capture(manager, task, request_id="req-2")

    diff = manager.diff(successor)

    assert diff["added"] == ["epics/demo/NOTES.md"]
    assert diff["changed"] == ["epics/demo/PLAN.md"]
    assert diff["omitted"] == []
    assert "-first" in diff["text"]
    assert "+second" in diff["text"]
    assert not diff["truncated"]


@pytest.mark.asyncio
async def test_an_omitted_path_is_a_bundle_difference(
    manager, task, workspace
) -> None:
    (workspace / "epics" / "demo" / "GONE.md").write_text("# Gone\n")
    await _capture(manager, task, request_id="req-1")
    (workspace / "epics" / "demo" / "GONE.md").unlink()
    successor = await _capture(manager, task, request_id="req-2")

    assert manager.diff(successor)["omitted"] == ["epics/demo/GONE.md"]


@pytest.mark.asyncio
async def test_a_purged_predecessor_is_named_not_treated_as_empty(
    manager, engine, task, workspace
) -> None:
    first = await _capture(manager, task, request_id="req-1")
    (workspace / "epics" / "demo" / "PLAN.md").write_text("# Plan\nsecond\n")
    second = await _capture(manager, task, request_id="req-2")
    manager.purge(
        first,
        expected_manifest_id=first.manifest_id or "",
        expected_version=results_version(engine, task.id),
    )

    diff = manager.diff(get_result(engine, second.id))

    assert not diff["predecessor_available"]
    assert "purged" in diff["predecessor_reason"]
    # Emphatically not "everything was added": that would be a comparison
    # against a revision nobody can read.
    assert diff["added"] == []
    assert diff["text"] == ""


@pytest.mark.asyncio
async def test_the_first_revision_has_no_predecessor(manager, task) -> None:
    result = await _capture(manager, task)

    diff = manager.diff(result)

    assert diff["predecessor_id"] is None
    assert "first captured revision" in diff["predecessor_reason"]


# --- Download -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_zip_carries_relative_paths_bytes_and_a_manifest(
    manager, task
) -> None:
    result = await _capture(manager, task)

    payload = manager.zip_bundle(result)

    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert archive.namelist() == [
            "epics/demo/PLAN.md",
            RESERVED_MANIFEST_NAME,
        ]
        assert archive.read("epics/demo/PLAN.md") == b"# Plan\nfirst\n"
        manifest = json.loads(archive.read(RESERVED_MANIFEST_NAME))
        assert manifest["result_id"] == result.id
        assert manifest["files"][0]["sha256"] == hashlib.sha256(
            b"# Plan\nfirst\n"
        ).hexdigest()
        # Nothing downloaded from a result is executable.
        for info in archive.infolist():
            assert (info.external_attr >> 16) & 0o111 == 0


@pytest.mark.asyncio
async def test_an_unavailable_file_fails_the_whole_archive(
    manager, engine, task
) -> None:
    """A partial archive would look like the complete result to whoever opens
    it later."""
    result = await _capture(manager, task)
    with engine.begin() as conn:
        conn.execute(
            task_result_files.delete().where(
                task_result_files.c.result_id == result.id
            )
        )

    with pytest.raises(ResultUnavailableError):
        manager.zip_bundle(get_result(engine, result.id))


# --- Purge ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_purge_leaves_a_readable_tombstone(manager, engine, task) -> None:
    result = await _capture(manager, task)
    manager.accept(result, expected_manifest_id=result.manifest_id or "")
    accepted = get_result(engine, result.id)

    manager.purge(
        accepted,
        expected_manifest_id=accepted.manifest_id or "",
        expected_version=results_version(engine, task.id),
    )

    tomb = get_result(engine, result.id)
    assert tomb.state == STATE_PURGED
    assert tomb.accepted_at == accepted.accepted_at
    assert tomb.purged_by == "operator"
    payload = manager.projection(task.id)["results"][0]
    # The record still describes what it was; the bytes are simply gone.
    assert payload["file_count"] == 1
    assert payload["purged_at"] is not None
    with pytest.raises(ResultPurgedError):
        manager.read_file(tomb, "epics/demo/PLAN.md")


@pytest.mark.asyncio
async def test_purge_of_an_unaccepted_revision_is_allowed(
    manager, engine, task
) -> None:
    result = await _capture(manager, task)

    manager.purge(
        result,
        expected_manifest_id=result.manifest_id or "",
        expected_version=results_version(engine, task.id),
    )

    assert get_result(engine, result.id).state == STATE_PURGED


@pytest.mark.asyncio
async def test_an_in_flight_capture_cannot_be_purged(manager, engine, task) -> None:
    await manager.capture(task.id, paths=["epics/demo"], request_id="req-1")
    pending = list_results(engine, task.id)[0]

    with pytest.raises(ResultStateError):
        manager.purge(
            pending, expected_manifest_id="", expected_version=results_version(
                engine, task.id
            )
        )

    for job in list(manager._jobs):
        await asyncio.gather(job, return_exceptions=True)


@pytest.mark.asyncio
async def test_acceptance_racing_a_purge_needs_refreshed_review(
    manager, engine, task
) -> None:
    """Whichever lands first wins, and the loser is told to look again rather
    than being applied to a revision the operator did not review."""
    result = await _capture(manager, task)
    manager.purge(
        result,
        expected_manifest_id=result.manifest_id or "",
        expected_version=results_version(engine, task.id),
    )

    with pytest.raises(ResultPurgedError):
        manager.accept(
            get_result(engine, result.id),
            expected_manifest_id=result.manifest_id or "",
        )


@pytest.mark.asyncio
async def test_a_purge_does_not_disturb_other_revisions(
    manager, engine, task, workspace
) -> None:
    first = await _capture(manager, task, request_id="req-1")
    (workspace / "epics" / "demo" / "PLAN.md").write_text("# Plan\nsecond\n")
    second = await _capture(manager, task, request_id="req-2")
    manager.accept(second, expected_manifest_id=second.manifest_id or "")

    manager.purge(
        first,
        expected_manifest_id=first.manifest_id or "",
        expected_version=results_version(engine, task.id),
    )

    kept = get_result(engine, second.id)
    assert kept.available
    assert manager.read_bundle(kept)["epics/demo/PLAN.md"] == b"# Plan\nsecond\n"


@pytest.mark.asyncio
async def test_projection_reports_a_damaged_manifest_without_listing_files(
    manager, engine, task
) -> None:
    result = await _capture(manager, task)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE task_results SET manifest_json = :doc WHERE id = :id"),
            {"doc": '{"files": "not a list"}', "id": result.id},
        )

    payload = manager.projection(task.id)["results"][0]

    assert payload["files"] == []
    assert not payload["available"]
    assert "unreadable" in payload["unavailable_reason"]
