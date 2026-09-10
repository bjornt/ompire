"""Durable task-result registry: the manifest contract, the retained bytes, and
every mutation that changes what an operator can see about a result. No ORM —
Core queries only.

A *result* is an immutable capture of files a task produced, retained outside
its disposable workspace (ADR-0034). This module owns four things and
deliberately nothing else:

- the shape and identity of a manifest, so "this exact revision" is a value a
  client can name and a later request can be checked against;
- the bounded, purely syntactic rules a selected path must satisfy, which the
  filesystem capture in `results.py` then re-checks against real descriptors;
- reserved-write mutations, so a capture's bytes and its `ready` state commit
  together and an acceptance cannot race a purge;
- metadata projections that never load a byte of file content.

It performs no filesystem, Git, or network work. It never repairs a damaged
result from the workspace, and it never invents provenance: an unrecorded
producing run, step, or session is `unknown` in the manifest, which is a fact,
where a plausible guess would be a fabrication.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, Engine, func, select

from ompire_daemon.db import (
    task_result_files,
    task_result_references,
    task_results,
    tasks,
)
from ompire_daemon.registry.model_profiles import reserved_write

# --- The fixed bounds -------------------------------------------------------
#
# These are limits, not settings. They are what makes a bundle small enough to
# live transactionally in the operator's SQLite database, and small enough that
# a human can actually review every byte before accepting it. Raising them is a
# storage decision (ADR-0034), not a configuration change, so nothing reads
# them from config and no API accepts an override.

MAX_FILES = 128
MAX_FILE_BYTES = 1 * 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_PATH_COMPONENTS = 16
MAX_PATH_BYTES = 1024
MAX_VISITED_ENTRIES = 1024
CAPTURE_DEADLINE_SECONDS = 30.0
MAX_DIFF_BYTES = 1 * 1024 * 1024

# The name a downloaded ZIP reserves for Ompire's own manifest. A captured file
# may not use it: a bundle whose manifest could be shadowed by a captured file
# would let the agent describe its own result.
RESERVED_MANIFEST_NAME = "__ompire_result_manifest__.json"

# Extension → media type. The allowlist *is* the supported-type answer: an
# extension that is not here is refused, rather than captured as some generic
# byte stream whose preview nobody can promise is inert.
MEDIA_TYPES: dict[str, str] = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".json": "application/json",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
}

SUPPORTED_EXTENSIONS = tuple(sorted(MEDIA_TYPES))

MANIFEST_FORMAT = 2

# Capture states. `capturing` is in flight, `failed` is a capture that produced
# no bundle at all, `ready` is a complete retained revision, and `purged` is a
# tombstone whose bytes an operator explicitly removed. Acceptance is *not* a
# state: it is an independent decision that survives the result becoming
# unreadable, because the operator really did make it.
STATE_CAPTURING = "capturing"
STATE_FAILED = "failed"
STATE_READY = "ready"
STATE_PURGED = "purged"

# Control characters and the separators that would make a component something
# other than a single ordinary name.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class DamagedManifestError(ValueError):
    """A retained manifest cannot be read as one. The revision describes
    nothing trustworthy, so it is reported unavailable rather than served from
    whatever the file rows happen to hold."""


class ResultNotFoundError(Exception):
    def __init__(self, result_id: str) -> None:
        super().__init__(f"result {result_id!r} not found")
        self.result_id = result_id


class InvalidSelectionError(ValueError):
    """One selected entry cannot be captured, with the offending entry named
    and its contents deliberately absent. Selection is all-or-nothing: a
    partially honoured selection is a bundle nobody asked for."""

    def __init__(self, entry: str, reason: str) -> None:
        super().__init__(f"cannot capture {entry!r}: {reason}")
        self.entry = entry
        self.reason = reason


class CaptureInProgressError(Exception):
    """A second capture for a task that already has one in flight. One at a
    time keeps 'what is the predecessor' answerable without a tie-break."""

    def __init__(self, task_id: int, result_id: str) -> None:
        super().__init__(
            f"task {task_id} already has capture {result_id} in progress; "
            "wait for it to finish before capturing again"
        )
        self.task_id = task_id
        self.result_id = result_id


class SelectionMismatchError(Exception):
    """A repeated request id asking for a different selection. Answering it
    would report on files the caller never requested under an id they believe
    identifies their own request."""

    def __init__(self, request_id: str, result_id: str) -> None:
        super().__init__(
            f"request {request_id!r} already captured a different selection "
            f"(result {result_id}); use a new request id to capture again"
        )
        self.request_id = request_id
        self.result_id = result_id


class ResultStateError(Exception):
    """The revision is not in a state this command accepts — still capturing,
    failed, unavailable, or purged. 409: the client refreshes and decides
    again against what is actually there."""

    def __init__(self, result_id: str, detail: str) -> None:
        super().__init__(f"result {result_id}: {detail}")
        self.result_id = result_id
        self.detail = detail


class ResultPurgedError(Exception):
    """410: the revision is a tombstone. Its identity, manifest, and decisions
    are still readable; its bytes are gone and are never reconstructed."""

    def __init__(self, result_id: str) -> None:
        super().__init__(f"result {result_id} was purged; its files are gone")
        self.result_id = result_id


class StaleRevisionError(Exception):
    """The caller named a manifest identity or task result version that is no
    longer current. Acceptance and purge both bind to exactly what the operator
    reviewed, so a mismatch is refused rather than retargeted."""

    def __init__(self, result_id: str, expected: str, actual: str | None) -> None:
        super().__init__(
            f"result {result_id} no longer matches the reviewed revision "
            f"(expected {expected}, found {actual or 'none'}); refresh and "
            "review the current revision before deciding"
        )
        self.result_id = result_id
        self.expected = expected
        self.actual = actual


class ResultsRetainedError(Exception):
    """Task purge refused: the task still owns result bytes. Named revisions,
    so the operator can purge exactly those and retry — there is no bypass."""

    def __init__(self, task_id: int, result_ids: list[str]) -> None:
        super().__init__(
            f"task {task_id} still has {len(result_ids)} retained result "
            f"revision(s) ({', '.join(result_ids)}); purge them explicitly "
            "before purging the task"
        )
        self.task_id = task_id
        self.result_ids = result_ids


class ResultReferencedError(Exception):
    """Result purge refused: consumer tasks were launched with these exact
    bytes as pinned inputs (ADR-0035).

    Names the consumers, because that is the only actionable correction: the
    operator purges those task records — under the ordinary task-purge rules,
    which have their own refusals — or keeps the revision. There is no force,
    and a refused consumer purge releases nothing.
    """

    def __init__(self, result_id: str, consumer_task_ids: list[int]) -> None:
        super().__init__(
            f"result {result_id} is pinned as an input by "
            f"{len(consumer_task_ids)} task(s) "
            f"({', '.join(str(task_id) for task_id in consumer_task_ids)}); "
            "purge those task records explicitly before purging this revision"
        )
        self.result_id = result_id
        self.consumer_task_ids = consumer_task_ids


class ResultNotAttachableError(Exception):
    """This revision cannot be pinned as a launch input, and why.

    Deliberately distinct from `ResultStateError`: attachment additionally
    requires the operator's acceptance and an intact payload, so "you have not
    accepted this" and "this is still capturing" are different corrections.
    """

    def __init__(self, result_id: str, reason: str, detail: str) -> None:
        super().__init__(f"result {result_id}: {detail}")
        self.result_id = result_id
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class ResultFile:
    """One retained file's description, as the manifest records it. The
    manifest is the single source: nothing stores a second, independently
    mutable copy of a length or a checksum to disagree with it."""

    path: str
    length: int
    sha256: str
    media_type: str


@dataclass(frozen=True)
class TaskResult:
    id: str
    task_id: int
    request_id: str
    selection: tuple[str, ...]
    selection_fingerprint: str
    state: str
    error: str | None
    unavailable_reason: str | None
    manifest: dict[str, Any] | None
    manifest_id: str | None
    content_id: str | None
    predecessor_id: str | None
    workflow_seq: int | None
    workflow_provenance: dict[str, Any] | None
    started_at: str
    finished_at: str | None
    accepted_at: str | None
    accepted_by: str | None
    purged_at: str | None
    purged_by: str | None

    @property
    def accepted(self) -> bool:
        return self.accepted_at is not None

    @property
    def available(self) -> bool:
        """Whether this revision's bytes can still be read and served.

        Deliberately independent of acceptance: a `ready` revision whose
        integrity check failed is unavailable *and* keeps its acceptance,
        because the decision was real even though the payload no longer is.
        """
        return self.state == STATE_READY and self.unavailable_reason is None

    @property
    def files(self) -> tuple[ResultFile, ...]:
        return manifest_files(self.manifest)

    @property
    def total_bytes(self) -> int:
        return sum(entry.length for entry in self.files)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def new_result_id() -> str:
    """An opaque capture identity, independent of content.

    Two captures of byte-identical files are two results: they were produced at
    different moments by different work, and merging them would merge their
    provenance. Content equality is recognizable through `content_id` instead.
    """
    return f"res_{secrets.token_hex(16)}"


# --- Selection: purely syntactic rules --------------------------------------


def validate_relative_path(entry: str) -> str:
    """Check one repository-relative selection entry and return its normalized
    form, or raise `InvalidSelectionError`.

    This is the *syntax* boundary only. It cannot say anything about what is on
    disk — a component that names a symlink today is an ordinary name here, and
    `results.py` rejects it against a real descriptor. Both checks exist because
    neither alone is sufficient: a resolved string path is not a trusted capture
    boundary, and a descriptor walk still needs to know which strings are even
    worth opening.
    """
    if not entry or not entry.strip():
        raise InvalidSelectionError(entry, "empty selection entry")
    text = entry.strip()
    if _CONTROL_RE.search(text):
        raise InvalidSelectionError(entry, "path contains control characters")
    if "\\" in text:
        raise InvalidSelectionError(entry, "backslashes are not path separators")
    if text.startswith("/"):
        raise InvalidSelectionError(entry, "absolute paths are not accepted")
    if len(text.encode("utf-8")) > MAX_PATH_BYTES:
        raise InvalidSelectionError(
            entry, f"path exceeds {MAX_PATH_BYTES} bytes"
        )
    components = text.split("/")
    if len(components) > MAX_PATH_COMPONENTS:
        raise InvalidSelectionError(
            entry, f"path has more than {MAX_PATH_COMPONENTS} components"
        )
    for component in components:
        if component == "":
            raise InvalidSelectionError(entry, "empty path component")
        if component in (".", ".."):
            raise InvalidSelectionError(entry, "relative path components")
        if component.startswith("."):
            # `.git`, `.ompire`, credential and session directories all live
            # behind this one rule. Naming them individually would invite the
            # next hidden namespace to be forgotten.
            raise InvalidSelectionError(
                entry, "dot-prefixed names are never captured"
            )
    if components[-1] == RESERVED_MANIFEST_NAME:
        raise InvalidSelectionError(
            entry, f"{RESERVED_MANIFEST_NAME} is reserved for Ompire's manifest"
        )
    return text


def media_type_for(path: str) -> str:
    """The media type of a supported file, or raise for an unsupported one."""
    suffix = path[path.rfind(".") :] if "." in path.rsplit("/", 1)[-1] else ""
    media = MEDIA_TYPES.get(suffix.lower())
    if media is None:
        raise InvalidSelectionError(
            path,
            "unsupported file type; supported extensions are "
            + ", ".join(SUPPORTED_EXTENSIONS),
        )
    return media


def normalize_selection(entries: Sequence[str]) -> tuple[str, ...]:
    """Validate, de-duplicate, and order a selection.

    Overlapping selections include a file once, so the normalized form is what
    the fingerprint and the manifest both record. Order is deterministic
    because two spellings of the same request must produce the same
    fingerprint — otherwise a replay would look like a different selection.
    """
    if not entries:
        raise InvalidSelectionError("", "no paths selected")
    normalized = {validate_relative_path(entry) for entry in entries}
    return tuple(sorted(normalized))


def selection_fingerprint(selection: Sequence[str]) -> str:
    payload = json.dumps(list(selection), separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --- Manifest: canonical form and identity ----------------------------------


def canonical_json(document: Mapping[str, Any]) -> str:
    """One byte-for-byte reproducible encoding, so an identity means the same
    thing on every machine and across every restart."""
    return json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def manifest_identity(manifest: Mapping[str, Any]) -> str:
    """Hash the *complete* manifest. This is the revision binding: acceptance
    names it, and any change to what was captured — bytes, paths, provenance,
    capture time — yields a different one."""
    return hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()


def content_identity(files: Iterable[ResultFile]) -> str:
    """Hash only the file set. Two captures with equal bytes share this and
    keep separate `id`s and provenance, so an unchanged bundle is recognizable
    without merging the two pieces of work that produced it."""
    entries = [
        [entry.path, entry.media_type, entry.length, entry.sha256]
        for entry in sorted(files, key=lambda item: item.path)
    ]
    return hashlib.sha256(
        json.dumps(entries, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def build_manifest(
    *,
    result_id: str,
    task_id: int,
    project_name: str,
    selection: Sequence[str],
    files: Sequence[ResultFile],
    predecessor_id: str | None,
    provenance: Mapping[str, Any],
    captured_at: str,
    capture_actor: str = "operator",
    input_results: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """The immutable description of exactly what was retained.

    Version two adds immutable task-input references. Old manifests remain
    readable as format one; no row is rewritten to claim linkage it never
    recorded.
    """
    ordered = sorted(files, key=lambda entry: entry.path)
    return {
        "format": MANIFEST_FORMAT,
        "result_id": result_id,
        "task_id": task_id,
        "project_name": project_name,
        "captured_at": captured_at,
        "capture_actor": capture_actor,
        "selection": list(selection),
        "files": [
            {
                "path": entry.path,
                "length": entry.length,
                "sha256": entry.sha256,
                "media_type": entry.media_type,
            }
            for entry in ordered
        ],
        "file_count": len(ordered),
        "total_bytes": sum(entry.length for entry in ordered),
        "predecessor_id": predecessor_id,
        "provenance": dict(provenance),
        "input_results": [dict(item) for item in input_results],
    }


def manifest_files(manifest: Mapping[str, Any] | None) -> tuple[ResultFile, ...]:
    """Read a manifest's file list, refusing a structurally damaged one.

    Called before *any* retained path is trusted. A manifest that cannot be
    read this way describes nothing, so the revision is unavailable rather
    than served from whatever the file rows happen to contain.
    """
    if manifest is None:
        return ()
    raw = manifest.get("files")
    if not isinstance(raw, list):
        raise DamagedManifestError("manifest has no file list")
    entries: list[ResultFile] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise DamagedManifestError("manifest file entry is not an object")
        path = item.get("path")
        length = item.get("length")
        digest = item.get("sha256")
        media = item.get("media_type")
        if not isinstance(path, str) or not isinstance(length, int):
            raise DamagedManifestError("manifest file entry has no usable path/length")
        if not isinstance(digest, str) or not isinstance(media, str):
            raise DamagedManifestError("manifest file entry has no usable checksum/type")
        # Re-run the syntax rules on the *retained* path. A manifest is read
        # back after a restart, and a path that would be refused today must
        # not become a ZIP member simply because it is already stored.
        validate_relative_path(path)
        if path in seen:
            raise DamagedManifestError(f"manifest lists {path!r} twice")
        seen.add(path)
        entries.append(
            ResultFile(path=path, length=length, sha256=digest, media_type=media)
        )
    if not entries:
        raise DamagedManifestError("manifest lists no files")
    return tuple(sorted(entries, key=lambda entry: entry.path))


# --- Row decoding -----------------------------------------------------------


def _row_to_result(row) -> TaskResult:
    manifest = json.loads(row.manifest_json) if row.manifest_json else None
    return TaskResult(
        id=row.id,
        task_id=row.task_id,
        request_id=row.request_id,
        selection=tuple(json.loads(row.selection_json)),
        selection_fingerprint=row.selection_fingerprint,
        state=row.state,
        error=row.error,
        unavailable_reason=row.unavailable_reason,
        manifest=manifest,
        manifest_id=row.manifest_id,
        content_id=row.content_id,
        predecessor_id=row.predecessor_id,
        workflow_seq=row.workflow_seq,
        workflow_provenance=(
            json.loads(row.workflow_provenance_json)
            if row.workflow_provenance_json
            else None
        ),
        started_at=row.started_at,
        finished_at=row.finished_at,
        accepted_at=row.accepted_at,
        accepted_by=row.accepted_by,
        purged_at=row.purged_at,
        purged_by=row.purged_by,
    )


_METADATA_COLUMNS = (
    task_results.c.id,
    task_results.c.task_id,
    task_results.c.request_id,
    task_results.c.selection_fingerprint,
    task_results.c.selection_json,
    task_results.c.state,
    task_results.c.error,
    task_results.c.unavailable_reason,
    task_results.c.manifest_json,
    task_results.c.manifest_id,
    task_results.c.content_id,
    task_results.c.predecessor_id,
    task_results.c.workflow_seq,
    task_results.c.workflow_provenance_json,
    task_results.c.started_at,
    task_results.c.finished_at,
    task_results.c.accepted_at,
    task_results.c.accepted_by,
    task_results.c.purged_at,
    task_results.c.purged_by,
)


def _metadata_select():
    """Every result column except the file BLOBs — which live in another table
    entirely, so metadata reads can never accidentally drag megabytes into a
    projection or a fleet-wide broadcast."""
    return select(*_METADATA_COLUMNS)


# --- Version projection -----------------------------------------------------


def results_version(engine: Engine, task_id: int) -> int:
    with engine.connect() as conn:
        row = conn.execute(
            select(tasks.c.results_version).where(tasks.c.id == task_id)
        ).first()
    return int(row.results_version) if row is not None else 0


def bump_results_version(conn: Connection, task_id: int) -> int:
    """Advance the task's result projection version inside the caller's
    reservation, so every observable mutation and its version move together.

    Public because the export journal (ADR-0036) rides the same projection: an
    export transition is something the operator can see about a result, so it
    has to move the version the client reconciles against.
    """
    conn.execute(
        tasks.update()
        .where(tasks.c.id == task_id)
        .values(results_version=tasks.c.results_version + 1)
    )
    row = conn.execute(
        select(tasks.c.results_version).where(tasks.c.id == task_id)
    ).first()
    return int(row.results_version) if row is not None else 0


# --- Reads ------------------------------------------------------------------


def list_results(engine: Engine, task_id: int) -> list[TaskResult]:
    """Newest first, metadata only."""
    with engine.connect() as conn:
        rows = conn.execute(
            _metadata_select()
            .where(task_results.c.task_id == task_id)
            .order_by(task_results.c.started_at.desc(), task_results.c.id.desc())
        ).all()
    return [_row_to_result(row) for row in rows]


def list_tasks_with_results(engine: Engine) -> dict[int, list[TaskResult]]:
    """Every task that has any result row, for the snapshot projection."""
    with engine.connect() as conn:
        rows = conn.execute(
            _metadata_select().order_by(
                task_results.c.started_at.desc(), task_results.c.id.desc()
            )
        ).all()
    grouped: dict[int, list[TaskResult]] = {}
    for row in rows:
        grouped.setdefault(row.task_id, []).append(_row_to_result(row))
    return grouped


def get_result(engine: Engine, result_id: str) -> TaskResult:
    with engine.connect() as conn:
        row = conn.execute(
            _metadata_select().where(task_results.c.id == result_id)
        ).first()
    if row is None:
        raise ResultNotFoundError(result_id)
    return _row_to_result(row)


def read_file_bytes(engine: Engine, result_id: str, path: str) -> bytes | None:
    """One retained file's exact bytes, or None if the row is missing.

    Missing is a real answer here — an integrity failure the caller classifies
    — rather than an exception, because the caller is already comparing against
    the manifest and needs to report *which* file is gone.
    """
    with engine.connect() as conn:
        row = conn.execute(
            select(task_result_files.c.content)
            .where(task_result_files.c.result_id == result_id)
            .where(task_result_files.c.relative_path == path)
        ).first()
    return bytes(row.content) if row is not None else None


def read_all_files(engine: Engine, result_id: str) -> dict[str, bytes]:
    with engine.connect() as conn:
        return read_all_files_on(conn, result_id)


def read_all_files_on(conn: Connection, result_id: str) -> dict[str, bytes]:
    """Every retained file on the caller's connection.

    The connection-scoped form so materialization can read the bytes on the
    same connection that just re-verified their identity, rather than proving
    one thing and then reading another.
    """
    rows = conn.execute(
        select(task_result_files.c.relative_path, task_result_files.c.content).where(
            task_result_files.c.result_id == result_id
        )
    ).all()
    return {row.relative_path: bytes(row.content) for row in rows}


# --- Mutations --------------------------------------------------------------


def open_capture(
    engine: Engine,
    *,
    task_id: int,
    request_id: str,
    selection: Sequence[str],
) -> tuple[TaskResult, bool]:
    """Admit one operator capture under its caller-owned replay key."""
    return _open_capture(
        engine,
        task_id=task_id,
        request_id=request_id,
        selection=selection,
        workflow_seq=None,
        workflow_provenance=None,
    )


def open_workflow_capture(
    engine: Engine,
    *,
    task_id: int,
    workflow_seq: int,
    selection: Sequence[str],
    provenance: Mapping[str, Any],
) -> tuple[TaskResult, bool]:
    """Admit a capture owned by one persisted workflow attempt.

    This is intentionally not a variation of the public request-id API:
    callers cannot claim the reserved workflow sequence or its provenance.
    Re-driving the same attempt returns its original operation instead of
    reading current workspace bytes.
    """
    if workflow_seq < 1:
        raise ValueError("workflow sequence must be positive")
    return _open_capture(
        engine,
        task_id=task_id,
        request_id=f"workflow:{workflow_seq}:{secrets.token_hex(16)}",
        selection=selection,
        workflow_seq=workflow_seq,
        workflow_provenance=provenance,
    )


def _open_capture(
    engine: Engine,
    *,
    task_id: int,
    request_id: str,
    selection: Sequence[str],
    workflow_seq: int | None,
    workflow_provenance: Mapping[str, Any] | None,
) -> tuple[TaskResult, bool]:
    fingerprint = selection_fingerprint(selection)
    selection_json = json.dumps(list(selection), separators=(",", ":"))
    now = _now_iso()
    result_id = new_result_id()
    with reserved_write(engine) as conn:
        existing = (
            conn.execute(
                _metadata_select()
                .where(task_results.c.task_id == task_id)
                .where(task_results.c.workflow_seq == workflow_seq)
            ).first()
            if workflow_seq is not None
            else conn.execute(
                _metadata_select()
                .where(task_results.c.task_id == task_id)
                .where(task_results.c.request_id == request_id)
            ).first()
        )
        if existing is not None:
            if existing.selection_fingerprint != fingerprint:
                raise SelectionMismatchError(
                    f"workflow:{workflow_seq}" if workflow_seq is not None else request_id,
                    existing.id,
                )
            return _row_to_result(existing), False
        in_flight = conn.execute(
            select(task_results.c.id)
            .where(task_results.c.task_id == task_id)
            .where(task_results.c.state == STATE_CAPTURING)
        ).first()
        if in_flight is not None:
            raise CaptureInProgressError(task_id, in_flight.id)
        predecessor = conn.execute(
            select(task_results.c.id)
            .where(task_results.c.task_id == task_id)
            .where(task_results.c.state == STATE_READY)
            .order_by(task_results.c.started_at.desc(), task_results.c.id.desc())
        ).first()
        conn.execute(
            task_results.insert().values(
                id=result_id,
                task_id=task_id,
                request_id=request_id,
                selection_fingerprint=fingerprint,
                selection_json=selection_json,
                state=STATE_CAPTURING,
                error=None,
                unavailable_reason=None,
                manifest_json=None,
                manifest_id=None,
                content_id=None,
                predecessor_id=predecessor.id if predecessor is not None else None,
                workflow_seq=workflow_seq,
                workflow_provenance_json=(
                    canonical_json(workflow_provenance)
                    if workflow_provenance is not None
                    else None
                ),
                started_at=now,
                finished_at=None,
                accepted_at=None,
                accepted_by=None,
                purged_at=None,
                purged_by=None,
            )
        )
        bump_results_version(conn, task_id)
        row = conn.execute(
            _metadata_select().where(task_results.c.id == result_id)
        ).one()
        return _row_to_result(row), True


def finish_capture(
    engine: Engine,
    result_id: str,
    *,
    manifest: Mapping[str, Any],
    contents: Mapping[str, bytes],
) -> TaskResult:
    """Commit the bytes, the manifest, and `ready` in one transaction.

    This single-transaction shape is the whole reason recovery is simple: there
    is no state in which a revision is `ready` with some of its files, so
    restart never has to adopt a partly copied bundle or re-read a workspace
    that may no longer exist.
    """
    entries = manifest_files(manifest)
    missing = [entry.path for entry in entries if entry.path not in contents]
    if missing:
        raise ValueError(f"manifest names files that were not captured: {missing}")
    identity = manifest_identity(manifest)
    content = content_identity(entries)
    encoded = canonical_json(manifest)
    now = _now_iso()
    with reserved_write(engine) as conn:
        row = conn.execute(
            _metadata_select().where(task_results.c.id == result_id)
        ).first()
        if row is None:
            raise ResultNotFoundError(result_id)
        if row.state != STATE_CAPTURING:
            raise ResultStateError(result_id, f"is {row.state}, not capturing")
        conn.execute(
            task_result_files.insert(),
            [
                {
                    "result_id": result_id,
                    "relative_path": entry.path,
                    "content": contents[entry.path],
                }
                for entry in entries
            ],
        )
        conn.execute(
            task_results.update()
            .where(task_results.c.id == result_id)
            .values(
                state=STATE_READY,
                manifest_json=encoded,
                manifest_id=identity,
                content_id=content,
                finished_at=now,
            )
        )
        bump_results_version(conn, row.task_id)
        updated = conn.execute(
            _metadata_select().where(task_results.c.id == result_id)
        ).one()
        return _row_to_result(updated)


def fail_capture(engine: Engine, result_id: str, error: str) -> TaskResult:
    """Record a capture that produced no bundle.

    A failed capture is history, not a partial result: it has no manifest, no
    files, and never becomes another revision's predecessor. Existing complete
    revisions are untouched, which is what lets an operator correct a selection
    and retry without risking what they already accepted.
    """
    now = _now_iso()
    with reserved_write(engine) as conn:
        row = conn.execute(
            _metadata_select().where(task_results.c.id == result_id)
        ).first()
        if row is None:
            raise ResultNotFoundError(result_id)
        if row.state != STATE_CAPTURING:
            return _row_to_result(row)
        conn.execute(
            task_results.update()
            .where(task_results.c.id == result_id)
            .values(state=STATE_FAILED, error=error, finished_at=now)
        )
        bump_results_version(conn, row.task_id)
        updated = conn.execute(
            _metadata_select().where(task_results.c.id == result_id)
        ).one()
        return _row_to_result(updated)


def accept_result(
    engine: Engine, result_id: str, *, expected_manifest_id: str
) -> TaskResult:
    """Record the operator's decision about exactly this revision.

    Idempotent by design: repeating a successful acceptance returns the
    original decision rather than restamping it, so a retried request cannot
    quietly rewrite when a result was accepted or by whom. Accepting a
    successor leaves every earlier decision exactly as it was — there is no
    floating "latest accepted" that retargets anything.
    """
    now = _now_iso()
    with reserved_write(engine) as conn:
        row = conn.execute(
            _metadata_select().where(task_results.c.id == result_id)
        ).first()
        if row is None:
            raise ResultNotFoundError(result_id)
        if row.state == STATE_PURGED:
            raise ResultPurgedError(result_id)
        if row.state != STATE_READY:
            raise ResultStateError(
                result_id, f"is {row.state} and cannot be accepted"
            )
        if row.unavailable_reason is not None:
            raise ResultStateError(
                result_id,
                f"is unavailable ({row.unavailable_reason}) and cannot be accepted",
            )
        if row.manifest_id != expected_manifest_id:
            raise StaleRevisionError(result_id, expected_manifest_id, row.manifest_id)
        if row.accepted_at is not None:
            return _row_to_result(row)
        conn.execute(
            task_results.update()
            .where(task_results.c.id == result_id)
            .values(accepted_at=now, accepted_by="operator")
        )
        bump_results_version(conn, row.task_id)
        updated = conn.execute(
            _metadata_select().where(task_results.c.id == result_id)
        ).one()
        return _row_to_result(updated)


def mark_unavailable(engine: Engine, result_id: str, reason: str) -> TaskResult:
    """Classify a retained revision as unreadable, keeping its history.

    Written when an integrity check fails at the read boundary. It never
    discards the acceptance and never consults the workspace: the operator's
    decision happened, and the current clone is not evidence about what was
    captured months ago.
    """
    with reserved_write(engine) as conn:
        row = conn.execute(
            _metadata_select().where(task_results.c.id == result_id)
        ).first()
        if row is None:
            raise ResultNotFoundError(result_id)
        if row.state != STATE_READY or row.unavailable_reason == reason:
            return _row_to_result(row)
        conn.execute(
            task_results.update()
            .where(task_results.c.id == result_id)
            .values(unavailable_reason=reason)
        )
        bump_results_version(conn, row.task_id)
        updated = conn.execute(
            _metadata_select().where(task_results.c.id == result_id)
        ).one()
        return _row_to_result(updated)


def purge_result(
    engine: Engine,
    result_id: str,
    *,
    expected_manifest_id: str,
    expected_version: int,
) -> TaskResult:
    """Remove one revision's retained bytes, atomically, leaving a tombstone.

    Both expectations are checked under the reservation: the manifest identity
    proves the operator confirmed *this* revision, and the task result version
    proves nothing else changed between the confirmation dialog and the
    command. Repeating a completed purge is idempotent; a retry can therefore
    never remove a different revision.

    A revision pinned as a launch input by any consumer task is refused
    outright, naming those consumers: retention is a dependency, not a
    preference, and there is no force (ADR-0035). A revision with an unfinished
    checkout export is refused the same way, and named — but only until that
    export settles (ADR-0036).

    This is logical removal. The row keeps identity, manifest, provenance, and
    both decisions; the bytes are deleted from the table. SQLite may reuse the
    freed pages later, and it makes no promise about backups or copies the
    operator already downloaded.
    """
    from ompire_daemon.registry.result_exports import (
        assert_result_exports_settled_on,
    )

    now = _now_iso()
    with reserved_write(engine) as conn:
        row = conn.execute(
            _metadata_select().where(task_results.c.id == result_id)
        ).first()
        if row is None:
            raise ResultNotFoundError(result_id)
        if row.state == STATE_CAPTURING:
            raise ResultStateError(
                result_id, "is still capturing and cannot be purged"
            )
        if row.manifest_id != expected_manifest_id:
            raise StaleRevisionError(result_id, expected_manifest_id, row.manifest_id)
        if row.state == STATE_PURGED:
            return _row_to_result(row)
        # Checked on *this* reservation, before anything is deleted: a launch
        # accepting these bytes commits its task and its references in one
        # transaction, so either that consumer exists and this purge is
        # refused, or it does not and the purge proceeds. There is no ordering
        # in which both win (ADR-0035).
        assert_result_unpinned_on(conn, result_id)
        # An export that is still running, or whose effects nobody could
        # classify, is reading these bytes or is unexplained. That protection
        # is temporary and releases itself (ADR-0036): a completed export is a
        # delivered copy, not a consumer reference.
        assert_result_exports_settled_on(conn, result_id)
        current = conn.execute(
            select(tasks.c.results_version).where(tasks.c.id == row.task_id)
        ).first()
        actual_version = int(current.results_version) if current is not None else 0
        if actual_version != expected_version:
            raise StaleRevisionError(
                result_id, str(expected_version), str(actual_version)
            )
        conn.execute(
            task_result_files.delete().where(
                task_result_files.c.result_id == result_id
            )
        )
        conn.execute(
            task_results.update()
            .where(task_results.c.id == result_id)
            .values(state=STATE_PURGED, purged_at=now, purged_by="operator")
        )
        bump_results_version(conn, row.task_id)
        updated = conn.execute(
            _metadata_select().where(task_results.c.id == result_id)
        ).one()
        return _row_to_result(updated)


def reconcile_interrupted_captures(engine: Engine) -> list[TaskResult]:
    """Turn every capture the last daemon left in flight into a visible failure.

    Run before any result command is accepted or any snapshot is served. No
    workspace is re-read: the interrupted capture's files may be long gone, and
    quietly capturing today's bytes under yesterday's request id is exactly the
    substitution the request-id contract exists to prevent.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            select(task_results.c.id).where(task_results.c.state == STATE_CAPTURING)
        ).all()
    return [
        fail_capture(
            engine,
            row.id,
            "the daemon restarted while this capture was running; nothing was "
            "retained. Capture again when the workspace is available.",
        )
        for row in rows
    ]


def assert_no_retained_results(conn: Connection, task_id: int) -> None:
    """Refuse task purge while the task owns bytes or an in-flight capture.

    Called *before* any destructive work, on the caller's own reserved
    connection, so a refusal cannot happen after review or delivery history has
    already been deleted. Purged tombstones do not block: their bytes are gone
    and their record travels with the rest of the task's history.
    """
    rows = conn.execute(
        select(task_results.c.id, task_results.c.state)
        .where(task_results.c.task_id == task_id)
        .where(task_results.c.state.in_((STATE_READY, STATE_CAPTURING)))
        .order_by(task_results.c.started_at)
    ).all()
    if rows:
        raise ResultsRetainedError(task_id, [row.id for row in rows])


def delete_task_results(conn: Connection, task_id: int) -> None:
    """Delete a task's remaining result tombstones on the caller's connection.

    Only reachable once `assert_no_retained_results` has passed, so this can
    never be the thing that destroys retained bytes.
    """
    ids = [
        row.id
        for row in conn.execute(
            select(task_results.c.id).where(task_results.c.task_id == task_id)
        ).all()
    ]
    if ids:
        conn.execute(
            task_result_files.delete().where(
                task_result_files.c.result_id.in_(ids)
            )
        )
        conn.execute(task_results.delete().where(task_results.c.task_id == task_id))


def retained_counts(engine: Engine) -> dict[int, dict[str, int]]:
    """Per-task retained/total counts and byte totals for the Tasks index.

    Byte totals come from the manifests, never from `length(content)`: the
    manifest is what acceptance was bound to, and a disagreement between the
    two is an integrity failure to report rather than a number to display.
    """
    counts: dict[int, dict[str, int]] = {}
    with engine.connect() as conn:
        rows = conn.execute(
            _metadata_select().order_by(task_results.c.started_at)
        ).all()
    for row in rows:
        entry = counts.setdefault(
            row.task_id,
            {"total": 0, "retained": 0, "accepted": 0, "bytes": 0},
        )
        entry["total"] += 1
        if row.state == STATE_READY:
            entry["retained"] += 1
            try:
                entry["bytes"] += sum(
                    item.length
                    for item in manifest_files(
                        json.loads(row.manifest_json) if row.manifest_json else None
                    )
                )
            except (ValueError, json.JSONDecodeError):
                # A damaged manifest contributes no byte total. The revision's
                # own detail view reports the damage; a dashboard count is not
                # the place to guess a size for it.
                pass
        if row.accepted_at is not None:
            entry["accepted"] += 1
    return counts


def count_in_flight(engine: Engine, task_id: int) -> int:
    with engine.connect() as conn:
        row = conn.execute(
            select(func.count())
            .select_from(task_results)
            .where(task_results.c.task_id == task_id)
            .where(task_results.c.state == STATE_CAPTURING)
        ).first()
    return int(row[0]) if row is not None else 0


# --- Attachment: connection-scoped validation ------------------------------
#
# Everything below takes a `Connection` rather than an `Engine`, because a
# launch validates these revisions *inside* the reservation that creates the
# consumer task. Opening a second connection there would put the check and the
# admission in different transactions, which is exactly how a purge committing
# between them would leave a task pinned to bytes that are already gone.
#
# They are also deliberately free of the manager's side effects: no
# damage-classification write, no version bump, no broadcast. A refusal must
# never nest a second write reservation inside the caller's.


def read_result_on(conn: Connection, result_id: str) -> TaskResult | None:
    """One result's metadata on the caller's connection, or None."""
    row = conn.execute(
        _metadata_select().where(task_results.c.id == result_id)
    ).first()
    return _row_to_result(row) if row is not None else None


def verify_attachable_on(
    conn: Connection, result_id: str, *, expected_manifest_id: str
) -> TaskResult:
    """The revision this launch may pin, or a refusal naming the reason.

    Checks identity before content: a caller naming a manifest that is no
    longer this result's is told its selection is stale, rather than having a
    successor revision quietly substituted for the one it reviewed.
    """
    result = read_result_on(conn, result_id)
    if result is None:
        raise ResultNotFoundError(result_id)
    if result.state == STATE_PURGED:
        raise ResultNotAttachableError(
            result_id,
            "purged",
            "was purged; its files are gone and it cannot be attached",
        )
    if result.state != STATE_READY:
        raise ResultNotAttachableError(
            result_id, "incomplete", f"is {result.state} and cannot be attached"
        )
    if result.unavailable_reason is not None:
        raise ResultNotAttachableError(
            result_id,
            "unavailable",
            f"is unavailable ({result.unavailable_reason}) and cannot be attached",
        )
    if result.manifest_id != expected_manifest_id:
        raise StaleRevisionError(result_id, expected_manifest_id, result.manifest_id)
    if not result.accepted:
        raise ResultNotAttachableError(
            result_id,
            "not-accepted",
            "has not been accepted; review and accept the revision before "
            "launching a task from it",
        )
    return result


def verify_payload_on(conn: Connection, result: TaskResult) -> None:
    """Re-check the retained bytes against the manifest acceptance was bound to.

    Run at consumption, not only at capture: a revision accepted months ago is
    read back from a database that may have been restored, copied, or damaged
    since. A mismatch refuses the launch; it never repairs the manifest and
    never rewrites the bytes to agree with it.
    """
    entries = result.files
    rows = conn.execute(
        select(
            task_result_files.c.relative_path, task_result_files.c.content
        ).where(task_result_files.c.result_id == result.id)
    ).all()
    stored = {row.relative_path: row.content for row in rows}
    for entry in entries:
        data = stored.get(entry.path)
        if data is None:
            raise ResultNotAttachableError(
                result.id,
                "damaged",
                f"is missing retained bytes for {entry.path!r}",
            )
        if len(data) != entry.length:
            raise ResultNotAttachableError(
                result.id,
                "damaged",
                f"retained bytes for {entry.path!r} are {len(data)} bytes, "
                f"not the accepted {entry.length}",
            )
        if hashlib.sha256(data).hexdigest() != entry.sha256:
            raise ResultNotAttachableError(
                result.id,
                "damaged",
                f"retained bytes for {entry.path!r} do not match the accepted "
                "checksum",
            )
    extra = sorted(set(stored) - {entry.path for entry in entries})
    if extra:
        raise ResultNotAttachableError(
            result.id,
            "damaged",
            f"retains files the accepted manifest does not describe: {extra}",
        )


# --- Attachment: the reference index ---------------------------------------


def insert_references_on(
    conn: Connection,
    *,
    consumer_task_id: int,
    references: Sequence[tuple[str, int, str]],
) -> list[int]:
    """Reserve every referenced revision for one consumer, on this connection.

    `references` is `(result_id, producer_task_id, manifest_id)`. Returns the
    producing task ids whose result projection moved, so the caller can publish
    them *after* the transaction commits — a reverse-dependency list that a
    purge dialog is reading has to converge without the operator refreshing.
    """
    if not references:
        return []
    now = _now_iso()
    conn.execute(
        task_result_references.insert(),
        [
            {
                "consumer_task_id": consumer_task_id,
                "result_id": result_id,
                "producer_task_id": producer_task_id,
                "manifest_id": manifest_id,
                "created_at": now,
            }
            for result_id, producer_task_id, manifest_id in references
        ],
    )
    producers = sorted({producer for _id, producer, _m in references})
    for producer in producers:
        bump_results_version(conn, producer)
    return producers


def assert_result_unpinned_on(conn: Connection, result_id: str) -> None:
    """Refuse a result purge while any consumer task pins it.

    Called on the purge's own reserved connection, before a byte is deleted,
    so a refusal cannot happen after the tombstone was written.
    """
    rows = conn.execute(
        select(task_result_references.c.consumer_task_id)
        .where(task_result_references.c.result_id == result_id)
        .order_by(task_result_references.c.consumer_task_id)
    ).all()
    if rows:
        raise ResultReferencedError(
            result_id, [int(row.consumer_task_id) for row in rows]
        )


def release_consumer_references_on(conn: Connection, consumer_task_id: int) -> list[int]:
    """Drop one consumer's references once its own purge is certain.

    Only reachable after every task-purge refusal has passed, and inside the
    transaction that deletes the consumer row: a refused consumer purge
    releases nothing, and there is no window in which the task is gone while
    its references still protect bytes.
    """
    rows = conn.execute(
        select(task_result_references.c.producer_task_id).where(
            task_result_references.c.consumer_task_id == consumer_task_id
        )
    ).all()
    if not rows:
        return []
    conn.execute(
        task_result_references.delete().where(
            task_result_references.c.consumer_task_id == consumer_task_id
        )
    )
    producers = sorted({int(row.producer_task_id) for row in rows})
    for producer in producers:
        bump_results_version(conn, producer)
    return producers


def consumers_by_result(engine: Engine) -> dict[str, list[int]]:
    """Reverse dependencies for the whole database, for result projections."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                task_result_references.c.result_id,
                task_result_references.c.consumer_task_id,
            ).order_by(task_result_references.c.consumer_task_id)
        ).all()
    consumers: dict[str, list[int]] = {}
    for row in rows:
        consumers.setdefault(row.result_id, []).append(int(row.consumer_task_id))
    return consumers


def references_for_consumer(engine: Engine, consumer_task_id: int) -> list[str]:
    """The revisions one consumer task pins, in insertion order."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(task_result_references.c.result_id)
            .where(task_result_references.c.consumer_task_id == consumer_task_id)
            .order_by(task_result_references.c.created_at, task_result_references.c.result_id)
        ).all()
    return [row.result_id for row in rows]
