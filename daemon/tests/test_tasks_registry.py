"""Registry-level tests for the crash-recovery capability's session-id
persistence and startup reconciliation matrix (`reconcile_startup`)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine

from ompire_daemon.db import make_engine
from ompire_daemon.migrate import upgrade_head
from ompire_daemon.registry.projects import create_project
from ompire_daemon.registry.tasks import (
    Task,
    create_task,
    get_task,
    mark_session_id,
    mark_spawn_completed,
    reconcile_startup,
)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    db_path = tmp_path / "ompire.db"
    upgrade_head(db_path)
    return make_engine(db_path)


@pytest.fixture
def project(engine: Engine, tmp_path: Path):  # noqa: ANN001, ANN201
    return create_project(
        engine,
        name="demo",
        title="Demo",
        upstream_url="https://example.com/demo.git",
        checkout_path=str(tmp_path / "checkout"),
        default_checkout_root=tmp_path,
    )


def _make_task(engine: Engine, project, tmp_path: Path, slug: str) -> Task:  # noqa: ANN001
    return create_task(
        engine,
        project_name=project.name,
        slug=slug,
        branch=f"ompire/{slug}",
        clone_path=str(tmp_path / "tasks" / slug),
        prompt="fix it",
    )


def test_session_id_round_trips(engine: Engine, project, tmp_path: Path) -> None:  # noqa: ANN001
    task = _make_task(engine, project, tmp_path, "fix-bug")
    assert task.session_id is None

    updated = mark_session_id(engine, task.id, "abc-123")
    assert updated.session_id == "abc-123"
    assert get_task(engine, task.id).session_id == "abc-123"


def test_reconcile_startup_fails_interrupted_spawn(
    engine: Engine, project, tmp_path: Path  # noqa: ANN001
) -> None:
    task = _make_task(engine, project, tmp_path, "never-spawned")

    failed, candidates = reconcile_startup(engine)

    assert [t.id for t in failed] == [task.id]
    assert candidates == []
    refreshed = get_task(engine, task.id)
    assert refreshed.state == "failed"
    assert "restarted" in (refreshed.error or "")


def test_reconcile_startup_fails_missing_session_id(
    engine: Engine, project, tmp_path: Path  # noqa: ANN001
) -> None:
    task = _make_task(engine, project, tmp_path, "no-session")
    mark_spawn_completed(engine, task.id)

    failed, candidates = reconcile_startup(engine)

    assert [t.id for t in failed] == [task.id]
    assert candidates == []
    refreshed = get_task(engine, task.id)
    assert refreshed.state == "failed"
    assert "session id" in (refreshed.error or "")


def test_reconcile_startup_returns_recoverable_candidate(
    engine: Engine, project, tmp_path: Path  # noqa: ANN001
) -> None:
    task = _make_task(engine, project, tmp_path, "recoverable")
    mark_spawn_completed(engine, task.id)
    mark_session_id(engine, task.id, "sess-xyz")

    failed, candidates = reconcile_startup(engine)

    assert failed == []
    assert [t.id for t in candidates] == [task.id]
    assert candidates[0].session_id == "sess-xyz"
    # Not failed — still `created`, waiting on the caller's container check.
    assert get_task(engine, task.id).state == "created"


def test_reconcile_startup_leaves_failed_and_archived_alone(
    engine: Engine, project, tmp_path: Path  # noqa: ANN001
) -> None:
    from ompire_daemon.registry.tasks import mark_archived, mark_failed

    already_failed = _make_task(engine, project, tmp_path, "already-failed")
    mark_failed(engine, already_failed.id, "some earlier failure")

    archived = _make_task(engine, project, tmp_path, "archived")
    mark_spawn_completed(engine, archived.id)
    mark_archived(engine, archived.id)

    failed, candidates = reconcile_startup(engine)

    assert failed == []
    assert candidates == []
    assert get_task(engine, already_failed.id).error == "some earlier failure"
    assert get_task(engine, archived.id).state == "archived"
