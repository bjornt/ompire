"""Review registry tests: durable status, ordered iteration history, the
write-ahead process marker, and the cleanup/purge split.

The registry is the record; `ReviewManager` is only the process supervisor
(review capability; ADR-0016's review slice).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine

from ompire_daemon.registry.projects import create_project
from ompire_daemon.registry.reviews import (
    append_iteration,
    clear_process_marker,
    delete_review,
    get_review,
    list_interrupted_candidates,
    list_iterations,
    list_reviews,
    open_review,
    set_status,
)
from ompire_daemon.registry.tasks import create_task, mark_archived, purge_task


@pytest.fixture
def engine_task(app, git_checkout: Path, tmp_path: Path) -> tuple[Engine, int]:
    engine = app.state.engine
    project = create_project(
        engine,
        name="demo",
        title="Demo",
        upstream_url="https://example.com/demo.git",
        checkout_path=str(git_checkout),
        default_checkout_root=git_checkout.parent,
    )
    task = create_task(
        engine,
        project_name=project.name,
        slug="task1",
        branch="ompire/task1",
        clone_path=str(tmp_path / "tasks" / "task1"),
        prompt="hello",
    )
    return engine, task.id


def test_no_review_row_until_started(engine_task) -> None:
    engine, task_id = engine_task
    assert get_review(engine, task_id) is None
    assert list_reviews(engine) == []


def test_open_review_stamps_process_marker(engine_task) -> None:
    engine, task_id = engine_task

    record = open_review(engine, task_id)

    assert record.status == "open"
    assert record.process_started_at is not None
    assert record.iterations == []


def test_clear_process_marker_leaves_status(engine_task) -> None:
    engine, task_id = engine_task
    open_review(engine, task_id)

    clear_process_marker(engine, task_id)

    record = get_review(engine, task_id)
    assert record is not None
    assert record.status == "open"
    assert record.process_started_at is None


def test_iterations_are_ordered_and_re_review_appends_to_one_history(engine_task) -> None:
    """Re-review after comments reopens the same row: the loop stays one
    review with one ordered history, not a sequence of unrelated reviews."""
    engine, task_id = engine_task

    open_review(engine, task_id)
    append_iteration(engine, task_id, outcome="comments", comment_count=2)
    clear_process_marker(engine, task_id)
    # Second pass.
    open_review(engine, task_id)
    append_iteration(engine, task_id, outcome="approved", comment_count=0, status="approved")

    record = get_review(engine, task_id)
    assert record is not None
    assert record.status == "approved"
    assert [(it.seq, it.outcome) for it in record.iterations] == [
        (1, "comments"),
        (2, "approved"),
    ]
    assert record.iterations[0].comment_count == 2


def test_append_iteration_lands_status_in_one_transaction(engine_task) -> None:
    engine, task_id = engine_task
    open_review(engine, task_id)

    append_iteration(engine, task_id, outcome="error", stderr="boom", status="error")

    record = get_review(engine, task_id)
    assert record is not None
    assert record.status == "error"
    assert record.iterations[-1].stderr == "boom"


def test_set_status_without_iteration(engine_task) -> None:
    engine, task_id = engine_task
    open_review(engine, task_id)

    record = set_status(engine, task_id, "aborted")

    assert record is not None
    assert record.status == "aborted"


def test_interrupted_candidates_exclude_cleared_marker(engine_task) -> None:
    """A review left `open` because its comments went back to the agent has a
    cleared marker, so it is not a restart casualty."""
    engine, task_id = engine_task
    open_review(engine, task_id)
    append_iteration(engine, task_id, outcome="comments", comment_count=1)
    clear_process_marker(engine, task_id)

    assert list_interrupted_candidates(engine) == []


def test_interrupted_candidates_include_live_marker(engine_task) -> None:
    engine, task_id = engine_task
    open_review(engine, task_id)

    candidates = list_interrupted_candidates(engine)

    assert [c.task_id for c in candidates] == [task_id]


def test_interrupted_candidates_exclude_terminal_reviews(engine_task) -> None:
    engine, task_id = engine_task
    open_review(engine, task_id)
    append_iteration(engine, task_id, outcome="approved", status="approved")

    assert list_interrupted_candidates(engine) == []


def test_delete_review_removes_both_tables(engine_task) -> None:
    engine, task_id = engine_task
    open_review(engine, task_id)
    append_iteration(engine, task_id, outcome="approved", status="approved")

    delete_review(engine, task_id)

    assert get_review(engine, task_id) is None
    assert list_iterations(engine, task_id) == []


def test_purge_deletes_review_history(engine_task) -> None:
    engine, task_id = engine_task
    open_review(engine, task_id)
    append_iteration(engine, task_id, outcome="approved", status="approved")
    mark_archived(engine, task_id)

    purge_task(engine, task_id)

    assert get_review(engine, task_id) is None
    assert list_iterations(engine, task_id) == []


def test_archive_retains_review_history(engine_task) -> None:
    """Cleanup archives the task; the review evidence explaining why it was
    allowed to publish stays (`VISION.md` principle 4)."""
    engine, task_id = engine_task
    open_review(engine, task_id)
    append_iteration(engine, task_id, outcome="approved", status="approved")

    mark_archived(engine, task_id)

    record = get_review(engine, task_id)
    assert record is not None
    assert record.status == "approved"
    assert len(record.iterations) == 1
