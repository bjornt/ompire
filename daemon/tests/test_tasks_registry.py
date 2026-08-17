"""Registry-level tests for per-session identity (`task_sessions`), workflow
step records (`workflow_step_records`), and the startup reconciliation matrix
(`reconcile_startup`)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine

from ompire_daemon.db import make_engine
from ompire_daemon.migrate import upgrade_head
from ompire_daemon.registry.projects import create_project
from ompire_daemon.registry.sessions import (
    list_resumable_sessions,
    list_sessions,
    mark_session_id,
    record_session_spawned,
)
from ompire_daemon.registry.tasks import (
    Task,
    create_task,
    get_task,
    mark_spawn_completed,
    reconcile_startup,
)
from ompire_daemon.registry.workflows import (
    append_step_record,
    finish_step_record,
    list_step_records,
    mark_prompt_sent,
    set_run_status,
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


def test_task_carries_workflow_fields(engine: Engine, project, tmp_path: Path) -> None:  # noqa: ANN001
    task = _make_task(engine, project, tmp_path, "fix-bug")
    assert task.workflow_name == "single-step"
    assert task.workflow_status is None
    assert task.workflow_step is None

    updated = set_run_status(engine, task.id, "running", "work")
    assert updated.workflow_status == "running"
    assert updated.workflow_step == "work"
    assert get_task(engine, task.id).workflow_status == "running"


def test_session_rows_round_trip(engine: Engine, project, tmp_path: Path) -> None:  # noqa: ANN001
    task = _make_task(engine, project, tmp_path, "fix-bug")

    session = record_session_spawned(engine, task.id, "main")
    assert session.omp_session_id is None
    # Lazy spawn must not have happened yet: nothing resumable.
    assert list_resumable_sessions(engine, task.id) == []

    updated = mark_session_id(engine, task.id, "main", "abc-123")
    assert updated.omp_session_id == "abc-123"
    assert [s.omp_session_id for s in list_resumable_sessions(engine, task.id)] == ["abc-123"]

    other = record_session_spawned(engine, task.id, "reproducer")
    assert other.name == "reproducer"
    assert [s.name for s in list_sessions(engine, task.id)] == ["main", "reproducer"]

    # Idempotent re-spawn keeps the captured identity.
    again = record_session_spawned(engine, task.id, "main")
    assert again.omp_session_id == "abc-123"


def test_step_records_round_trip(engine: Engine, project, tmp_path: Path) -> None:  # noqa: ANN001
    task = _make_task(engine, project, tmp_path, "fix-bug")

    first = append_step_record(engine, task.id, step="work", kind="agent", session="main")
    assert first.seq == 1
    assert first.status == "running"
    assert first.finished_at is None
    assert first.prompted_at is None

    mark_prompt_sent(engine, task.id, first.seq)
    assert list_step_records(engine, task.id)[0].prompted_at is not None

    finished = finish_step_record(
        engine,
        task.id,
        first.seq,
        status="ok",
        outcome={"status": "success", "summary": "done"},
    )
    assert finished.status == "ok"
    assert finished.outcome == {"status": "success", "summary": "done"}
    assert finished.finished_at is not None

    second = append_step_record(engine, task.id, step="validate", kind="command")
    assert second.seq == 2
    records = list_step_records(engine, task.id)
    assert [r.step for r in records] == ["work", "validate"]
    assert [r.kind for r in records] == ["agent", "command"]
    # History rows are independent: the first record keeps its outcome.
    assert records[0].outcome is not None
    assert records[1].outcome is None


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


def test_reconcile_startup_missing_session_is_not_fatal(
    engine: Engine, project, tmp_path: Path  # noqa: ANN001
) -> None:
    """Sessions are lazily spawned (workflow-engine design D-6): a
    spawn-completed task with no recorded session identity (a command-only
    workflow, or a run that failed before its first agent step) is a
    recovery candidate, not a failure."""
    task = _make_task(engine, project, tmp_path, "no-session")
    mark_spawn_completed(engine, task.id)

    failed, candidates = reconcile_startup(engine)

    assert failed == []
    assert [t.id for t in candidates] == [task.id]
    assert get_task(engine, task.id).state == "created"


def test_reconcile_startup_returns_recoverable_candidate(
    engine: Engine, project, tmp_path: Path  # noqa: ANN001
) -> None:
    task = _make_task(engine, project, tmp_path, "recoverable")
    mark_spawn_completed(engine, task.id)
    record_session_spawned(engine, task.id, "main")
    mark_session_id(engine, task.id, "main", "sess-xyz")

    failed, candidates = reconcile_startup(engine)

    assert failed == []
    assert [t.id for t in candidates] == [task.id]
    assert [s.omp_session_id for s in list_resumable_sessions(engine, task.id)] == ["sess-xyz"]
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
