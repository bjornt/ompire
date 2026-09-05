"""Make the accepted Workshop additions source the one that actually applies.

my-workshop resolves additions itself, and its rule is local-first: a
`workshop.my.yaml` next to the resolved `workshop.yaml` wins over the
operator's `~/.config/my-workshop/my.yaml`, and there is no flag to select a
source. So "leave the argv alone and hope" cannot honor a `global` selection
in a repository that ships its own additions, and cannot honor a `project`
selection in one that does not — it would silently fall back to the operator's
global file (ADR-0026 forbids exactly that implicit fallback).

The adapter is therefore a bounded, daemon-owned staging step around the
launcher:

1. before the launcher runs, put the *selected* source's content at the
   clone's local additions path — including an explicitly empty file when the
   selected source is absent, so the launcher's local-first rule cannot reach
   the other source;
2. run the launcher unchanged (its working directory, YAML detection,
   merging, and provisioning are its own contract, not reimplemented here);
3. restore the clone's original local file, or its original absence, whether
   the launcher succeeded or failed — before any agent starts.

The original content and the fact of the staging live in the daemon's data
directory, never in the clone: a backup inside the task's working tree would
be readable by the agent and would show up in status, diffs, and reviews.
The operator's own files are never modified — the registered checkout is not
touched at all, and the global additions file is only ever read.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# The launcher's project-local additions file, resolved beside `workshop.yaml`
# in the clone root.
LOCAL_ADDITIONS_FILENAME = "workshop.my.yaml"

# What is written when the selected source does not exist. A present but empty
# document is the honest encoding of "this source contributes no additions",
# and it is what keeps the launcher from falling through to the other source.
EMPTY_ADDITIONS = "# no additions: selected source is absent\n"

_STAGING_DIRNAME = "workshop-staging"


class WorkshopAdditionsError(Exception):
    """The selected additions source cannot be applied. Raised before the
    launcher runs, so the task fails workspace setup rather than starting an
    agent under an additions file nobody chose."""


def global_additions_path() -> Path:
    """`$XDG_CONFIG_HOME/my-workshop/my.yaml`, defaulting to
    `~/.config/my-workshop/my.yaml` — the launcher's own location."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "my-workshop" / "my.yaml"


@dataclass(frozen=True)
class StagedAdditions:
    """One in-flight staging. `note` is the operator-facing disclosure: an
    absent source is reported as "no additions", never as a successful
    application of the other source."""

    task_id: int
    clone_path: str
    source: str
    had_original: bool
    backup_path: str | None
    note: str


def _staging_dir(data_dir: Path) -> Path:
    path = data_dir / _STAGING_DIRNAME
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _local_path(clone_path: str) -> Path:
    """The clone's own additions path, refusing anything that is not a plain
    file directly inside the clone. A symlink here would let a repository
    redirect the daemon's write outside the task's workspace."""
    root = Path(clone_path).resolve()
    path = root / LOCAL_ADDITIONS_FILENAME
    if path.is_symlink():
        raise WorkshopAdditionsError(
            f"{path} is a symlink; refusing to stage Workshop additions through it"
        )
    if path.exists() and not path.is_file():
        raise WorkshopAdditionsError(f"{path} is not a regular file")
    return path


def _read_selected_source(source: str, clone_path: str) -> tuple[str | None, str]:
    """Return `(content, note)` for the accepted source.

    `None` content means the source is absent — no additions. An unreadable
    source is an error, not an empty result: the launcher's own loader treats
    a read failure as "no additions", which would quietly turn a broken file
    into a successful launch with nothing applied.
    """
    if source == "project":
        path = _local_path(clone_path)
        if not path.exists():
            return None, (
                f"project additions: {LOCAL_ADDITIONS_FILENAME} is not in this "
                "repository; launching with no additions"
            )
        try:
            return path.read_text(encoding="utf-8"), "project additions applied"
        except OSError as exc:
            raise WorkshopAdditionsError(
                f"cannot read the project's {LOCAL_ADDITIONS_FILENAME}: {exc}"
            ) from exc
    if source == "global":
        path = global_additions_path()
        if not path.exists():
            return None, (
                f"global additions: {path} does not exist; launching with no additions"
            )
        if path.is_symlink() and not path.resolve().is_file():
            raise WorkshopAdditionsError(f"global additions path {path} is not a file")
        try:
            return path.read_text(encoding="utf-8"), f"global additions applied from {path}"
        except OSError as exc:
            raise WorkshopAdditionsError(
                f"cannot read the global additions file {path}: {exc}"
            ) from exc
    raise WorkshopAdditionsError(f"unknown Workshop additions source {source!r}")


def stage(data_dir: Path, task_id: int, clone_path: str, source: str) -> StagedAdditions:
    """Put the selected source's additions in place for one launch.

    Always writes the local file — with the selected content, or with an
    explicitly empty document when the source is absent — so the launcher can
    only see the chosen source.
    """
    local = _local_path(clone_path)
    content, note = _read_selected_source(source, clone_path)

    staging_dir = _staging_dir(data_dir)
    backup_path: Path | None = None
    had_original = local.exists()
    if had_original:
        backup_path = staging_dir / f"{task_id}.original"
        try:
            backup_path.write_text(local.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError as exc:
            raise WorkshopAdditionsError(
                f"cannot preserve the clone's existing {LOCAL_ADDITIONS_FILENAME}: {exc}"
            ) from exc

    staged = StagedAdditions(
        task_id=task_id,
        clone_path=str(Path(clone_path).resolve()),
        source=source,
        had_original=had_original,
        backup_path=str(backup_path) if backup_path is not None else None,
        note=note,
    )
    # The record goes down *before* the clone is touched: a crash between the
    # two must leave something that says a restore is owed.
    _write_record(staging_dir, staged)
    try:
        local.write_text(content if content is not None else EMPTY_ADDITIONS, encoding="utf-8")
    except OSError as exc:
        restore(data_dir, staged)
        raise WorkshopAdditionsError(
            f"cannot write the staged {LOCAL_ADDITIONS_FILENAME}: {exc}"
        ) from exc
    return staged


def restore(data_dir: Path, staged: StagedAdditions) -> None:
    """Put the clone back exactly as it was — including back to *absent* —
    and drop the staging record. Idempotent."""
    local = Path(staged.clone_path) / LOCAL_ADDITIONS_FILENAME
    try:
        if staged.had_original and staged.backup_path is not None:
            local.write_text(
                Path(staged.backup_path).read_text(encoding="utf-8"), encoding="utf-8"
            )
        elif local.exists():
            local.unlink()
    except OSError as exc:
        # Restoration failing is worth shouting about — the clone now carries
        # a daemon-written file — but it must not mask the launch outcome.
        logger.error(
            "could not restore %s for task %d: %s", local, staged.task_id, exc
        )
    staging_dir = _staging_dir(data_dir)
    for path in (
        staging_dir / f"{staged.task_id}.json",
        staging_dir / f"{staged.task_id}.original",
    ):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("could not remove staging file %s: %s", path, exc)


def _write_record(staging_dir: Path, staged: StagedAdditions) -> None:
    path = staging_dir / f"{staged.task_id}.json"
    path.write_text(
        json.dumps(
            {
                "task_id": staged.task_id,
                "clone_path": staged.clone_path,
                "source": staged.source,
                "had_original": staged.had_original,
                "backup_path": staged.backup_path,
                "note": staged.note,
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def recover_pending(data_dir: Path) -> list[int]:
    """Undo any staging a crash left behind, before agents are started.

    Returns the task ids restored. Called from startup, ahead of task
    recovery: an agent must never come up in a clone still carrying another
    source's additions.
    """
    staging_dir = data_dir / _STAGING_DIRNAME
    if not staging_dir.is_dir():
        return []
    restored: list[int] = []
    for record_path in sorted(staging_dir.glob("*.json")):
        try:
            document = json.loads(record_path.read_text(encoding="utf-8"))
            staged = StagedAdditions(
                task_id=int(document["task_id"]),
                clone_path=document["clone_path"],
                source=document["source"],
                had_original=bool(document["had_original"]),
                backup_path=document.get("backup_path"),
                note=document.get("note", ""),
            )
        except (OSError, ValueError, KeyError) as exc:
            logger.warning("ignoring unreadable staging record %s: %s", record_path, exc)
            continue
        logger.info(
            "restoring interrupted Workshop additions staging for task %d",
            staged.task_id,
        )
        restore(data_dir, staged)
        restored.append(staged.task_id)
    return restored
