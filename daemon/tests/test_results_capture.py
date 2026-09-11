"""Tests for `ompire_daemon.results`: the trusted capture boundary.

These cover the edges where a plausible implementation is wrong rather than the
ordinary happy path — escape through a component that becomes a symlink, an
entry that is not an ordinary file, a source that changes mid-read, the bounds,
credential material, and what a failure leaves behind. Wiring and defaults are
covered by the REST and registry suites.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from ompire_daemon.config import Config
from ompire_daemon.db import db_path_for, ensure_db_dir, make_engine
from ompire_daemon.events import EventHub
from ompire_daemon.isolation import WorkspaceGuard
from ompire_daemon.migrate import upgrade_head
from ompire_daemon.registry.results import (
    MAX_FILES,
    STATE_FAILED,
    STATE_READY,
    InvalidSelectionError,
    ResultStateError,
    get_result,
    list_results,
)
from ompire_daemon.results import ResultManager
from ompire_daemon.work.projects import create_project
from ompire_daemon.work.tasks import create_task, mark_archived
from tests.conftest import make_execution_inputs


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    clone = tmp_path / "tasks" / "demo" / "task-1"
    (clone / "epics" / "demo").mkdir(parents=True)
    (clone / "epics" / "demo" / "PLAN.md").write_text("# Plan\nline two\n")
    (clone / "epics" / "demo" / "SPEC.md").write_text("# Spec\n")
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


async def _capture(manager, task, paths, request_id="req-1"):
    """Run one capture to completion, the way the daemon does."""
    await manager.capture(task.id, paths=paths, request_id=request_id)
    for job in list(manager._jobs):
        await asyncio.gather(job, return_exceptions=True)
    return list_results(manager._engine, task.id)[0]


# --- The happy path, only as the baseline the refusals are measured against --


@pytest.mark.asyncio
async def test_capture_retains_exact_bytes_and_relative_paths(
    manager, task, workspace
) -> None:
    result = await _capture(manager, task, ["epics/demo"])

    assert result.state == STATE_READY
    assert [entry.path for entry in result.files] == [
        "epics/demo/PLAN.md",
        "epics/demo/SPEC.md",
    ]
    contents = manager.read_bundle(result)
    assert contents["epics/demo/PLAN.md"] == b"# Plan\nline two\n"
    # Bytes are exact: the final newline survives, and nothing is normalized.
    assert contents["epics/demo/SPEC.md"] == (
        workspace / "epics" / "demo" / "SPEC.md"
    ).read_bytes()


@pytest.mark.asyncio
async def test_workflow_capture_keeps_attempt_provenance(manager, task, workspace) -> None:
    result = await manager.capture_workflow(
        task,
        workflow_seq=7,
        paths=["epics/demo/PLAN.md"],
        allowlist=("epics",),
        provenance={
            "producing_attempt": 6,
            "producing_step": "propose",
            "producing_session": "plan",
        },
    )

    assert result.state == STATE_READY
    assert result.workflow_seq == 7
    assert result.manifest["capture_actor"] == "workflow"
    assert result.manifest["provenance"]["producing_attempt"] == 6
    assert manager.read_bundle(result)["epics/demo/PLAN.md"] == (
        workspace / "epics" / "demo" / "PLAN.md"
    ).read_bytes()

@pytest.mark.asyncio
async def test_later_workspace_edits_do_not_mutate_a_capture(
    manager, task, workspace
) -> None:
    result = await _capture(manager, task, ["epics/demo/PLAN.md"])
    (workspace / "epics" / "demo" / "PLAN.md").write_text("# Rewritten\n")

    assert manager.read_bundle(result)["epics/demo/PLAN.md"] == b"# Plan\nline two\n"


# --- Escape and unsafe entries ----------------------------------------------


@pytest.mark.asyncio
async def test_a_symlinked_component_cannot_escape_the_workspace(
    manager, task, workspace, tmp_path: Path
) -> None:
    """The classic capture escape: a *directory component* is a link.

    Resolving the string first and opening it afterwards would read the target
    happily. Every component is opened `O_NOFOLLOW`, so this fails at the link.
    """
    secret = tmp_path / "outside"
    secret.mkdir()
    (secret / "PLAN.md").write_text("secrets\n")
    (workspace / "epics" / "elsewhere").symlink_to(secret)

    result = await _capture(manager, task, ["epics/elsewhere/PLAN.md"])

    assert result.state == STATE_FAILED
    assert "epics/elsewhere" in (result.error or "")
    assert "secrets" not in (result.error or "")


@pytest.mark.asyncio
async def test_a_symlinked_file_is_refused(manager, task, workspace, tmp_path) -> None:
    target = tmp_path / "target.md"
    target.write_text("outside\n")
    (workspace / "epics" / "demo" / "LINK.md").symlink_to(target)

    result = await _capture(manager, task, ["epics/demo"])

    assert result.state == STATE_FAILED
    assert "symlink" in (result.error or "")


@pytest.mark.asyncio
async def test_a_hard_link_is_refused(manager, task, workspace, tmp_path) -> None:
    outside = tmp_path / "other.md"
    outside.write_text("shared\n")
    os.link(outside, workspace / "epics" / "demo" / "HARD.md")

    result = await _capture(manager, task, ["epics/demo"])

    assert result.state == STATE_FAILED
    assert "hard link" in (result.error or "")


@pytest.mark.asyncio
async def test_a_special_file_is_refused(manager, task, workspace) -> None:
    os.mkfifo(workspace / "epics" / "demo" / "PIPE.md")

    result = await _capture(manager, task, ["epics/demo"])

    assert result.state == STATE_FAILED
    assert "not a regular file" in (result.error or "")


@pytest.mark.asyncio
async def test_an_unsupported_type_inside_a_selection_fails_the_capture(
    manager, task, workspace
) -> None:
    """Not a silent skip: the operator selected this directory, and a bundle
    that quietly dropped part of it would misdescribe what it is."""
    (workspace / "epics" / "demo" / "diagram.png").write_bytes(b"\x89PNG\r\n")

    result = await _capture(manager, task, ["epics/demo"])

    assert result.state == STATE_FAILED
    assert "unsupported file type" in (result.error or "")


@pytest.mark.asyncio
async def test_hidden_names_inside_a_selection_are_not_captured(
    manager, task, workspace
) -> None:
    (workspace / "epics" / "demo" / ".secrets").mkdir()
    (workspace / "epics" / "demo" / ".secrets" / "token.txt").write_text("x\n")
    (workspace / "epics" / "demo" / ".hidden.md").write_text("x\n")

    result = await _capture(manager, task, ["epics/demo"])

    assert result.state == STATE_READY
    assert [entry.path for entry in result.files] == [
        "epics/demo/PLAN.md",
        "epics/demo/SPEC.md",
    ]


@pytest.mark.asyncio
async def test_the_clone_root_is_not_a_valid_selection(manager, task) -> None:
    """Refused before a capture identity exists.

    A malformed selection is a bad request, not a failed capture: recording it
    as a revision would put an operator typo into the task's result history.
    """
    for entry in (".", "", "/", "./"):
        with pytest.raises(InvalidSelectionError):
            await manager.capture(task.id, paths=[entry], request_id="req-1")
    assert list_results(manager._engine, task.id) == []


@pytest.mark.asyncio
async def test_invalid_utf8_is_refused(manager, task, workspace) -> None:
    (workspace / "epics" / "demo" / "bad.md").write_bytes(b"# Plan\n\xff\xfe\n")

    result = await _capture(manager, task, ["epics/demo"])

    assert result.state == STATE_FAILED
    assert "UTF-8" in (result.error or "")


# --- Credentials -------------------------------------------------------------


@pytest.mark.asyncio
async def test_credential_material_is_refused_without_echoing_it(
    manager, task, workspace
) -> None:
    token = "ghp_" + "a" * 36
    (workspace / "epics" / "demo" / "notes.md").write_text(f"token: {token}\n")

    result = await _capture(manager, task, ["epics/demo"])

    assert result.state == STATE_FAILED
    assert "credential" in (result.error or "")
    # The refusal must not publish what it found into result history.
    assert token not in (result.error or "")
    assert manager.projection(task.id)["results"][0]["error"] is not None
    assert token not in str(manager.projection(task.id))


@pytest.mark.asyncio
async def test_a_private_key_block_is_refused(manager, task, workspace) -> None:
    (workspace / "epics" / "demo" / "key.txt").write_text(
        "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n"
    )

    result = await _capture(manager, task, ["epics/demo"])

    assert result.state == STATE_FAILED
    assert "private key" in (result.error or "")


@pytest.mark.asyncio
async def test_the_daemons_own_token_is_recognized(
    manager, task, workspace, tmp_path
) -> None:
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "token").write_text("s3cr3t-daemon-token\n")
    (workspace / "epics" / "demo" / "leak.md").write_text(
        "curl -H 'Authorization: Bearer s3cr3t-daemon-token' localhost\n"
    )

    result = await _capture(manager, task, ["epics/demo"])

    assert result.state == STATE_FAILED
    assert "s3cr3t-daemon-token" not in (result.error or "")


# --- Bounds ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_too_many_files_fails_rather_than_truncating(
    manager, task, workspace
) -> None:
    bulk = workspace / "epics" / "bulk"
    bulk.mkdir()
    for index in range(MAX_FILES + 1):
        (bulk / f"n{index:03d}.md").write_text("x\n")

    result = await _capture(manager, task, ["epics/bulk"])

    assert result.state == STATE_FAILED
    assert str(MAX_FILES) in (result.error or "")


@pytest.mark.asyncio
async def test_an_oversized_file_is_refused(manager, task, workspace) -> None:
    (workspace / "epics" / "demo" / "big.md").write_text("x" * (1024 * 1024 + 1))

    result = await _capture(manager, task, ["epics/demo"])

    assert result.state == STATE_FAILED
    assert "per-file limit" in (result.error or "")


@pytest.mark.asyncio
async def test_the_total_byte_limit_is_enforced(manager, task, workspace) -> None:
    bulk = workspace / "epics" / "bulk"
    bulk.mkdir()
    for index in range(10):
        (bulk / f"n{index}.md").write_text("x" * (1024 * 1024))

    result = await _capture(manager, task, ["epics/bulk"])

    assert result.state == STATE_FAILED
    assert "total limit" in (result.error or "")


# --- Failure leaves earlier work alone ---------------------------------------


@pytest.mark.asyncio
async def test_a_failed_capture_preserves_an_existing_revision(
    manager, task, workspace
) -> None:
    good = await _capture(manager, task, ["epics/demo"], request_id="req-1")
    (workspace / "epics" / "demo" / "broken.md").write_bytes(b"\xff")

    bad = await _capture(manager, task, ["epics/demo"], request_id="req-2")

    assert bad.state == STATE_FAILED
    reread = get_result(manager._engine, good.id)
    assert reread.state == STATE_READY
    assert manager.read_bundle(reread)["epics/demo/PLAN.md"] == b"# Plan\nline two\n"


@pytest.mark.asyncio
async def test_capture_is_refused_while_another_writer_owns_the_workspace(
    manager, task
) -> None:
    manager._guard.acquire(task.id, "review")

    with pytest.raises(Exception) as exc:
        await manager.capture(task.id, paths=["epics/demo"], request_id="req-1")

    assert "review" in str(exc.value)
    # Nothing was recorded: a refused admission is not a failed capture.
    assert list_results(manager._engine, task.id) == []


@pytest.mark.asyncio
async def test_capture_is_refused_after_cleanup(manager, task) -> None:
    mark_archived(manager._engine, task.id)

    with pytest.raises(ResultStateError) as exc:
        await manager.capture(task.id, paths=["epics/demo"], request_id="req-1")

    assert "cleaned up" in exc.value.detail


# --- Provenance ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_provenance_names_what_is_unknown(manager, task) -> None:
    """A manual capture knows the operator did it and little else. The run's
    most recent step is not relabeled as the producer of these files."""
    result = await _capture(manager, task, ["epics/demo"])

    provenance = (result.manifest or {})["provenance"]
    assert provenance["capture_actor"] == "operator"
    assert provenance["producing_step"] == "unknown"
    assert provenance["producing_session"] == "unknown"
    assert provenance["producing_run"] == "unknown"
    # No Git repository in this workspace: an honest gap, not a guessed base.
    assert provenance["capture_head_commit"] is None
    assert "capture_head_commit" in provenance["gaps"]
    assert provenance["launch_base_branch"] == "main"


@pytest.mark.asyncio
async def test_a_task_without_launch_inputs_can_still_be_captured(
    manager, engine, task, workspace
) -> None:
    """A legacy task's files are perfectly safe to keep; only its provenance
    is unknown, and it says so."""
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(
            text("UPDATE tasks SET execution_inputs_json = NULL WHERE id = :id"),
            {"id": task.id},
        )

    result = await _capture(manager, task, ["epics/demo"])

    assert result.state == STATE_READY
    provenance = (result.manifest or {})["provenance"]
    assert provenance["launch_base_branch"] is None
    assert "launch_inputs" in provenance["gaps"]


# --- Racing sources -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_source_changed_after_admission_refuses_the_capture(
    manager, task, workspace, monkeypatch
) -> None:
    """The workspace guard coordinates Ompire's own writers, not an arbitrary
    background process inside the container. So the read boundary checks, and a
    file that moved between admission and its read refuses the whole capture
    rather than retaining a half-consistent bundle."""
    from ompire_daemon.results import _Walker

    target = workspace / "epics" / "demo" / "PLAN.md"
    original = _Walker.read
    swapped: list[bool] = []

    def _swap_then_read(self, dir_fd, name, display, admitted):
        if not swapped:
            swapped.append(True)
            replacement = workspace / "epics" / "demo" / ".replacement"
            replacement.write_text("# Substituted\n")
            os.replace(replacement, target)
        return original(self, dir_fd, name, display, admitted)

    monkeypatch.setattr(_Walker, "read", _swap_then_read)

    result = await _capture(manager, task, ["epics/demo"])

    assert result.state == STATE_FAILED
    assert "changed" in (result.error or "")


@pytest.mark.asyncio
async def test_a_file_growing_past_the_limit_mid_read_is_refused(
    manager, task, workspace, monkeypatch
) -> None:
    from ompire_daemon.results import _Walker

    target = workspace / "epics" / "demo" / "PLAN.md"
    original = _Walker.read

    def _grow_then_read(self, dir_fd, name, display, admitted):
        if display.endswith("PLAN.md"):
            with target.open("a") as handle:
                handle.write("x" * (1024 * 1024 + 16))
        return original(self, dir_fd, name, display, admitted)

    monkeypatch.setattr(_Walker, "read", _grow_then_read)

    result = await _capture(manager, task, ["epics/demo"])

    assert result.state == STATE_FAILED


@pytest.mark.asyncio
async def test_a_cancelled_capture_records_an_interruption_and_retains_nothing(
    manager, task
) -> None:
    await manager.capture(task.id, paths=["epics/demo"], request_id="req-1")
    job = next(iter(manager._jobs))
    await asyncio.sleep(0)  # let the capture reach its guarded read
    job.cancel()
    await asyncio.gather(job, return_exceptions=True)

    result = list_results(manager._engine, task.id)[0]
    assert result.state == STATE_FAILED
    assert manager.projection(task.id)["results"][0]["file_count"] == 0
    # The guard is free again, so the operator can correct and capture anew.
    manager._guard.assert_available(task.id)


@pytest.mark.asyncio
async def test_restart_turns_an_unfinished_capture_into_a_visible_failure(
    manager, engine, task
) -> None:
    """No workspace is re-read on restart: the interrupted request keeps its
    identity and its failure, and a retry is an explicit new capture."""
    from ompire_daemon.registry.results import normalize_selection, open_capture

    opened, _ = open_capture(
        engine,
        task_id=task.id,
        request_id="req-1",
        selection=normalize_selection(["epics/demo"]),
    )

    reconciled = manager.restore()

    assert [item.id for item in reconciled] == [opened.id]
    assert get_result(engine, opened.id).state == STATE_FAILED


@pytest.mark.asyncio
async def test_shutdown_reconciles_a_capture_that_never_started(manager, task) -> None:
    """Cancelled before its job first ran, so it never reached its own
    interruption handler. The row must not be left looking in-flight."""
    await manager.capture(task.id, paths=["epics/demo"], request_id="req-1")

    await manager.shutdown()

    assert list_results(manager._engine, task.id)[0].state == STATE_FAILED


# --- Byte fidelity ------------------------------------------------------------


@pytest.mark.asyncio
async def test_bytes_are_retained_exactly_including_odd_endings(
    manager, task, workspace
) -> None:
    """Nothing normalizes captured text.

    A plan whose file has CRLF endings or no final newline must come back
    byte-identical, because the checksum the operator accepts is over these
    bytes — and a zero-byte file is a real file, not an absent one.
    """
    edge = workspace / "epics" / "edge"
    edge.mkdir()
    (edge / "empty.md").write_bytes(b"")
    (edge / "crlf.md").write_bytes(b"# A\r\nline\r\n")
    (edge / "nofinal.md").write_bytes(b"no trailing newline")

    result = await _capture(manager, task, ["epics/edge"])

    assert result.state == STATE_READY
    contents = manager.read_bundle(result)
    assert contents["epics/edge/empty.md"] == b""
    assert contents["epics/edge/crlf.md"] == b"# A\r\nline\r\n"
    assert contents["epics/edge/nofinal.md"] == b"no trailing newline"
    # A zero-length file is described honestly rather than dropped.
    assert any(entry.length == 0 for entry in result.files)


@pytest.mark.asyncio
async def test_a_directory_with_no_supported_files_captures_nothing(
    manager, task, workspace
) -> None:
    """A bundle has to contain at least one file. An empty one would be an
    accepted result that says nothing."""
    (workspace / "epics" / "hollow").mkdir()

    result = await _capture(manager, task, ["epics/hollow"])

    assert result.state == STATE_FAILED
    assert "no supported files" in (result.error or "")


@pytest.mark.asyncio
async def test_capture_records_the_observed_head_of_a_real_repository(
    manager, task, workspace
) -> None:
    """The positive provenance path, with the clone's configuration disarmed.

    Both Git observations run with hooks pointed at nothing and system/global
    configuration ignored, because the clone is agent-writable and a capture
    must not be the moment repository-controlled configuration executes.
    """
    import subprocess

    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    def run(*args: str) -> None:
        subprocess.run(args, cwd=workspace, check=True, capture_output=True, env=env)

    run("git", "init", "-q", "-b", "main")
    run("git", "config", "user.email", "t@example.com")
    run("git", "config", "user.name", "Test")
    run("git", "add", "-A")
    run("git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "plan")
    head = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=workspace, check=True, capture_output=True, text=True, env=env,
    ).stdout.strip()

    result = await _capture(manager, task, ["epics/demo"])

    assert result.state == STATE_READY
    provenance = (result.manifest or {})["provenance"]
    assert provenance["capture_head_commit"] == head
    assert provenance["git_observation"] == "capture-time observation"
    assert "capture_head_commit" not in provenance["gaps"]
    # `main` is the task's recorded launch base and exists here, so the
    # merge-base is readable too.
    assert provenance["capture_merge_base"] == head
    # Still labelled an observation, never presented as the spawn base.
    assert provenance["launch_base_branch"] == "main"
