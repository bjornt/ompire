"""Carry-forward of operator state out of a snap revision directory (ADR-0024)."""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient

from alembic import command
from ompire_daemon import migrate
from ompire_daemon.app import create_app
from ompire_daemon.auth import token_path_for
from ompire_daemon.config import Config
from ompire_daemon.datadir import (
    DataCarryForwardError,
    audit_log_path_for,
    carry_forward_snap_state,
)
from ompire_daemon.db import db_path_for

# A trailing newline the daemon's own reader strips: the carry must preserve
# the file's bytes, not a normalised token value.
TOKEN_BYTES = b"carried-token-value\n"

skip_as_root = pytest.mark.skipif(
    os.geteuid() == 0, reason="root ignores the directory permissions this forces"
)


def _seed_revision(revision: Path) -> sqlite3.Connection:
    """Populate `revision` the way a previous install left it.

    The returned connection stays open on purpose. Everything committed in WAL
    mode lives in the write-ahead log until a checkpoint, and closing the last
    connection would fold it into the main database — hiding exactly the
    partial-copy bug these tests exist to catch.
    """
    db = db_path_for(revision)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE marker (value TEXT)")
    conn.execute("INSERT INTO marker VALUES ('checkpointed')")
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    # Committed after the checkpoint, so this row exists only in the WAL.
    conn.execute("INSERT INTO marker VALUES ('wal-only')")
    conn.commit()

    token_path_for(revision).write_bytes(TOKEN_BYTES)
    return conn


def _markers(db: Path) -> list[str]:
    conn = sqlite3.connect(db)
    try:
        return sorted(row[0] for row in conn.execute("SELECT value FROM marker"))
    finally:
        conn.close()


def _snapshot(directory: Path) -> dict[str, tuple[bytes, int, int]]:
    return {
        str(path.relative_to(directory)): (
            path.read_bytes(),
            path.stat().st_mtime_ns,
            stat.S_IMODE(path.stat().st_mode),
        )
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


@pytest.fixture
def snap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """An empty common directory and an empty revision directory."""
    common = tmp_path / "common"
    revision = tmp_path / "x8"
    common.mkdir()
    revision.mkdir()
    monkeypatch.setenv("SNAP_USER_COMMON", str(common))
    monkeypatch.setenv("SNAP_USER_DATA", str(revision))
    return common, revision


@pytest.fixture
def seeded(snap: tuple[Path, Path]) -> Iterator[tuple[Path, Path]]:
    """A revision directory holding a previous install's state."""
    common, revision = snap
    conn = _seed_revision(revision)
    try:
        yield common, revision
    finally:
        conn.close()


def test_carries_database_including_wal_only_rows(seeded: tuple[Path, Path]) -> None:
    common, revision = seeded

    assert carry_forward_snap_state(common) == revision

    assert _markers(db_path_for(common)) == ["checkpointed", "wal-only"]


def test_carries_token_byte_for_byte(seeded: tuple[Path, Path]) -> None:
    common, _ = seeded

    carry_forward_snap_state(common)

    assert token_path_for(common).read_bytes() == TOKEN_BYTES


def test_carries_audit_log_when_present(seeded: tuple[Path, Path]) -> None:
    common, revision = seeded
    audit_log_path_for(revision).write_text("shipped task 1\n")

    carry_forward_snap_state(common)

    assert audit_log_path_for(common).read_text() == "shipped task 1\n"


def test_carried_state_is_owner_only(seeded: tuple[Path, Path]) -> None:
    common, revision = seeded
    audit_log_path_for(revision).write_text("shipped task 1\n")
    token_path_for(revision).chmod(0o644)

    carry_forward_snap_state(common)

    assert stat.S_IMODE(db_path_for(common).parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(db_path_for(common).stat().st_mode) == 0o600
    assert stat.S_IMODE(token_path_for(common).stat().st_mode) == 0o600
    assert stat.S_IMODE(audit_log_path_for(common).stat().st_mode) == 0o600


def test_leaves_the_source_untouched(seeded: tuple[Path, Path]) -> None:
    """The revision directory is the operator's fallback copy."""
    common, revision = seeded
    audit_log_path_for(revision).write_text("shipped task 1\n")
    before = _snapshot(revision)

    carry_forward_snap_state(common)

    assert _snapshot(revision) == before


def test_second_start_carries_nothing(seeded: tuple[Path, Path]) -> None:
    common, _ = seeded
    carry_forward_snap_state(common)
    carried = _snapshot(common)

    assert carry_forward_snap_state(common) is None
    assert _snapshot(common) == carried


def test_never_overwrites_an_existing_database(seeded: tuple[Path, Path]) -> None:
    common, _ = seeded
    db_path_for(common).parent.mkdir(parents=True)
    db_path_for(common).write_bytes(b"this install's own database")

    assert carry_forward_snap_state(common) is None
    assert db_path_for(common).read_bytes() == b"this install's own database"


@pytest.mark.usefixtures("seeded")
def test_skips_an_operator_chosen_data_dir(tmp_path: Path) -> None:
    """A directory named in config.toml never receives another install's state."""
    chosen = tmp_path / "operator-chosen"
    chosen.mkdir()

    assert carry_forward_snap_state(chosen) is None
    assert not db_path_for(chosen).exists()


def test_skips_a_revision_dir_without_a_database(snap: tuple[Path, Path]) -> None:
    common, revision = snap
    token_path_for(revision).write_bytes(TOKEN_BYTES)

    assert carry_forward_snap_state(common) is None
    assert not token_path_for(common).exists()


def test_skips_outside_a_snap(
    seeded: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    common, _ = seeded
    monkeypatch.delenv("SNAP_USER_COMMON", raising=False)

    assert carry_forward_snap_state(common) is None
    assert not db_path_for(common).exists()


@skip_as_root
def test_failure_raises_and_leaves_no_database(seeded: tuple[Path, Path]) -> None:
    """An empty database in place of the operator's own is not a fallback."""
    common, revision = seeded
    audit_log_path_for(revision).write_text("shipped task 1\n")
    db_path_for(common).parent.mkdir(parents=True)
    db_path_for(common).parent.chmod(0o500)

    try:
        with pytest.raises(DataCarryForwardError) as excinfo:
            carry_forward_snap_state(common)
    finally:
        db_path_for(common).parent.chmod(0o700)

    assert str(revision) in str(excinfo.value)
    assert str(common) in str(excinfo.value)
    assert not db_path_for(common).exists()
    # Nothing half-carried is left behind for the retry to trip over.
    assert list(db_path_for(common).parent.iterdir()) == []
    assert not any(path.name.startswith(".carry-forward.") for path in common.iterdir())


def _upgrade_to(db_path: Path, revision: str) -> None:
    """Bring `db_path` to a specific past revision of the real migration chain."""
    cfg = AlembicConfig(str(migrate._ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, revision)


def test_create_app_migrates_a_carried_older_database(
    snap: tuple[Path, Path], tmp_path: Path
) -> None:
    """A carried database is an ordinary existing database to `upgrade_head`.

    The upgrade an operator actually performs brings new migrations with it,
    so the carry-forward has to land before the schema upgrade and needs no
    special casing once it has.
    """
    common, revision = snap
    source_db = db_path_for(revision)
    source_db.parent.mkdir(parents=True)
    _upgrade_to(source_db, "0010")
    conn = sqlite3.connect(source_db)
    conn.execute(
        "INSERT INTO projects (name, title, upstream_url, fork_url, checkout_path)"
        " VALUES ('ompire', 'Ompire', 'https://github.com/op/ompire', NULL,"
        " '/home/op/proj/ompire')"
    )
    conn.commit()
    conn.close()
    token_path_for(revision).write_bytes(TOKEN_BYTES)

    app = create_app(
        Config(
            data_dir=common,
            task_dir_root=tmp_path / "tasks",
            checkout_root=tmp_path / "proj",
        ),
        frontend_dist=tmp_path / "no-dist",
    )

    # The operator's browser already holds this token; it must keep working.
    assert app.state.auth_token == TOKEN_BYTES.decode().strip()

    response = TestClient(app).get(
        "/api/projects",
        headers={"Authorization": f"Bearer {app.state.auth_token}"},
    )

    assert response.status_code == 200
    projects = response.json()
    assert [project["name"] for project in projects] == ["ompire"]
    # Backfilled by 0011, so the migration ran against the carried rows.
    assert projects[0]["setup_state"] == "ready"
