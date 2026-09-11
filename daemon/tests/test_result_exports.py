"""Tests for exporting a retained revision into the operator's checkout.

The theme is that this is the one place Ompire writes into a directory it does
not own (ADR-0036), so every test here is about a promise made to files that
were already there: nothing is replaced, an approval names one observation, an
interrupted operation is classified rather than repeated, and an effect nobody
can establish stays `unknown`.

The checkout under test is always a throwaway directory built by the fixture.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

import pytest

from ompire_daemon.config import Config
from ompire_daemon.db import db_path_for, ensure_db_dir, make_engine
from ompire_daemon.events import EventHub
from ompire_daemon.isolation import WorkspaceGuard
from ompire_daemon.migrate import upgrade_head
from ompire_daemon.registry.result_exports import (
    OUTCOME_CREATED,
    OUTCOME_IDENTICAL,
    OUTCOME_NOT_INSTALLED,
    OUTCOME_UNKNOWN,
    STATE_COMPLETED,
    STATE_INCOMPLETE,
    STATE_RUNNING,
    STATE_UNRESOLVED,
    CheckoutBusyError,
    ExportsActiveError,
    get_export,
    list_task_exports,
)
from ompire_daemon.registry.results import list_results, results_version
from ompire_daemon.result_exports import ExportError, ResultExportManager
from ompire_daemon.results import ResultManager
from ompire_daemon.work.projects import (
    DEFAULT_FETCH_REMOTE,
    create_project,
    update_project,
)
from ompire_daemon.work.tasks import (
    create_task,
    mark_archived,
    purge_task,
)
from tests.conftest import make_execution_inputs

PLAN = "# Plan\nfirst\n"
SPEC = "# Spec\nwhat changes\n"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    clone = tmp_path / "tasks" / "demo" / "task-1"
    (clone / "epics" / "demo").mkdir(parents=True)
    (clone / "epics" / "demo" / "PLAN.md").write_text(PLAN)
    (clone / "epics" / "demo" / "SPEC.md").write_text(SPEC)
    return clone


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A disposable registered checkout. Never a developer's own tree."""
    root = tmp_path / "proj" / "demo"
    root.mkdir(parents=True)
    (root / ".git").mkdir()
    return root


@pytest.fixture
def engine(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_path_for(data_dir)
    ensure_db_dir(db_path)
    upgrade_head(db_path)
    return make_engine(db_path)


@pytest.fixture
def task(engine, tmp_path: Path, workspace: Path, checkout: Path):
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
def config(tmp_path: Path) -> Config:
    return Config(data_dir=tmp_path / "data", task_dir_root=tmp_path / "tasks")


@pytest.fixture
def results(engine, config: Config) -> ResultManager:
    return ResultManager(config, engine, EventHub(), WorkspaceGuard())


@pytest.fixture
def exports(engine, config: Config, results: ResultManager) -> ResultExportManager:
    return ResultExportManager(config, engine, EventHub(), results)


async def _accepted(results: ResultManager, task):
    await results.capture(task.id, paths=["epics/demo"], request_id="req-1")
    for job in list(results._jobs):
        await asyncio.gather(job, return_exceptions=True)
    result = list_results(results._engine, task.id)[0]
    assert result.state == "ready", result
    results.accept(result, expected_manifest_id=result.manifest_id or "")
    return list_results(results._engine, task.id)[0]


async def _drain(exports: ResultExportManager) -> None:
    for job in list(exports._jobs):
        await asyncio.gather(job, return_exceptions=True)


async def _export(
    exports: ResultExportManager,
    task,
    result,
    *,
    paths=("epics/demo/PLAN.md", "epics/demo/SPEC.md"),
    prefix="",
    request_id="exp-1",
):
    """Preview, confirm the exact preview, and wait for the installation."""
    preview = exports.preview(
        task_id=task.id,
        result_id=result.id,
        expected_manifest_id=result.manifest_id or "",
        paths=list(paths),
        prefix=prefix,
    )
    record, created = await exports.start(
        task_id=task.id,
        result_id=result.id,
        expected_manifest_id=result.manifest_id or "",
        paths=list(paths),
        prefix=prefix,
        token=preview["preview_token"],
        request_id=request_id,
    )
    await _drain(exports)
    return preview, get_export(exports._engine, record.id), created


# --- The happy path ---------------------------------------------------------


@pytest.mark.asyncio
async def test_export_creates_the_reviewed_files_at_their_original_paths(
    results, exports, task, checkout
) -> None:
    result = await _accepted(results, task)

    preview, record, created = await _export(exports, task, result)

    assert created is True
    assert record.state == STATE_COMPLETED
    assert [entry["classification"] for entry in preview["preview"]["files"]] == [
        "create",
        "create",
    ]
    assert (checkout / "epics" / "demo" / "PLAN.md").read_text() == PLAN
    assert (checkout / "epics" / "demo" / "SPEC.md").read_text() == SPEC
    assert {entry.outcome for entry in record.files} == {OUTCOME_CREATED}
    # An ordinary, non-executable, owner-private copy — never the mode the
    # workspace happened to have.
    mode = (checkout / "epics" / "demo" / "PLAN.md").stat().st_mode & 0o777
    assert mode == 0o600
    assert record.created_directories == ("epics", "epics/demo")


@pytest.mark.asyncio
async def test_a_prefix_relocates_the_tree_without_renaming_files(
    results, exports, task, checkout
) -> None:
    result = await _accepted(results, task)

    _preview, record, _ = await _export(exports, task, result, prefix="handoffs")

    assert record.state == STATE_COMPLETED
    assert (checkout / "handoffs" / "epics" / "demo" / "PLAN.md").read_text() == PLAN
    assert not (checkout / "epics").exists()


@pytest.mark.asyncio
async def test_a_subset_exports_only_what_was_selected(
    results, exports, task, checkout
) -> None:
    result = await _accepted(results, task)

    preview, record, _ = await _export(
        exports, task, result, paths=("epics/demo/PLAN.md",)
    )

    assert record.state == STATE_COMPLETED
    assert preview["preview"]["omitted"] == ["epics/demo/SPEC.md"]
    assert (checkout / "epics" / "demo" / "PLAN.md").exists()
    assert not (checkout / "epics" / "demo" / "SPEC.md").exists()


@pytest.mark.asyncio
async def test_an_all_identical_selection_is_a_valid_no_write_export(
    results, exports, task, checkout
) -> None:
    """Not a refusal and not a rewrite: the files already say what the operator
    approved, so the export records that and touches nothing."""
    result = await _accepted(results, task)
    target = checkout / "epics" / "demo"
    target.mkdir(parents=True)
    (target / "PLAN.md").write_text(PLAN)
    (target / "SPEC.md").write_text(SPEC)
    before = [(path.stat().st_mtime_ns, path.stat().st_mode) for path in sorted(target.iterdir())]

    preview, record, _ = await _export(exports, task, result)

    assert [entry["classification"] for entry in preview["preview"]["files"]] == [
        "identical",
        "identical",
    ]
    assert record.state == STATE_COMPLETED
    assert {entry.outcome for entry in record.files} == {OUTCOME_IDENTICAL}
    after = [(path.stat().st_mtime_ns, path.stat().st_mode) for path in sorted(target.iterdir())]
    assert after == before


# --- Nothing is replaced ----------------------------------------------------


@pytest.mark.asyncio
async def test_a_differing_destination_is_a_conflict_that_blocks_confirmation(
    results, exports, task, checkout
) -> None:
    result = await _accepted(results, task)
    target = checkout / "epics" / "demo"
    target.mkdir(parents=True)
    (target / "PLAN.md").write_text("# Plan\nthe operator's own edit\n")

    preview = exports.preview(
        task_id=task.id,
        result_id=result.id,
        expected_manifest_id=result.manifest_id or "",
        paths=["epics/demo/PLAN.md", "epics/demo/SPEC.md"],
        prefix="",
    )

    assert preview["blocked"] is True
    classifications = {
        entry["path"]: entry["classification"] for entry in preview["preview"]["files"]
    }
    assert classifications["epics/demo/PLAN.md"] == "conflict"
    assert classifications["epics/demo/SPEC.md"] == "create"
    assert "the operator's own edit" in preview["diff"]

    with pytest.raises(ExportError) as exc:
        await exports.start(
            task_id=task.id,
            result_id=result.id,
            expected_manifest_id=result.manifest_id or "",
            paths=["epics/demo/PLAN.md", "epics/demo/SPEC.md"],
            prefix="",
            token=preview["preview_token"],
            request_id="exp-1",
        )

    assert exc.value.reason == "conflicts-selected"
    # Refused whole: the non-conflicting sibling is not delivered behind the
    # operator's back either.
    assert (target / "PLAN.md").read_text() == "# Plan\nthe operator's own edit\n"
    assert not (target / "SPEC.md").exists()
    assert list_task_exports(exports._engine, task.id) == []


@pytest.mark.asyncio
async def test_deselecting_the_conflict_lets_the_rest_export(
    results, exports, task, checkout
) -> None:
    result = await _accepted(results, task)
    target = checkout / "epics" / "demo"
    target.mkdir(parents=True)
    (target / "PLAN.md").write_text("# Plan\nthe operator's own edit\n")

    _preview, record, _ = await _export(
        exports, task, result, paths=("epics/demo/SPEC.md",)
    )

    assert record.state == STATE_COMPLETED
    assert (target / "PLAN.md").read_text() == "# Plan\nthe operator's own edit\n"
    assert (target / "SPEC.md").read_text() == SPEC


@pytest.mark.asyncio
async def test_a_file_that_appears_after_approval_is_never_replaced(
    results, exports, task, checkout
) -> None:
    """The race the create-only guarantee exists for. Approval said the
    destination was free; by installation time it is not, and the operator's
    file wins."""
    result = await _accepted(results, task)
    preview = exports.preview(
        task_id=task.id,
        result_id=result.id,
        expected_manifest_id=result.manifest_id or "",
        paths=["epics/demo/PLAN.md"],
        prefix="",
    )
    record, _ = await exports.start(
        task_id=task.id,
        result_id=result.id,
        expected_manifest_id=result.manifest_id or "",
        paths=["epics/demo/PLAN.md"],
        prefix="",
        token=preview["preview_token"],
        request_id="exp-1",
    )
    # Slip in between admission and the worker thread's rename.
    target = checkout / "epics" / "demo"
    target.mkdir(parents=True, exist_ok=True)
    (target / "PLAN.md").write_text("someone else got here first\n")
    await _drain(exports)

    settled = get_export(exports._engine, record.id)
    assert settled.state == STATE_INCOMPLETE
    assert settled.files[0].outcome == OUTCOME_NOT_INSTALLED
    assert (target / "PLAN.md").read_text() == "someone else got here first\n"


@pytest.mark.asyncio
async def test_a_symlinked_destination_is_a_conflict_and_is_never_followed(
    results, exports, task, checkout, tmp_path: Path
) -> None:
    result = await _accepted(results, task)
    outside = tmp_path / "outside.md"
    outside.write_text("untouched\n")
    target = checkout / "epics" / "demo"
    target.mkdir(parents=True)
    (target / "PLAN.md").symlink_to(outside)

    preview = exports.preview(
        task_id=task.id,
        result_id=result.id,
        expected_manifest_id=result.manifest_id or "",
        paths=["epics/demo/PLAN.md"],
        prefix="",
    )

    entry = preview["preview"]["files"][0]
    assert entry["classification"] == "conflict"
    assert entry["reason"] == "symlink-destination"
    assert outside.read_text() == "untouched\n"


@pytest.mark.asyncio
async def test_a_directory_where_a_file_belongs_is_a_conflict(
    results, exports, task, checkout
) -> None:
    result = await _accepted(results, task)
    (checkout / "epics" / "demo" / "PLAN.md").mkdir(parents=True)

    preview = exports.preview(
        task_id=task.id,
        result_id=result.id,
        expected_manifest_id=result.manifest_id or "",
        paths=["epics/demo/PLAN.md"],
        prefix="",
    )

    assert preview["preview"]["files"][0]["reason"] == "directory-destination"


@pytest.mark.asyncio
async def test_a_credential_bearing_destination_is_a_conflict_without_its_content(
    results, exports, task, checkout
) -> None:
    """A conflict diff would otherwise hand the operator's own secrets back to
    the browser. The conflict is reported; the content is not."""
    result = await _accepted(results, task)
    target = checkout / "epics" / "demo"
    target.mkdir(parents=True)
    (target / "PLAN.md").write_text(
        "-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n"
    )

    preview = exports.preview(
        task_id=task.id,
        result_id=result.id,
        expected_manifest_id=result.manifest_id or "",
        paths=["epics/demo/PLAN.md"],
        prefix="",
    )

    entry = preview["preview"]["files"][0]
    assert entry["classification"] == "conflict"
    assert "secret" not in preview["diff"]
    assert "private key" in entry["detail"]


# --- The approval binds one observation -------------------------------------


@pytest.mark.asyncio
async def test_a_changed_destination_invalidates_the_preview(
    results, exports, task, checkout
) -> None:
    result = await _accepted(results, task)
    preview = exports.preview(
        task_id=task.id,
        result_id=result.id,
        expected_manifest_id=result.manifest_id or "",
        paths=["epics/demo/PLAN.md"],
        prefix="",
    )
    target = checkout / "epics" / "demo"
    target.mkdir(parents=True)
    (target / "PLAN.md").write_text("appeared after the preview\n")

    with pytest.raises(ExportError) as exc:
        await exports.start(
            task_id=task.id,
            result_id=result.id,
            expected_manifest_id=result.manifest_id or "",
            paths=["epics/demo/PLAN.md"],
            prefix="",
            token=preview["preview_token"],
            request_id="exp-1",
        )

    assert exc.value.reason == "stale-preview"
    assert (target / "PLAN.md").read_text() == "appeared after the preview\n"


@pytest.mark.asyncio
async def test_a_changed_prefix_requires_a_new_preview(
    results, exports, task
) -> None:
    result = await _accepted(results, task)
    preview = exports.preview(
        task_id=task.id,
        result_id=result.id,
        expected_manifest_id=result.manifest_id or "",
        paths=["epics/demo/PLAN.md"],
        prefix="",
    )

    with pytest.raises(ExportError) as exc:
        await exports.start(
            task_id=task.id,
            result_id=result.id,
            expected_manifest_id=result.manifest_id or "",
            paths=["epics/demo/PLAN.md"],
            prefix="handoffs",
            token=preview["preview_token"],
            request_id="exp-1",
        )

    assert exc.value.reason == "stale-preview"


@pytest.mark.asyncio
async def test_a_replayed_request_returns_the_original_operation(
    results, exports, task, checkout
) -> None:
    result = await _accepted(results, task)
    preview, first, created = await _export(exports, task, result)
    assert created is True

    record, created_again = await exports.start(
        task_id=task.id,
        result_id=result.id,
        expected_manifest_id=result.manifest_id or "",
        paths=["epics/demo/PLAN.md", "epics/demo/SPEC.md"],
        prefix="",
        token=preview["preview_token"],
        request_id="exp-1",
    )

    assert created_again is False
    assert record.id == first.id
    assert len(list_task_exports(exports._engine, task.id)) == 1


@pytest.mark.asyncio
async def test_a_successor_capture_does_not_retarget_an_existing_selection(
    results, exports, task, workspace, checkout
) -> None:
    """An unchanged older selection stays exactly as previewed. A newer
    revision is a different revision, not an update to this one."""
    result = await _accepted(results, task)
    preview = exports.preview(
        task_id=task.id,
        result_id=result.id,
        expected_manifest_id=result.manifest_id or "",
        paths=["epics/demo/PLAN.md"],
        prefix="",
    )
    (workspace / "epics" / "demo" / "PLAN.md").write_text("# Plan\nsecond\n")
    await results.capture(task.id, paths=["epics/demo"], request_id="req-2")
    for job in list(results._jobs):
        await asyncio.gather(job, return_exceptions=True)

    record, _ = await exports.start(
        task_id=task.id,
        result_id=result.id,
        expected_manifest_id=result.manifest_id or "",
        paths=["epics/demo/PLAN.md"],
        prefix="",
        token=preview["preview_token"],
        request_id="exp-1",
    )
    await _drain(exports)

    assert get_export(exports._engine, record.id).state == STATE_COMPLETED
    assert (checkout / "epics" / "demo" / "PLAN.md").read_text() == PLAN


# --- Eligibility and boundaries ---------------------------------------------


@pytest.mark.asyncio
async def test_an_unaccepted_revision_cannot_be_exported(
    results, exports, task
) -> None:
    await results.capture(task.id, paths=["epics/demo"], request_id="req-1")
    for job in list(results._jobs):
        await asyncio.gather(job, return_exceptions=True)
    result = list_results(results._engine, task.id)[0]

    with pytest.raises(ExportError) as exc:
        exports.preview(
            task_id=task.id,
            result_id=result.id,
            expected_manifest_id=result.manifest_id or "",
            paths=["epics/demo/PLAN.md"],
            prefix="",
        )

    assert exc.value.reason == "not-accepted"


@pytest.mark.asyncio
async def test_a_path_outside_the_manifest_is_refused(
    results, exports, task
) -> None:
    result = await _accepted(results, task)

    with pytest.raises(ExportError) as exc:
        exports.preview(
            task_id=task.id,
            result_id=result.id,
            expected_manifest_id=result.manifest_id or "",
            paths=["epics/demo/OTHER.md"],
            prefix="",
        )

    assert exc.value.reason == "unknown-file"


@pytest.mark.parametrize(
    "prefix", ["../escape", "/absolute", ".hidden", "a/../b"]
)
@pytest.mark.asyncio
async def test_an_unsafe_prefix_is_refused_without_touching_the_checkout(
    results, exports, task, checkout, prefix: str
) -> None:
    result = await _accepted(results, task)
    before = sorted(p.name for p in checkout.iterdir())

    with pytest.raises(ExportError) as exc:
        exports.preview(
            task_id=task.id,
            result_id=result.id,
            expected_manifest_id=result.manifest_id or "",
            paths=["epics/demo/PLAN.md"],
            prefix=prefix,
        )

    assert exc.value.reason == "invalid-prefix"
    assert sorted(p.name for p in checkout.iterdir()) == before


@pytest.mark.asyncio
async def test_a_launcher_control_destination_is_refused(
    results, exports, task, workspace, config, engine
) -> None:
    """`workshop.yaml` is read before an agent exists; retained text landing
    there would change how the container itself is built."""
    (workspace / "workshop.yaml").write_text("image: demo\n")
    await results.capture(
        task.id, paths=["workshop.yaml"], request_id="req-launcher"
    )
    for job in list(results._jobs):
        await asyncio.gather(job, return_exceptions=True)
    result = list_results(engine, task.id)[0]
    results.accept(result, expected_manifest_id=result.manifest_id or "")
    result = list_results(engine, task.id)[0]

    with pytest.raises(ExportError) as exc:
        exports.preview(
            task_id=task.id,
            result_id=result.id,
            expected_manifest_id=result.manifest_id or "",
            paths=["workshop.yaml"],
            prefix="",
        )

    assert exc.value.reason == "reserved-destination"


@pytest.mark.asyncio
async def test_a_nested_repository_boundary_refuses_the_destination(
    results, exports, task, checkout
) -> None:
    result = await _accepted(results, task)
    nested = checkout / "epics"
    nested.mkdir()
    (nested / ".git").mkdir()

    preview = exports.preview(
        task_id=task.id,
        result_id=result.id,
        expected_manifest_id=result.manifest_id or "",
        paths=["epics/demo/PLAN.md"],
        prefix="",
    )

    entry = preview["preview"]["files"][0]
    assert entry["classification"] == "conflict"
    assert entry["reason"] == "nested-repository"


@pytest.mark.asyncio
async def test_preview_writes_nothing_at_all(
    results, exports, task, checkout
) -> None:
    result = await _accepted(results, task)
    before = {
        path.relative_to(checkout): path.stat().st_mtime_ns
        for path in checkout.rglob("*")
    }

    exports.preview(
        task_id=task.id,
        result_id=result.id,
        expected_manifest_id=result.manifest_id or "",
        paths=["epics/demo/PLAN.md", "epics/demo/SPEC.md"],
        prefix="handoffs",
    )

    after = {
        path.relative_to(checkout): path.stat().st_mtime_ns
        for path in checkout.rglob("*")
    }
    assert after == before


# --- Serialization and lifetime ---------------------------------------------


@pytest.mark.asyncio
async def test_an_unresolved_export_holds_the_checkout_root(
    results, exports, task, engine
) -> None:
    """Keyed by device and inode, so a second registration naming the same
    directory by another path cannot slip past it."""
    result = await _accepted(results, task)
    _preview, record, _ = await _export(exports, task, result)
    from ompire_daemon.registry.result_exports import finish_export

    finish_export(engine, record.id, state=STATE_UNRESOLVED, error="left unknown")

    with pytest.raises(Exception) as exc:
        await _export(exports, task, result, paths=("epics/demo/PLAN.md",), request_id="exp-2")

    assert "unresolved" in str(exc.value)


@pytest.mark.asyncio
async def test_an_unfinished_export_blocks_result_purge_and_names_itself(
    results, exports, task, engine
) -> None:
    result = await _accepted(results, task)
    _preview, record, _ = await _export(exports, task, result)
    from ompire_daemon.registry.result_exports import finish_export

    finish_export(engine, record.id, state=STATE_UNRESOLVED, error="left unknown")
    current = list_results(engine, task.id)[0]

    with pytest.raises(ExportsActiveError) as exc:
        results.purge(
            current,
            expected_manifest_id=current.manifest_id or "",
            expected_version=results_version(engine, task.id),
        )

    assert record.id in exc.value.export_ids


@pytest.mark.asyncio
async def test_a_settled_export_releases_the_result_for_purge(
    results, exports, task, engine, checkout
) -> None:
    """Temporary protection, deliberately: the delivered copies are ordinary
    checkout files and outlive the retained bytes."""
    result = await _accepted(results, task)
    _preview, record, _ = await _export(exports, task, result)
    assert record.state == STATE_COMPLETED
    current = list_results(engine, task.id)[0]

    results.purge(
        current,
        expected_manifest_id=current.manifest_id or "",
        expected_version=results_version(engine, task.id),
    )

    assert list_results(engine, task.id)[0].state == "purged"
    # The export's own record survives its source's tombstone.
    assert get_export(engine, record.id).state == STATE_COMPLETED
    assert (checkout / "epics" / "demo" / "PLAN.md").read_text() == PLAN


@pytest.mark.asyncio
async def test_task_purge_removes_terminal_export_history_and_no_files(
    results, exports, task, engine, checkout
) -> None:
    result = await _accepted(results, task)
    _preview, record, _ = await _export(exports, task, result)
    # Terminal, so it holds nothing: an unfinished export would refuse below.
    assert record.state == STATE_COMPLETED
    current = list_results(engine, task.id)[0]
    results.purge(
        current,
        expected_manifest_id=current.manifest_id or "",
        expected_version=results_version(engine, task.id),
    )
    mark_archived(engine, task.id)

    purge_task(engine, task.id)

    assert list_task_exports(engine, task.id) == []
    assert (checkout / "epics" / "demo" / "PLAN.md").read_text() == PLAN


@pytest.mark.asyncio
async def test_repointing_the_checkout_is_refused_while_an_export_is_unfinished(
    results, exports, task, engine, tmp_path: Path, checkout
) -> None:
    result = await _accepted(results, task)
    _preview, record, _ = await _export(exports, task, result)
    from ompire_daemon.registry.result_exports import finish_export

    finish_export(engine, record.id, state=STATE_UNRESOLVED, error="left unknown")
    elsewhere = tmp_path / "proj" / "other"
    elsewhere.mkdir(parents=True)

    with pytest.raises(ExportsActiveError):
        update_project(
            engine,
            "demo",
            title="Demo",
            upstream_url="https://github.com/owner/repo",
            fork_url=None,
            checkout_path=str(elsewhere),
            fetch_remote=DEFAULT_FETCH_REMOTE,
        )

    # An unrelated edit to the same project is unaffected.
    updated = update_project(
        engine,
        "demo",
        title="Renamed",
        upstream_url="https://github.com/owner/repo",
        fork_url=None,
        checkout_path=str(checkout),
        fetch_remote=DEFAULT_FETCH_REMOTE,
    )
    assert updated.title == "Renamed"


# --- Interruption and recovery ----------------------------------------------


@pytest.mark.asyncio
async def test_a_crash_before_any_staging_reports_nothing_installed(
    results, exports, task, engine, checkout
) -> None:
    """The daemon stopped between admission and staging. Nothing was written,
    and recovery says exactly that rather than guessing."""
    result = await _accepted(results, task)
    preview = exports.preview(
        task_id=task.id,
        result_id=result.id,
        expected_manifest_id=result.manifest_id or "",
        paths=["epics/demo/PLAN.md"],
        prefix="",
    )
    record, _ = await exports.start(
        task_id=task.id,
        result_id=result.id,
        expected_manifest_id=result.manifest_id or "",
        paths=["epics/demo/PLAN.md"],
        prefix="",
        token=preview["preview_token"],
        request_id="exp-1",
    )
    for job in list(exports._jobs):
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)
    # Put the row back the way an abrupt stop would leave it.
    from ompire_daemon.registry.result_exports import _update

    _update(engine, record.id, state=STATE_RUNNING, finished_at=None)

    settled = exports.restore()

    assert [entry.state for entry in settled] == [STATE_INCOMPLETE]
    assert settled[0].files[0].outcome == OUTCOME_NOT_INSTALLED
    assert not (checkout / "epics").exists()


@pytest.mark.asyncio
async def test_recovery_recognizes_a_rename_that_beat_its_journal_entry(
    results, exports, task, engine, checkout
) -> None:
    """The staged inode is found at the destination, which establishes the move
    happened. Matching bytes alone would not."""
    result = await _accepted(results, task)
    _preview, record, _ = await _export(
        exports, task, result, paths=("epics/demo/PLAN.md",)
    )
    installed = checkout / "epics" / "demo" / "PLAN.md"
    identity = installed.stat()
    from ompire_daemon.registry.result_exports import (
        OUTCOME_PENDING,
        _update,
        record_file_outcome,
        record_staged_file,
    )

    # Rewind to "renamed, but the outcome was never journalled".
    record_file_outcome(
        engine, record.id, "epics/demo/PLAN.md", outcome=OUTCOME_PENDING
    )
    record_staged_file(
        engine,
        record.id,
        "epics/demo/PLAN.md",
        device=identity.st_dev,
        inode=identity.st_ino,
    )
    _update(engine, record.id, state=STATE_RUNNING, finished_at=None)

    settled = exports.restore()

    assert settled[0].state == STATE_COMPLETED
    assert settled[0].files[0].outcome == OUTCOME_CREATED


@pytest.mark.asyncio
async def test_matching_bytes_under_a_foreign_identity_stay_unknown(
    results, exports, task, engine, checkout
) -> None:
    result = await _accepted(results, task)
    _preview, record, _ = await _export(
        exports, task, result, paths=("epics/demo/PLAN.md",)
    )
    from ompire_daemon.registry.result_exports import (
        OUTCOME_PENDING,
        _update,
        record_file_outcome,
        record_staged_file,
    )

    record_file_outcome(
        engine, record.id, "epics/demo/PLAN.md", outcome=OUTCOME_PENDING
    )
    # A staged identity that is not the file now at the destination.
    record_staged_file(
        engine, record.id, "epics/demo/PLAN.md", device=1, inode=999999
    )
    _update(engine, record.id, state=STATE_RUNNING, finished_at=None)

    settled = exports.restore()

    assert settled[0].state == STATE_UNRESOLVED
    assert settled[0].files[0].outcome == OUTCOME_UNKNOWN
    assert "cannot be established" in (settled[0].files[0].error or "")


@pytest.mark.asyncio
async def test_acknowledging_closes_uncertainty_without_claiming_success(
    results, exports, task, engine, checkout
) -> None:
    result = await _accepted(results, task)
    _preview, record, _ = await _export(
        exports, task, result, paths=("epics/demo/PLAN.md",)
    )
    from ompire_daemon.registry.result_exports import (
        OUTCOME_PENDING,
        _update,
        record_file_outcome,
        record_staged_file,
    )

    record_file_outcome(
        engine, record.id, "epics/demo/PLAN.md", outcome=OUTCOME_PENDING
    )
    record_staged_file(
        engine, record.id, "epics/demo/PLAN.md", device=1, inode=999999
    )
    _update(engine, record.id, state=STATE_RUNNING, finished_at=None)
    unresolved = exports.restore()[0]
    assert unresolved.state == STATE_UNRESOLVED

    closed = exports.acknowledge(
        unresolved, expected_version=results_version(engine, task.id)
    )

    assert closed.state == STATE_INCOMPLETE
    assert closed.acknowledged_at is not None
    # The uncertainty itself is preserved, never rewritten into a success.
    assert closed.files[0].outcome == OUTCOME_UNKNOWN
    # And the temporary protection is released.
    current = list_results(engine, task.id)[0]
    results.purge(
        current,
        expected_manifest_id=current.manifest_id or "",
        expected_version=results_version(engine, task.id),
    )


@pytest.mark.asyncio
async def test_recovery_leaves_no_staging_directory_behind(
    results, exports, task, checkout
) -> None:
    result = await _accepted(results, task)
    await _export(exports, task, result)

    leftovers = [
        path.name
        for path in checkout.iterdir()
        if path.name.startswith(".ompire-export-")
    ]
    assert leftovers == []


@pytest.mark.asyncio
async def test_a_replaced_checkout_root_stops_before_writing_anything(
    results, exports, task, engine, tmp_path: Path, checkout
) -> None:
    """A database reservation says no other export owns the root. It says
    nothing about the directory, so identity is checked against the approval."""
    result = await _accepted(results, task)
    preview = exports.preview(
        task_id=task.id,
        result_id=result.id,
        expected_manifest_id=result.manifest_id or "",
        paths=["epics/demo/PLAN.md"],
        prefix="",
    )
    record, _ = await exports.start(
        task_id=task.id,
        result_id=result.id,
        expected_manifest_id=result.manifest_id or "",
        paths=["epics/demo/PLAN.md"],
        prefix="",
        token=preview["preview_token"],
        request_id="exp-1",
    )
    # Swap the directory itself for a different one at the same path.
    replacement = tmp_path / "proj" / "replacement"
    replacement.mkdir(parents=True)
    os.rename(checkout, tmp_path / "proj" / "moved-away")
    os.rename(replacement, checkout)
    await _drain(exports)

    settled = get_export(engine, record.id)
    assert settled.state == STATE_INCOMPLETE
    assert "not the directory" in (settled.error or "")
    assert not (checkout / "epics").exists()


# --- The retained source is the only source ---------------------------------


@pytest.mark.asyncio
async def test_export_uses_retained_bytes_after_the_workspace_is_gone(
    results, exports, task, engine, workspace, checkout
) -> None:
    result = await _accepted(results, task)
    (workspace / "epics" / "demo" / "PLAN.md").write_text("# Edited later\n")
    mark_archived(engine, task.id)

    _preview, record, _ = await _export(exports, task, result)

    assert record.state == STATE_COMPLETED
    installed = (checkout / "epics" / "demo" / "PLAN.md").read_bytes()
    assert installed == PLAN.encode()
    assert (
        hashlib.sha256(installed).hexdigest()
        == next(e.sha256 for e in result.files if e.path == "epics/demo/PLAN.md")
    )


@pytest.mark.asyncio
async def test_two_registrations_of_one_directory_cannot_export_at_once(
    results, exports, engine, task, tmp_path: Path, checkout, workspace
) -> None:
    """The reservation is keyed by the root's device and inode, so registering
    the same directory a second time under another name buys nothing."""
    result = await _accepted(results, task)
    create_project(
        engine,
        name="alias",
        title="Alias",
        upstream_url="https://github.com/owner/repo",
        fork_url=None,
        # The same directory, spelled differently.
        checkout_path=str(checkout.parent / "." / checkout.name),
        default_checkout_root=tmp_path / "proj",
    )
    alias_task = create_task(
        engine,
        project_name="alias",
        slug="task-2",
        branch="ompire/task-2",
        clone_path=str(workspace),
        prompt="explore",
        execution_inputs=make_execution_inputs(
            checkout_path=str(checkout),
            project_name="alias",
            branch="ompire/task-2",
        ),
    )
    alias_result = await _accepted(results, alias_task)
    _preview, record, _ = await _export(
        exports, task, result, paths=("epics/demo/PLAN.md",)
    )
    from ompire_daemon.registry.result_exports import finish_export

    finish_export(engine, record.id, state=STATE_UNRESOLVED, error="left unknown")

    with pytest.raises(CheckoutBusyError) as exc:
        await _export(
            exports,
            alias_task,
            alias_result,
            paths=("epics/demo/SPEC.md",),
            request_id="exp-alias",
        )

    assert exc.value.export_id == record.id


@pytest.mark.asyncio
async def test_the_active_root_reservation_is_enforced_by_the_database(
    results, engine, task
) -> None:
    """The refusal above is the readable one; this index is the guarantee.

    A check-then-insert is only as good as the transaction around it, and this
    reservation has to survive a second daemon process and a restart.
    """
    from sqlalchemy.exc import IntegrityError

    from ompire_daemon.db import result_exports

    result = await _accepted(results, task)
    row = {
        "task_id": task.id,
        "result_id": result.id,
        "manifest_id": result.manifest_id,
        "selection_json": "[]",
        "selection_fingerprint": "f",
        "prefix": "",
        "preview_token": "t",
        "preview_json": "{}",
        "project_name": "demo",
        "checkout_path": "/tmp/x",
        "root_device": 7,
        "root_inode": 11,
        "staging_name": ".ompire-export-a",
        "actor": "operator",
        "confirmed_at": "2026-09-09T00:00:00+00:00",
    }
    with engine.begin() as conn:
        conn.execute(
            result_exports.insert().values(
                id="exp_a", request_id="r1", state=STATE_RUNNING, **row
            )
        )
        # A *settled* export releases the root, so this one is admitted.
        conn.execute(
            result_exports.insert().values(
                id="exp_b", request_id="r2", state=STATE_COMPLETED, **row
            )
        )
        with pytest.raises(IntegrityError):
            conn.execute(
                result_exports.insert().values(
                    id="exp_c", request_id="r3", state=STATE_UNRESOLVED, **row
                )
            )
