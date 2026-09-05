"""The accepted Workshop additions source actually governs the launch.

my-workshop's own rule is local-first with no source flag, so "leave the argv
alone" cannot honor either selection reliably. These tests drive the adapter
around an executable fake launcher that records exactly what it saw at the
moment it ran — argv capture alone would prove nothing about which additions
file the launcher would have merged.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from ompire_daemon import workshopadditions
from ompire_daemon.config import Config
from ompire_daemon.events import EventHub
from ompire_daemon.registry.projects import create_project
from ompire_daemon.registry.tasks import clone_path_for, create_task, get_task
from ompire_daemon.spawn import run_spawn_pipeline
from ompire_daemon.workshopadditions import (
    EMPTY_ADDITIONS,
    LOCAL_ADDITIONS_FILENAME,
    WorkshopAdditionsError,
)
from tests.conftest import make_execution_inputs


class _RecordingRunner:
    """Stands in for the workflow engine at the pipeline's handoff. These
    tests are about the workspace the launcher saw and left behind, not about
    what the engine does with it afterwards."""

    def __init__(self) -> None:
        self.started: list[int] = []

    def start_run(self, task) -> None:
        self.started.append(task.id)

LOCAL_MARKER = "additions:\n  - source: project-local\n"
GLOBAL_MARKER = "additions:\n  - source: operator-global\n"


@pytest.fixture
def global_additions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the launcher's global additions path at a disposable file. The
    operator's real one is never read, let alone written."""
    config_home = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    path = config_home / "my-workshop" / "my.yaml"
    path.parent.mkdir(parents=True)
    return path


def test_global_selection_applies_the_operator_file(
    tmp_path: Path, global_additions: Path
) -> None:
    global_additions.write_text(GLOBAL_MARKER)
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / LOCAL_ADDITIONS_FILENAME).write_text(LOCAL_MARKER)

    staged = workshopadditions.stage(tmp_path / "data", 1, str(clone), "global")

    # The launcher's local-first rule now resolves to the *selected* source.
    assert (clone / LOCAL_ADDITIONS_FILENAME).read_text() == GLOBAL_MARKER
    assert "global additions applied" in staged.note

    workshopadditions.restore(tmp_path / "data", staged)
    # The repository's own file is back, byte for byte.
    assert (clone / LOCAL_ADDITIONS_FILENAME).read_text() == LOCAL_MARKER


def test_project_selection_never_falls_through_to_the_global_file(
    tmp_path: Path, global_additions: Path
) -> None:
    """A repository with no additions of its own must launch with none — not
    with the operator's, which is what the launcher would otherwise use."""
    global_additions.write_text(GLOBAL_MARKER)
    clone = tmp_path / "clone"
    clone.mkdir()

    staged = workshopadditions.stage(tmp_path / "data", 1, str(clone), "project")

    assert (clone / LOCAL_ADDITIONS_FILENAME).read_text() == EMPTY_ADDITIONS
    assert "no additions" in staged.note

    workshopadditions.restore(tmp_path / "data", staged)
    # Absence is restored as absence, not as an empty file the daemon wrote.
    assert not (clone / LOCAL_ADDITIONS_FILENAME).exists()


def test_absent_global_source_is_reported_as_no_additions(
    tmp_path: Path, global_additions: Path
) -> None:
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / LOCAL_ADDITIONS_FILENAME).write_text(LOCAL_MARKER)

    staged = workshopadditions.stage(tmp_path / "data", 1, str(clone), "global")

    assert (clone / LOCAL_ADDITIONS_FILENAME).read_text() == EMPTY_ADDITIONS
    assert "does not exist" in staged.note
    assert "no additions" in staged.note
    workshopadditions.restore(tmp_path / "data", staged)


def test_an_unreadable_selected_source_fails_instead_of_launching_empty(
    tmp_path: Path, global_additions: Path
) -> None:
    """The launcher's own loader treats a read failure as "no additions",
    which would turn a broken file into a successful launch with nothing
    applied. A selected but unreadable source is an error."""
    global_additions.write_text(GLOBAL_MARKER)
    global_additions.chmod(0o000)
    clone = tmp_path / "clone"
    clone.mkdir()
    try:
        with pytest.raises(WorkshopAdditionsError) as exc_info:
            workshopadditions.stage(tmp_path / "data", 1, str(clone), "global")
    finally:
        global_additions.chmod(0o600)
    assert "cannot read" in str(exc_info.value)
    # Nothing was staged into the clone.
    assert not (clone / LOCAL_ADDITIONS_FILENAME).exists()


def test_a_symlinked_additions_path_is_refused(tmp_path: Path) -> None:
    """A repository must not be able to redirect the daemon's write out of
    the task's workspace."""
    clone = tmp_path / "clone"
    clone.mkdir()
    outside = tmp_path / "outside.yaml"
    outside.write_text("untouched\n")
    (clone / LOCAL_ADDITIONS_FILENAME).symlink_to(outside)

    with pytest.raises(WorkshopAdditionsError) as exc_info:
        workshopadditions.stage(tmp_path / "data", 1, str(clone), "project")

    assert "symlink" in str(exc_info.value)
    assert outside.read_text() == "untouched\n"


def test_backups_are_daemon_owned_and_never_visible_in_the_clone(
    tmp_path: Path, global_additions: Path
) -> None:
    global_additions.write_text(GLOBAL_MARKER)
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / LOCAL_ADDITIONS_FILENAME).write_text(LOCAL_MARKER)
    data_dir = tmp_path / "data"

    staged = workshopadditions.stage(data_dir, 7, str(clone), "global")

    assert Path(staged.backup_path).is_relative_to(data_dir)
    # An agent starting in this clone sees only the selected source.
    assert [p.name for p in clone.iterdir()] == [LOCAL_ADDITIONS_FILENAME]
    workshopadditions.restore(data_dir, staged)


def test_interrupted_staging_is_undone_before_agents_start(
    tmp_path: Path, global_additions: Path
) -> None:
    """A crash between staging and restore must not leave a clone carrying
    another source's additions when the next daemon resumes its agents."""
    global_additions.write_text(GLOBAL_MARKER)
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / LOCAL_ADDITIONS_FILENAME).write_text(LOCAL_MARKER)
    data_dir = tmp_path / "data"

    workshopadditions.stage(data_dir, 9, str(clone), "global")
    assert (clone / LOCAL_ADDITIONS_FILENAME).read_text() == GLOBAL_MARKER

    restored = workshopadditions.recover_pending(data_dir)

    assert restored == [9]
    assert (clone / LOCAL_ADDITIONS_FILENAME).read_text() == LOCAL_MARKER
    # Recovery is idempotent: nothing is owed a second time.
    assert workshopadditions.recover_pending(data_dir) == []


async def test_launcher_sees_the_selected_source_and_the_clone_is_restored(
    app, git_checkout: Path, tmp_path: Path, global_additions: Path
) -> None:
    """End to end through the pipeline, against an executable fake launcher
    that records the additions file it found. The clone an agent later starts
    in is unchanged."""
    global_additions.write_text(GLOBAL_MARKER)
    engine = app.state.engine
    seen = tmp_path / "launcher-saw.txt"
    launcher = tmp_path / "fake-my-workshop"
    launcher.write_text(
        f'#!/bin/sh\ncat {LOCAL_ADDITIONS_FILENAME} > {seen} 2>/dev/null || '
        f'echo "MISSING" > {seen}\necho ws-1 > .workshop.lock\n'
    )
    launcher.chmod(0o755)
    config: Config = replace(
        app.state.config, my_workshop_command=(str(launcher),)
    )

    create_project(
        engine,
        name="demo",
        title="Demo",
        upstream_url="https://example.com/demo.git",
        checkout_path=str(git_checkout),
        default_checkout_root=git_checkout.parent,
    )
    clone_path = clone_path_for(config.task_dir_root, "demo", "additions")
    task = create_task(
        engine,
        project_name="demo",
        slug="additions",
        branch="ompire/additions",
        clone_path=str(clone_path),
        prompt="",
        execution_inputs=make_execution_inputs(
            checkout_path=str(git_checkout),
            branch="ompire/additions",
            workshop_additions="global",
        ),
    )

    hub = EventHub()
    queue = hub.subscribe()
    runner = _RecordingRunner()
    await run_spawn_pipeline(engine, hub, config, task.id, runner)

    assert get_task(engine, task.id).spawn_completed_at is not None
    assert runner.started == [task.id]
    # The launcher ran with the operator's global additions in place …
    assert seen.read_text() == GLOBAL_MARKER
    # … and the clone it left behind carries none of them.
    assert not (clone_path / LOCAL_ADDITIONS_FILENAME).exists()

    disclosures = []
    while not queue.empty():
        event = queue.get_nowait()
        if event.type == "workshop_additions":
            disclosures.append(event.payload)
    assert disclosures[0]["source"] == "global"


async def test_a_failed_launch_still_restores_the_clone(
    app, git_checkout: Path, tmp_path: Path, global_additions: Path
) -> None:
    global_additions.write_text(GLOBAL_MARKER)
    engine = app.state.engine
    launcher = tmp_path / "failing-my-workshop"
    launcher.write_text("#!/bin/sh\necho boom >&2\nexit 3\n")
    launcher.chmod(0o755)
    config: Config = replace(app.state.config, my_workshop_command=(str(launcher),))

    create_project(
        engine,
        name="demo",
        title="Demo",
        upstream_url="https://example.com/demo.git",
        checkout_path=str(git_checkout),
        default_checkout_root=git_checkout.parent,
    )
    clone_path = clone_path_for(config.task_dir_root, "demo", "failing")
    task = create_task(
        engine,
        project_name="demo",
        slug="failing",
        branch="ompire/failing",
        clone_path=str(clone_path),
        prompt="",
        execution_inputs=make_execution_inputs(
            checkout_path=str(git_checkout),
            branch="ompire/failing",
            workshop_additions="global",
        ),
    )

    await run_spawn_pipeline(engine, EventHub(), config, task.id, _RecordingRunner())

    failed = get_task(engine, task.id)
    assert failed.state == "failed"
    assert "boom" in (failed.error or "")
    # The staging was undone even though the launcher failed.
    assert not (clone_path / LOCAL_ADDITIONS_FILENAME).exists()
    assert workshopadditions.recover_pending(config.data_dir) == []
