"""Layout of the operator's data directory, and the one-time carry-forward
of state left behind in a snap revision directory.

Revision-independent operator state: ADR-0024
(docs/adr/0024-keep-operator-state-outside-package-revisions.md)
Local persistence: ADR-0005
(docs/adr/0005-persist-local-state-with-sqlite-core-and-alembic.md)
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
from pathlib import Path

from ompire_daemon.auth import token_path_for
from ompire_daemon.db import db_path_for, ensure_db_dir

logger = logging.getLogger(__name__)

AUDIT_LOG_FILENAME = "audit.log"

# Prefix for files staged inside the destination before they are renamed into
# place. Distinct from the `.<name>.tmp` form `auth.write_token_file` uses, so
# a leftover from either is unambiguous.
_STAGE_PREFIX = ".carry-forward."


class DataCarryForwardError(RuntimeError):
    """Operator state could not be brought out of a snap revision directory."""


def audit_log_path_for(data_dir: Path) -> Path:
    return data_dir / AUDIT_LOG_FILENAME


def carry_forward_snap_state(data_dir: Path) -> Path | None:
    """Bring a previous snap revision's operator state into `data_dir`.

    Returns the directory the state came from, or `None` when there was
    nothing to do — which is every start but the first one after the snap
    stopped storing state per revision. Raises `DataCarryForwardError` when a
    carry-forward is needed and cannot be completed; starting with an empty
    database in place of the operator's own is not an acceptable fallback.

    The source is left exactly as it was. It is the operator's fallback copy,
    and deleting or rewriting it is not this daemon's call to make.
    """
    source = _revision_source(data_dir)
    if source is None:
        return None

    ensure_db_dir(db_path_for(data_dir))

    # Stage every file under a temporary name first, then rename them into
    # place with the database LAST. The database is the marker `_revision_source`
    # tests, so it must never appear without the write-ahead log and token
    # that belong with it: a half-carried directory would look already-carried
    # on the next start and quietly serve incomplete state.
    staged: list[tuple[Path, Path]] = []
    try:
        staged += _stage_token(source, data_dir)
        staged += _stage_audit_log(source, data_dir)
        staged += _stage_database(source, data_dir)
        for stage_path, final_path in staged:
            os.replace(stage_path, final_path)
    except OSError as exc:
        _discard(staged)
        raise DataCarryForwardError(
            f"could not carry operator state from {source} to {data_dir}: {exc}"
        ) from exc

    logger.info(
        "carried operator state (%s) from %s to %s",
        ", ".join(final.name for _, final in staged),
        source,
        data_dir,
    )
    return source


def _revision_source(data_dir: Path) -> Path | None:
    """The revision directory to carry state from, or `None` for none."""
    snap_user_common = os.environ.get("SNAP_USER_COMMON")
    if not snap_user_common or data_dir != Path(snap_user_common):
        # Either not running under a snap, or the operator chose this
        # directory in config.toml. A directory the operator named is theirs;
        # another install's state does not get written into it.
        return None

    snap_user_data = os.environ.get("SNAP_USER_DATA")
    if not snap_user_data:
        return None

    source = Path(snap_user_data)
    if source == data_dir:
        return None

    if db_path_for(data_dir).exists():
        # Already carried, or this install has state of its own. Either way
        # the destination wins; a carry-forward never overwrites.
        return None

    if not db_path_for(source).is_file():
        return None

    return source


def _stage_database(source: Path, data_dir: Path) -> list[tuple[Path, Path]]:
    """Stage the database and its write-ahead log, database last.

    Snapd stops the old user daemon before starting the new one, so there is
    no concurrent writer and a cold copy of both files is complete. Copying
    the database alone would silently drop everything the previous daemon
    committed but had not yet checkpointed.
    """
    staged = []
    source_db = db_path_for(source)
    dest_db = db_path_for(data_dir)

    source_wal = _wal_of(source_db)
    if source_wal.is_file():
        staged.append(_stage(source_wal, _wal_of(dest_db)))

    staged.append(_stage(source_db, dest_db))
    return staged


def _stage_token(source: Path, data_dir: Path) -> list[tuple[Path, Path]]:
    """Stage the bearer token verbatim.

    The same bytes mean a browser that already holds this token keeps working
    after the upgrade, with no token query string to re-open the UI with.
    """
    source_token = token_path_for(source)
    if not source_token.is_file():
        return []
    return [_stage(source_token, token_path_for(data_dir))]


def _stage_audit_log(source: Path, data_dir: Path) -> list[tuple[Path, Path]]:
    source_audit = audit_log_path_for(source)
    if not source_audit.is_file():
        return []
    return [_stage(source_audit, audit_log_path_for(data_dir))]


def _stage(source_path: Path, final_path: Path) -> tuple[Path, Path]:
    """Copy `source_path` beside `final_path` under a staging name.

    Opened with an explicit owner-only mode rather than copied with
    `shutil.copy2`, so the staged file is never briefly readable by anyone
    else, and flushed to disk before it is renamed into place: after the
    rename it is the operator's only copy in this directory.
    """
    stage_path = final_path.with_name(f"{_STAGE_PREFIX}{final_path.name}")
    fd = os.open(stage_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as dest, source_path.open("rb") as src:
            shutil.copyfileobj(src, dest)
            dest.flush()
            os.fsync(dest.fileno())
        stage_path.chmod(0o600)
    except Exception:
        with contextlib.suppress(OSError):
            stage_path.unlink()
        raise
    return stage_path, final_path


def _discard(staged: list[tuple[Path, Path]]) -> None:
    for stage_path, _ in staged:
        with contextlib.suppress(OSError):
            stage_path.unlink()


def _wal_of(db_path: Path) -> Path:
    return db_path.with_name(f"{db_path.name}-wal")
