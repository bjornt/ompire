"""Workshop CLI helper tests, driven against fake `workshop` scripts on PATH."""

from __future__ import annotations

from pathlib import Path

import pytest

from ompire_daemon.workshop import WorkshopRemoveError, remove_workshop, workshop_status


@pytest.fixture
def clone(tmp_path: Path) -> Path:
    clone_dir = tmp_path / "clone"
    clone_dir.mkdir()
    return clone_dir


async def test_status_present(fake_workshop_cli: Path, clone: Path) -> None:
    assert await workshop_status(str(clone)) == "present"


async def test_status_absent_on_not_launched(fake_workshop_cli: Path, clone: Path) -> None:
    fake_workshop_cli.write_text('#!/bin/sh\necho "error: workshop not launched" >&2\nexit 1\n')
    assert await workshop_status(str(clone)) == "absent"


async def test_status_unknown_on_tool_error(fake_workshop_cli: Path, clone: Path) -> None:
    fake_workshop_cli.write_text('#!/bin/sh\necho "boom" >&2\nexit 2\n')
    assert await workshop_status(str(clone)) == "unknown"


async def test_status_absent_when_clone_missing(fake_workshop_cli: Path, tmp_path: Path) -> None:
    assert await workshop_status(str(tmp_path / "gone")) == "absent"


async def test_remove_success(fake_workshop_cli: Path, clone: Path) -> None:
    await remove_workshop(str(clone), timeout=5)


async def test_remove_already_gone_is_success(fake_workshop_cli: Path, clone: Path) -> None:
    fake_workshop_cli.write_text('#!/bin/sh\necho "error: workshop not launched" >&2\nexit 1\n')
    await remove_workshop(str(clone), timeout=5)


async def test_remove_missing_clone_is_success(fake_workshop_cli: Path, tmp_path: Path) -> None:
    await remove_workshop(str(tmp_path / "gone"), timeout=5)


async def test_remove_failure_raises(fake_workshop_cli: Path, clone: Path) -> None:
    fake_workshop_cli.write_text('#!/bin/sh\necho "lxd exploded" >&2\nexit 1\n')
    with pytest.raises(WorkshopRemoveError) as excinfo:
        await remove_workshop(str(clone), timeout=5)
    assert "lxd exploded" in excinfo.value.stderr


async def test_remove_timeout_raises(fake_workshop_cli: Path, clone: Path) -> None:
    fake_workshop_cli.write_text("#!/bin/sh\nsleep 5\n")
    with pytest.raises(WorkshopRemoveError) as excinfo:
        await remove_workshop(str(clone), timeout=1)
    assert "timed out" in excinfo.value.stderr
