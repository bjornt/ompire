"""Tests for `ompire_daemon.registry.results`: the durable result contract.

These are the invariants everything above this layer stands on — a revision's
identity is what acceptance binds to, a replayed request is not a second
bundle, bytes and `ready` land together, and an operator's decisions are never
rewritten by a later capture. They run against the registry directly, because
each one has to hold for a direct service caller and not only for a request
that happened to arrive over REST.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from ompire_daemon.db import (
    db_path_for,
    ensure_db_dir,
    make_engine,
    task_result_files,
    task_results,
)
from ompire_daemon.migrate import upgrade_head
from ompire_daemon.registry.projects import create_project
from ompire_daemon.registry.results import (
    STATE_FAILED,
    STATE_PURGED,
    STATE_READY,
    CaptureInProgressError,
    DamagedManifestError,
    InvalidSelectionError,
    ResultFile,
    ResultPurgedError,
    ResultsRetainedError,
    ResultStateError,
    SelectionMismatchError,
    StaleRevisionError,
    accept_result,
    assert_no_retained_results,
    build_manifest,
    content_identity,
    fail_capture,
    finish_capture,
    get_result,
    list_results,
    manifest_files,
    manifest_identity,
    mark_unavailable,
    normalize_selection,
    open_capture,
    purge_result,
    read_all_files,
    reconcile_interrupted_captures,
    results_version,
    retained_counts,
)
from ompire_daemon.registry.tasks import create_task, mark_archived
from tests.conftest import make_execution_inputs


@pytest.fixture
def engine(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_path_for(data_dir)
    ensure_db_dir(db_path)
    upgrade_head(db_path)
    return make_engine(db_path)


@pytest.fixture
def task(engine, tmp_path: Path):
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
        clone_path=str(tmp_path / "tasks" / "demo" / "task-1"),
        prompt="explore the idea",
        execution_inputs=make_execution_inputs(
            checkout_path=str(checkout),
            project_name="demo",
            branch="ompire/task-1",
        ),
    )


def _capture(engine, task, *, request_id: str, body: bytes = b"# Plan\n"):
    """Open, fill, and finish one capture the way the service does."""
    selection = normalize_selection(["epics/demo"])
    result, created = open_capture(
        engine, task_id=task.id, request_id=request_id, selection=selection
    )
    assert created
    entry = ResultFile(
        path="epics/demo/PLAN.md",
        length=len(body),
        sha256=__import__("hashlib").sha256(body).hexdigest(),
        media_type="text/markdown",
    )
    manifest = build_manifest(
        result_id=result.id,
        task_id=task.id,
        project_name=task.project_name,
        selection=selection,
        files=[entry],
        predecessor_id=result.predecessor_id,
        provenance={"producing_step": "unknown"},
        captured_at=result.started_at,
    )
    return finish_capture(
        engine, result.id, manifest=manifest, contents={entry.path: body}
    )


# --- Selection syntax -------------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    [
        "/etc/passwd",
        "../outside",
        "epics/../../etc",
        "epics/./demo",
        ".git/config",
        "epics/.ompire/state.json",
        "epics\\demo",
        "epics/demo\x00.md",
        "",
        "   ",
        "a/" * 20 + "deep.md",
        "epics/__ompire_result_manifest__.json",
    ],
)
def test_ineligible_selection_entries_are_refused(entry: str) -> None:
    with pytest.raises(InvalidSelectionError) as exc:
        normalize_selection([entry])
    # The refusal names the entry and says why, and never carries content.
    assert exc.value.reason


def test_overlapping_selection_is_deduplicated_and_ordered() -> None:
    assert normalize_selection(["b/x.md", "a", "b/x.md"]) == ("a", "b/x.md")


def test_empty_selection_is_refused() -> None:
    with pytest.raises(InvalidSelectionError):
        normalize_selection([])


# --- Identity ---------------------------------------------------------------


def test_equal_bytes_keep_separate_capture_identities(engine, task) -> None:
    first = _capture(engine, task, request_id="req-1")
    second = _capture(engine, task, request_id="req-2")

    assert first.id != second.id
    # Same files: recognizable as unchanged content...
    assert first.content_id == second.content_id
    # ...and still two distinct revisions, because they are two captures with
    # two capture times and two provenances.
    assert first.manifest_id != second.manifest_id
    assert second.predecessor_id == first.id


def test_manifest_identity_covers_the_whole_manifest(engine, task) -> None:
    result = _capture(engine, task, request_id="req-1")
    assert result.manifest is not None
    assert manifest_identity(result.manifest) == result.manifest_id

    altered = dict(result.manifest)
    altered["provenance"] = {"producing_step": "invented"}
    assert manifest_identity(altered) != result.manifest_id


def test_content_identity_ignores_capture_time_only(engine, task) -> None:
    entry = ResultFile("a.md", 3, "abc", "text/markdown")
    other = ResultFile("a.md", 4, "abc", "text/markdown")
    assert content_identity([entry]) != content_identity([other])


# --- Replay and concurrency -------------------------------------------------


def test_same_request_id_replays_the_original_operation(engine, task) -> None:
    original = _capture(engine, task, request_id="req-1")

    replayed, created = open_capture(
        engine,
        task_id=task.id,
        request_id="req-1",
        selection=normalize_selection(["epics/demo"]),
    )

    assert not created
    assert replayed.id == original.id
    assert len(list_results(engine, task.id)) == 1


def test_replayed_request_returns_the_original_failure(engine, task) -> None:
    """A lost response must not become a second attempt that captures
    different bytes — including when the first attempt failed."""
    opened, _ = open_capture(
        engine,
        task_id=task.id,
        request_id="req-1",
        selection=normalize_selection(["epics/demo"]),
    )
    fail_capture(engine, opened.id, "epics/demo: no such directory")

    replayed, created = open_capture(
        engine,
        task_id=task.id,
        request_id="req-1",
        selection=normalize_selection(["epics/demo"]),
    )

    assert not created
    assert replayed.state == STATE_FAILED
    assert replayed.error == "epics/demo: no such directory"


def test_same_request_id_with_a_different_selection_is_refused(engine, task) -> None:
    _capture(engine, task, request_id="req-1")

    with pytest.raises(SelectionMismatchError):
        open_capture(
            engine,
            task_id=task.id,
            request_id="req-1",
            selection=normalize_selection(["changes/other"]),
        )


def test_only_one_capture_per_task_is_in_flight(engine, task) -> None:
    open_capture(
        engine,
        task_id=task.id,
        request_id="req-1",
        selection=normalize_selection(["epics/demo"]),
    )

    with pytest.raises(CaptureInProgressError):
        open_capture(
            engine,
            task_id=task.id,
            request_id="req-2",
            selection=normalize_selection(["epics/demo"]),
        )


def test_failed_capture_is_never_a_predecessor(engine, task) -> None:
    opened, _ = open_capture(
        engine,
        task_id=task.id,
        request_id="req-1",
        selection=normalize_selection(["epics/demo"]),
    )
    fail_capture(engine, opened.id, "unsupported entry")

    second = _capture(engine, task, request_id="req-2")

    assert second.predecessor_id is None


# --- Atomicity --------------------------------------------------------------


def test_finish_rolls_back_bytes_when_the_manifest_is_inconsistent(
    engine, task
) -> None:
    """Bytes and `ready` commit together, so a rejected finish leaves neither."""
    result, _ = open_capture(
        engine,
        task_id=task.id,
        request_id="req-1",
        selection=normalize_selection(["epics/demo"]),
    )
    entry = ResultFile("epics/demo/PLAN.md", 3, "deadbeef", "text/markdown")
    manifest = build_manifest(
        result_id=result.id,
        task_id=task.id,
        project_name="demo",
        selection=["epics/demo"],
        files=[entry],
        predecessor_id=None,
        provenance={},
        captured_at=result.started_at,
    )

    with pytest.raises(ValueError):
        finish_capture(engine, result.id, manifest=manifest, contents={})

    assert read_all_files(engine, result.id) == {}
    assert get_result(engine, result.id).state == "capturing"


def test_a_capture_cannot_be_finished_twice(engine, task) -> None:
    result = _capture(engine, task, request_id="req-1")
    assert result.manifest is not None

    with pytest.raises(ResultStateError):
        finish_capture(
            engine,
            result.id,
            manifest=result.manifest,
            contents={"epics/demo/PLAN.md": b"# Plan\n"},
        )


# --- Acceptance -------------------------------------------------------------


def test_acceptance_is_bound_to_the_reviewed_manifest(engine, task) -> None:
    result = _capture(engine, task, request_id="req-1")

    with pytest.raises(StaleRevisionError):
        accept_result(engine, result.id, expected_manifest_id="not-the-one")

    accepted = accept_result(
        engine, result.id, expected_manifest_id=result.manifest_id or ""
    )
    assert accepted.accepted_by == "operator"
    assert accepted.accepted_at is not None


def test_repeating_acceptance_preserves_the_original_decision(engine, task) -> None:
    result = _capture(engine, task, request_id="req-1")
    first = accept_result(
        engine, result.id, expected_manifest_id=result.manifest_id or ""
    )

    again = accept_result(
        engine, result.id, expected_manifest_id=result.manifest_id or ""
    )

    assert again.accepted_at == first.accepted_at


def test_a_successor_leaves_earlier_decisions_untouched(engine, task) -> None:
    first = _capture(engine, task, request_id="req-1")
    accepted = accept_result(
        engine, first.id, expected_manifest_id=first.manifest_id or ""
    )
    second = _capture(engine, task, request_id="req-2", body=b"# Plan v2\n")
    accept_result(engine, second.id, expected_manifest_id=second.manifest_id or "")

    reread = get_result(engine, first.id)
    assert reread.accepted_at == accepted.accepted_at
    assert reread.manifest_id == first.manifest_id
    assert reread.state == STATE_READY


def test_unavailable_and_purged_revisions_cannot_be_accepted(engine, task) -> None:
    damaged = _capture(engine, task, request_id="req-1")
    mark_unavailable(engine, damaged.id, "a retained file is missing")
    with pytest.raises(ResultStateError):
        accept_result(
            engine, damaged.id, expected_manifest_id=damaged.manifest_id or ""
        )

    gone = _capture(engine, task, request_id="req-2")
    purge_result(
        engine,
        gone.id,
        expected_manifest_id=gone.manifest_id or "",
        expected_version=results_version(engine, task.id),
    )
    with pytest.raises(ResultPurgedError):
        accept_result(engine, gone.id, expected_manifest_id=gone.manifest_id or "")


def test_marking_unavailable_keeps_the_acceptance(engine, task) -> None:
    result = _capture(engine, task, request_id="req-1")
    accepted = accept_result(
        engine, result.id, expected_manifest_id=result.manifest_id or ""
    )

    damaged = mark_unavailable(engine, result.id, "checksum mismatch")

    assert damaged.accepted_at == accepted.accepted_at
    assert not damaged.available


# --- Purge ------------------------------------------------------------------


def test_purge_removes_bytes_and_keeps_the_tombstone(engine, task) -> None:
    result = _capture(engine, task, request_id="req-1")
    accept_result(engine, result.id, expected_manifest_id=result.manifest_id or "")

    tomb = purge_result(
        engine,
        result.id,
        expected_manifest_id=result.manifest_id or "",
        expected_version=results_version(engine, task.id),
    )

    assert tomb.state == STATE_PURGED
    assert tomb.purged_by == "operator"
    # The decision and the description survive; only the payload is gone.
    assert tomb.accepted_at is not None
    assert tomb.manifest_id == result.manifest_id
    assert read_all_files(engine, result.id) == {}


def test_purge_refuses_a_stale_task_result_version(engine, task) -> None:
    result = _capture(engine, task, request_id="req-1")
    stale_version = results_version(engine, task.id)
    _capture(engine, task, request_id="req-2")

    with pytest.raises(StaleRevisionError):
        purge_result(
            engine,
            result.id,
            expected_manifest_id=result.manifest_id or "",
            expected_version=stale_version,
        )


def test_repeated_purge_is_idempotent_and_cannot_hit_another_revision(
    engine, task
) -> None:
    first = _capture(engine, task, request_id="req-1")
    second = _capture(engine, task, request_id="req-2", body=b"# Keep me\n")
    purge_result(
        engine,
        first.id,
        expected_manifest_id=first.manifest_id or "",
        expected_version=results_version(engine, task.id),
    )

    # The retry carries the same expectations as the original request; the
    # completed purge answers it without touching anything else.
    again = purge_result(
        engine,
        first.id,
        expected_manifest_id=first.manifest_id or "",
        expected_version=0,
    )

    assert again.state == STATE_PURGED
    assert get_result(engine, second.id).state == STATE_READY
    assert read_all_files(engine, second.id)


# --- Restart recovery -------------------------------------------------------


def test_startup_turns_interrupted_captures_into_failures(engine, task) -> None:
    ready = _capture(engine, task, request_id="req-1")
    opened, _ = open_capture(
        engine,
        task_id=task.id,
        request_id="req-2",
        selection=normalize_selection(["epics/demo"]),
    )

    reconciled = reconcile_interrupted_captures(engine)

    assert [item.id for item in reconciled] == [opened.id]
    assert get_result(engine, opened.id).state == STATE_FAILED
    assert "restarted" in (get_result(engine, opened.id).error or "")
    # A committed result is untouched by recovery.
    assert get_result(engine, ready.id).state == STATE_READY


# --- Task purge boundary ----------------------------------------------------


def test_retained_results_block_task_purge_before_any_deletion(engine, task) -> None:
    result = _capture(engine, task, request_id="req-1")
    mark_archived(engine, task.id)

    with engine.connect() as conn, pytest.raises(ResultsRetainedError) as exc:
        assert_no_retained_results(conn, task.id)
    assert exc.value.result_ids == [result.id]


def test_purged_tombstones_do_not_block_task_purge(engine, task) -> None:
    result = _capture(engine, task, request_id="req-1")
    purge_result(
        engine,
        result.id,
        expected_manifest_id=result.manifest_id or "",
        expected_version=results_version(engine, task.id),
    )

    with engine.connect() as conn:
        assert_no_retained_results(conn, task.id)


# --- Projections ------------------------------------------------------------


def test_version_advances_on_every_observable_mutation(engine, task) -> None:
    versions = [results_version(engine, task.id)]
    result = _capture(engine, task, request_id="req-1")
    versions.append(results_version(engine, task.id))
    accept_result(engine, result.id, expected_manifest_id=result.manifest_id or "")
    versions.append(results_version(engine, task.id))
    purge_result(
        engine,
        result.id,
        expected_manifest_id=result.manifest_id or "",
        expected_version=results_version(engine, task.id),
    )
    versions.append(results_version(engine, task.id))

    assert versions == sorted(set(versions))
    assert versions[0] == 0


def test_metadata_reads_never_load_file_bytes(engine, task) -> None:
    _capture(engine, task, request_id="req-1", body=b"x" * 4096)

    listed = list_results(engine, task.id)

    assert len(listed) == 1
    # The projection dataclass has no content field at all; the bytes stay in
    # their own table until something explicitly asks for them.
    assert not hasattr(listed[0], "content")
    with engine.connect() as conn:
        stored = conn.execute(
            select(task_result_files.c.relative_path)
        ).scalars().all()
    assert stored == ["epics/demo/PLAN.md"]


def test_retained_counts_report_totals_per_task(engine, task) -> None:
    accepted = _capture(engine, task, request_id="req-1", body=b"# Plan\n")
    accept_result(engine, accepted.id, expected_manifest_id=accepted.manifest_id or "")
    _capture(engine, task, request_id="req-2", body=b"# Plan v2\n")

    counts = retained_counts(engine)[task.id]

    assert counts["total"] == 2
    assert counts["retained"] == 2
    assert counts["accepted"] == 1
    assert counts["bytes"] == len(b"# Plan\n") + len(b"# Plan v2\n")


def test_a_damaged_manifest_is_refused_rather_than_trusted(engine, task) -> None:
    result = _capture(engine, task, request_id="req-1")
    with engine.begin() as conn:
        conn.execute(
            task_results.update()
            .where(task_results.c.id == result.id)
            .values(manifest_json='{"files": [{"path": "../escape.md"}]}')
        )

    damaged = get_result(engine, result.id)
    with pytest.raises(DamagedManifestError):
        manifest_files(damaged.manifest)
