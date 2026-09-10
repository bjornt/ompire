"""Trusted durable-result capture, inspection, and download.

`ResultManager` is the host-side authority over a task's durable results
(ADR-0034). It reads bytes out of a task's workspace under the *daemon's*
privileges, retains them outside that workspace, and serves them back with
their integrity checked. The task's agent never names what is captured, never
sees the store, and gains nothing from a capture: a retained result grants no
publication, no execution, and no workflow progress.

Two boundaries do the real work here.

The first is the filesystem boundary. A resolved string path followed by an
ordinary `open` is not a trusted capture boundary — between resolving and
opening, any component can become a symlink pointing anywhere the daemon can
read. So every component is opened relative to the previous directory's
descriptor with `O_NOFOLLOW`, every entry is type-, link-, and device-checked
on the descriptor that was actually opened, and every file's identity and
metadata are re-checked *after* it is read. A source that changed during
capture refuses the whole capture rather than retaining a half-consistent
bundle.

The second is the content boundary. Only the allowlisted text types are
supported, encoding is validated strictly, and recognizable credential material
is a refusal — never a redaction, because storing altered bytes under a
checksum the operator will later trust is worse than storing nothing. That
check is bounded and is not a claim that arbitrary secrets can be recognized:
downloaded result files remain sensitive untrusted data.

Everything durable lives in `registry/results.py`. This module performs no
database writes of its own beyond calling that registry's reserved-write
mutations, and holds no SQLite write reservation across a filesystem read or
an `await`.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import stat
import time
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self

from sqlalchemy import Engine

from ompire_daemon.config import Config
from ompire_daemon.delivery import (
    WorkspaceBlockedError,
    WorkspaceBusyError,
    WorkspaceGuard,
    run_git,
    safe_git,
)
from ompire_daemon.events import EventHub
from ompire_daemon.gh import redact_github_text
from ompire_daemon.registry.result_exports import (
    ExportRecord,
    export_payload,
    exports_by_result,
)
from ompire_daemon.registry.results import (
    CAPTURE_DEADLINE_SECONDS,
    MAX_DIFF_BYTES,
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_PATH_BYTES,
    MAX_PATH_COMPONENTS,
    MAX_TOTAL_BYTES,
    MAX_VISITED_ENTRIES,
    MEDIA_TYPES,
    RESERVED_MANIFEST_NAME,
    STATE_PURGED,
    STATE_READY,
    SUPPORTED_EXTENSIONS,
    DamagedManifestError,
    InvalidSelectionError,
    ResultFile,
    ResultNotFoundError,
    ResultPurgedError,
    ResultStateError,
    TaskResult,
    accept_result,
    build_manifest,
    consumers_by_result,
    fail_capture,
    finish_capture,
    get_result,
    list_results,
    list_tasks_with_results,
    manifest_files,
    mark_unavailable,
    normalize_selection,
    open_capture,
    open_workflow_capture,
    purge_result,
    read_all_files,
    read_file_bytes,
    reconcile_interrupted_captures,
    results_version,
    retained_counts,
)
from ompire_daemon.work.tasks import Task, get_task, list_tasks

logger = logging.getLogger(__name__)

# The guard owner label a capture takes. It is a HOST owner: capture reads the
# whole selected tree, so it excludes review, drafting, delivery and cleanup
# exactly as they exclude it.
CAPTURE_OWNER = "result-capture"

# Private-key blocks are recognized structurally rather than by provider, so an
# unfamiliar key format is still refused.
_PRIVATE_KEY_MARKERS = (
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN DSA PRIVATE KEY-----",
    "-----BEGIN EC PRIVATE KEY-----",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "-----BEGIN PGP PRIVATE KEY BLOCK-----",
    "-----BEGIN PRIVATE KEY-----",
    "-----BEGIN ENCRYPTED PRIVATE KEY-----",
)


class CaptureError(Exception):
    """A capture that produced no bundle, with an operator-actionable reason.

    The message names the offending path and what was wrong with it, and never
    carries file content, a matched credential, or a token value — a refusal
    that quotes the secret it found has published it into the result history
    and the daemon log.
    """


class ResultUnavailableError(Exception):
    """A retained revision failed its integrity check. Carries the classified
    reason; the revision keeps its acceptance and its history."""

    def __init__(self, result_id: str, reason: str) -> None:
        super().__init__(f"result {result_id} is unavailable: {reason}")
        self.result_id = result_id
        self.reason = reason


@dataclass(frozen=True)
class _CapturedFile:
    path: str
    data: bytes
    sha256: str
    media_type: str


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# --- The filesystem boundary ------------------------------------------------


class _Walker:
    """One capture's bounded, descriptor-relative read of a task workspace.

    Constructed per capture and used from a worker thread. It owns every
    descriptor it opens and closes them all on the way out, including on the
    error paths — a capture that refuses must not leak the handle it refused
    on.
    """

    def __init__(self, root: Path, deadline: float) -> None:
        self._root_path = root
        self._deadline = deadline
        self._visited = 0
        self._root_fd: int | None = None
        self._root_dev: int | None = None
        self._owned: list[int] = []

    def __enter__(self) -> Self:
        try:
            fd = os.open(
                self._root_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
        except OSError as exc:
            raise CaptureError(
                f"this task's workspace is not readable ({exc.strerror}); it may "
                "already have been cleaned up"
            ) from exc
        self._root_fd = fd
        info = os.fstat(fd)
        self._root_dev = info.st_dev
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close_collected()
        if self._root_fd is not None:
            os.close(self._root_fd)
            self._root_fd = None

    # -- bounds --

    def _check_deadline(self) -> None:
        if time.monotonic() > self._deadline:
            raise CaptureError(
                f"capture exceeded its {int(CAPTURE_DEADLINE_SECONDS)}s deadline; "
                "select fewer or smaller paths and capture again"
            )

    def _count_visit(self, entry: str) -> None:
        self._visited += 1
        if self._visited > MAX_VISITED_ENTRIES:
            raise CaptureError(
                f"selection visits more than {MAX_VISITED_ENTRIES} entries "
                f"(reached at {entry!r}); select a narrower set of paths"
            )

    # -- traversal --

    def _open_dir(self, parent_fd: int, name: str, display: str) -> int:
        """Open one directory component, refusing a symlink at that component.

        `O_NOFOLLOW` on a directory open is the whole guarantee: if `name` is a
        symlink — even one created between the previous entry's check and this
        call — the open fails rather than following it somewhere outside the
        workspace.
        """
        try:
            return os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
        except NotADirectoryError:
            raise InvalidSelectionError(display, "not a directory") from None
        except FileNotFoundError:
            raise InvalidSelectionError(display, "no such path") from None
        except OSError as exc:
            # ELOOP is the symlink refusal; anything else is reported the same
            # way, because the operator's next move is identical.
            raise InvalidSelectionError(
                display, f"cannot be opened safely ({exc.strerror})"
            ) from exc

    def _assert_same_device(self, info: os.stat_result, display: str) -> None:
        if info.st_dev != self._root_dev:
            raise InvalidSelectionError(
                display, "is on a different filesystem than the workspace"
            )

    def collect(
        self, selection: tuple[str, ...]
    ) -> list[tuple[int, str, str, os.stat_result]]:
        """Expand the selection into an exact allowlist.

        Each entry is `(dir_fd, name, path, admitted)`. The descriptors are the
        *directories* holding each file, still open, so the read step never has
        to re-resolve a path it already validated, and `admitted` is the exact
        inode the size and link checks were made against — the read compares
        against it, so a file swapped between admission and read is caught
        rather than quietly read in place of the one that passed. Overlapping
        selections yield each file once.
        """
        found: dict[str, tuple[int, str, os.stat_result]] = {}
        owned: list[int] = []
        try:
            for entry in selection:
                self._check_deadline()
                parent_fd = self._root_fd
                assert parent_fd is not None
                components = entry.split("/")
                for component in components[:-1]:
                    fd = self._open_dir(parent_fd, component, entry)
                    owned.append(fd)
                    parent_fd = fd
                leaf = components[-1]
                self._count_visit(entry)
                try:
                    info = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    raise InvalidSelectionError(entry, "no such path") from None
                except OSError as exc:
                    raise InvalidSelectionError(
                        entry, f"cannot be read ({exc.strerror})"
                    ) from exc
                self._assert_same_device(info, entry)
                if stat.S_ISLNK(info.st_mode):
                    raise InvalidSelectionError(entry, "symlinks are not captured")
                if stat.S_ISDIR(info.st_mode):
                    fd = self._open_dir(parent_fd, leaf, entry)
                    owned.append(fd)
                    self._collect_dir(fd, entry, found, owned)
                elif stat.S_ISREG(info.st_mode):
                    self._admit_file(info, entry)
                    found[entry] = (parent_fd, leaf, info)
                else:
                    raise InvalidSelectionError(
                        entry, "is not a regular file or directory"
                    )
            if not found:
                raise CaptureError(
                    "the selection contained no supported files; supported "
                    f"extensions are {', '.join(SUPPORTED_EXTENSIONS)}"
                )
            if len(found) > MAX_FILES:
                raise CaptureError(
                    f"selection expands to {len(found)} files, more than the "
                    f"limit of {MAX_FILES}"
                )
        except BaseException:
            for fd in owned:
                with contextlib.suppress(OSError):
                    os.close(fd)
            raise
        self._owned = owned
        return [
            (fd, name, path, info)
            for path, (fd, name, info) in sorted(found.items())
        ]

    def close_collected(self) -> None:
        for fd in self._owned:
            with contextlib.suppress(OSError):
                os.close(fd)
        self._owned = []

    def _collect_dir(
        self,
        dir_fd: int,
        prefix: str,
        found: dict[str, tuple[int, str, os.stat_result]],
        owned: list[int],
    ) -> None:
        """Enumerate one selected directory without following links.

        Every entry is validated before it is accepted, and an *ineligible*
        entry inside a selected directory fails the whole capture. Silently
        skipping it would hand the operator a bundle that quietly omits part of
        what they selected — which is exactly the "partially successful result"
        this design refuses to produce.
        """
        self._check_deadline()
        try:
            names = sorted(os.listdir(dir_fd))
        except OSError as exc:
            raise InvalidSelectionError(
                prefix, f"cannot be listed ({exc.strerror})"
            ) from exc
        for name in names:
            child = f"{prefix}/{name}"
            self._count_visit(child)
            self._check_deadline()
            if name.startswith("."):
                # `.git`, `.ompire`, editor state and credential directories.
                # A hidden name inside a selected directory is skipped rather
                # than refused: the operator selected a planning directory, not
                # its incidental metadata, and refusing here would make it
                # impossible to capture any real directory.
                continue
            if len(child.encode("utf-8")) > MAX_PATH_BYTES:
                raise InvalidSelectionError(child, "path is too long")
            if len(child.split("/")) > MAX_PATH_COMPONENTS:
                raise InvalidSelectionError(
                    child, f"path exceeds {MAX_PATH_COMPONENTS} components"
                )
            if name == RESERVED_MANIFEST_NAME:
                raise InvalidSelectionError(
                    child, f"{RESERVED_MANIFEST_NAME} is reserved for Ompire"
                )
            try:
                info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except OSError as exc:
                raise InvalidSelectionError(
                    child, f"cannot be read ({exc.strerror})"
                ) from exc
            if stat.S_ISLNK(info.st_mode):
                raise InvalidSelectionError(child, "symlinks are not captured")
            self._assert_same_device(info, child)
            if stat.S_ISDIR(info.st_mode):
                fd = self._open_dir(dir_fd, name, child)
                owned.append(fd)
                self._collect_dir(fd, child, found, owned)
            elif stat.S_ISREG(info.st_mode):
                if _extension_of(name) not in MEDIA_TYPES:
                    # An unsupported file *inside* a selected directory is a
                    # refusal, not a skip: the operator asked for this
                    # directory's contents, and quietly dropping part of them
                    # would misdescribe what the bundle is.
                    raise InvalidSelectionError(
                        child,
                        "unsupported file type; supported extensions are "
                        + ", ".join(SUPPORTED_EXTENSIONS),
                    )
                self._admit_file(info, child)
                found[child] = (dir_fd, name, info)
            else:
                raise InvalidSelectionError(
                    child, "is not a regular file or directory"
                )

    def _admit_file(self, info: os.stat_result, display: str) -> None:
        if info.st_nlink != 1:
            raise InvalidSelectionError(
                display, "is a hard link to another file and is not captured"
            )
        if info.st_size > MAX_FILE_BYTES:
            raise InvalidSelectionError(
                display,
                f"is {info.st_size} bytes, over the {MAX_FILE_BYTES}-byte "
                "per-file limit",
            )
        if _extension_of(display.rsplit("/", 1)[-1]) not in MEDIA_TYPES:
            raise InvalidSelectionError(
                display,
                "unsupported file type; supported extensions are "
                + ", ".join(SUPPORTED_EXTENSIONS),
            )

    def read(
        self, dir_fd: int, name: str, display: str, admitted: os.stat_result
    ) -> bytes:
        """Read one admitted file, bounded, and prove it did not change.

        Three separate comparisons, because they catch three different races.
        The opened inode is compared against `admitted`, so a file replaced
        between admission and this read is refused rather than silently read in
        place of the one that passed the checks. The size bound is enforced
        against the *stream* rather than the size `stat` reported, so a file
        that grows during capture is refused instead of truncated. And the
        descriptor's metadata plus the directory entry are re-checked
        afterwards, so a file replaced *during* the read cannot pass as the one
        that was opened.
        """
        self._check_deadline()
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
        except OSError as exc:
            raise InvalidSelectionError(
                display, f"cannot be opened safely ({exc.strerror})"
            ) from exc
        try:
            before = os.fstat(fd)
            if (
                before.st_ino != admitted.st_ino
                or before.st_dev != admitted.st_dev
                or before.st_mtime_ns != admitted.st_mtime_ns
                or before.st_size != admitted.st_size
            ):
                raise InvalidSelectionError(
                    display, "changed after it was selected for capture"
                )
            if not stat.S_ISREG(before.st_mode):
                raise InvalidSelectionError(display, "is not a regular file")
            if before.st_nlink != 1:
                raise InvalidSelectionError(
                    display, "is a hard link to another file and is not captured"
                )
            self._assert_same_device(before, display)
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(fd, 64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_FILE_BYTES:
                    raise InvalidSelectionError(
                        display,
                        f"grew past the {MAX_FILE_BYTES}-byte per-file limit "
                        "while being captured",
                    )
                chunks.append(chunk)
                self._check_deadline()
            after = os.fstat(fd)
            if (
                after.st_ino != before.st_ino
                or after.st_dev != before.st_dev
                or after.st_mtime_ns != before.st_mtime_ns
                or after.st_size != total
            ):
                raise InvalidSelectionError(
                    display, "changed while it was being captured"
                )
            try:
                entry = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            except OSError as exc:
                raise InvalidSelectionError(
                    display, f"disappeared while being captured ({exc.strerror})"
                ) from exc
            if entry.st_ino != before.st_ino or entry.st_dev != before.st_dev:
                raise InvalidSelectionError(
                    display, "was replaced while it was being captured"
                )
            return b"".join(chunks)
        finally:
            os.close(fd)


def _extension_of(name: str) -> str:
    return name[name.rfind(".") :].lower() if "." in name else ""


# --- The content boundary ---------------------------------------------------


def credential_token_values(config: Config) -> tuple[str, ...]:
    """The literal secrets the recognizer must never let through.

    Read on every use rather than held on a manager, so a rotated token is
    recognized without a restart. Shared with checkout export (ADR-0036): the
    same values have to be recognized in a *destination* file a preview would
    otherwise display back to the operator.
    """
    try:
        token = (config.data_dir / "token").read_text().strip() or None
    except OSError:
        token = None
    return tuple(
        value
        for value in (
            os.environ.get("GH_TOKEN"),
            os.environ.get("GITHUB_TOKEN"),
            token,
        )
        if value
    )


def validate_content(display: str, data: bytes, token_values: tuple[str, ...]) -> str:
    """Decode strictly and refuse recognizable credentials.

    Returns the decoded text purely so the caller does not decode twice; the
    *stored* bytes are always the exact bytes read. Nothing here rewrites,
    normalizes, or redacts content: a capture either retains a file unchanged
    or refuses it.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidSelectionError(
            display, f"is not valid UTF-8 (byte {exc.start})"
        ) from None
    for marker in _PRIVATE_KEY_MARKERS:
        if marker in text:
            raise InvalidSelectionError(
                display,
                "contains a private key block; remove it from the workspace "
                "before capturing",
            )
    # Reused as a *recognizer*, never as a redactor: if redaction would change
    # anything, there is credential material here and the file is refused.
    if redact_github_text(text, token_values) != text:
        raise InvalidSelectionError(
            display,
            "contains recognizable credential material (a token, an "
            "authorization header, or a credential-bearing URL); remove it "
            "from the workspace before capturing",
        )
    return text


# --- The manager ------------------------------------------------------------


class ResultManager:
    """Capture, retain, inspect, and purge one Ompire's durable task results."""

    def __init__(
        self,
        config: Config,
        engine: Engine,
        events: EventHub,
        guard: WorkspaceGuard,
    ) -> None:
        self._config = config
        self._engine = engine
        self._hub = events
        self._guard = guard
        self._jobs: set[asyncio.Task[Any]] = set()

    # -- startup / shutdown --

    def restore(self) -> list[TaskResult]:
        """Turn interrupted captures into visible failures.

        Called before any result command is accepted and before the first
        snapshot is served, so a client can never see a `capturing` row from a
        daemon that is no longer running.
        """
        reconciled = reconcile_interrupted_captures(self._engine)
        for result in reconciled:
            logger.warning(
                "task %d capture %s was interrupted by a restart",
                result.task_id,
                result.id,
            )
        return reconciled

    async def shutdown(self) -> None:
        jobs = list(self._jobs)
        for job in jobs:
            job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        # A capture cancelled before its job ever ran never reached its own
        # interruption handler, so its row is still `capturing`. Reconcile here
        # rather than leaving an in-flight-looking capture for the next start
        # to explain: the same rule applies either way — nothing was retained,
        # and a retry is an explicit new capture.
        self.restore()

    # -- projections --

    def projection(self, task_id: int) -> dict[str, Any]:
        """One task's complete, metadata-only result document.

        Whole-document by design: a client applies it as a replacement keyed by
        `version`, so a missed or duplicated delta still converges. No file
        content is ever in here — content is fetched for the revision the
        operator actually selected.
        """
        results = list_results(self._engine, task_id)
        consumers = consumers_by_result(self._engine)
        exports = exports_by_result(self._engine)
        return {
            "task_id": task_id,
            "version": results_version(self._engine, task_id),
            "results": [
                self.result_payload(result, consumers=consumers, exports=exports)
                for result in results
            ],
        }

    def snapshot(self) -> dict[int, dict[str, Any]]:
        """Every task that has any result row. A task with none is absent
        rather than carrying an empty document around the fleet."""
        grouped = list_tasks_with_results(self._engine)
        versions = {task.id: task for task in list_tasks(self._engine)}
        consumers = consumers_by_result(self._engine)
        exports = exports_by_result(self._engine)
        payload: dict[int, dict[str, Any]] = {}
        for task_id, results in grouped.items():
            if task_id not in versions:
                continue
            payload[task_id] = {
                "task_id": task_id,
                "version": results_version(self._engine, task_id),
                "results": [
                    self.result_payload(result, consumers=consumers, exports=exports)
                    for result in results
                ],
            }
        return payload

    def retained_index(self) -> dict[int, dict[str, int]]:
        """Per-task counts for the Tasks index's Retained results section."""
        return retained_counts(self._engine)

    def publish(self, task_id: int) -> dict[str, Any]:
        projection = self.projection(task_id)
        self._hub.publish("task_results_updated", projection)
        return projection

    def result_payload(
        self,
        result: TaskResult,
        *,
        consumers: Mapping[str, list[int]] | None = None,
        exports: Mapping[str, list[ExportRecord]] | None = None,
    ) -> dict[str, Any]:
        """The wire shape of one revision, shared by REST and the projection so
        a client cannot see two different shapes for the same row.

        `consumer_task_ids` is the reverse dependency (ADR-0035): the tasks
        that pinned this exact revision as an input, and therefore the reason a
        purge would be refused. Passed in rather than queried per row so a
        fleet projection stays one query.

        `exports` is the checkout-export history (ADR-0036), metadata only. A
        running or unresolved entry is a *temporary* purge blocker; a settled
        one is history, because the copies it delivered are ordinary files in
        the operator's checkout and outlive everything here.
        """
        try:
            files = [
                {
                    "path": entry.path,
                    "length": entry.length,
                    "sha256": entry.sha256,
                    "media_type": entry.media_type,
                }
                for entry in result.files
            ]
            damaged: str | None = None
        except DamagedManifestError as exc:
            # A manifest that cannot be read describes nothing. Say so; do not
            # fall back to listing whatever file rows happen to exist.
            files = []
            damaged = f"the retained manifest is unreadable: {exc}"
        manifest = result.manifest or {}
        return {
            "id": result.id,
            "task_id": result.task_id,
            "state": result.state,
            "error": result.error,
            "unavailable_reason": result.unavailable_reason or damaged,
            "available": result.available and damaged is None,
            "manifest_id": result.manifest_id,
            "content_id": result.content_id,
            "predecessor_id": result.predecessor_id,
            "workflow_seq": result.workflow_seq,
            "workflow_provenance": result.workflow_provenance,
            "selection": list(result.selection),
            "files": files,
            "file_count": len(files),
            "total_bytes": sum(entry["length"] for entry in files),
            "provenance": manifest.get("provenance"),
            "input_results": manifest.get("input_results", []),
            "captured_at": manifest.get("captured_at") or result.started_at,
            "started_at": result.started_at,
            "finished_at": result.finished_at,
            "accepted_at": result.accepted_at,
            "accepted_by": result.accepted_by,
            "purged_at": result.purged_at,
            "purged_by": result.purged_by,
            "consumer_task_ids": list(
                (consumers if consumers is not None else consumers_by_result(self._engine))
                .get(result.id, ())
            ),
            "exports": [
                export_payload(record)
                for record in (
                    exports if exports is not None else exports_by_result(self._engine)
                ).get(result.id, ())
            ],
        }

    # -- capture --

    def _admit(self, task_id: int) -> Task:
        """Every capture admission check, for REST and direct callers alike.

        Re-read, not passed in: the caller's `Task` may be seconds old, and an
        archived task or a workspace another operation just took is exactly
        what this has to notice.
        """
        task = get_task(self._engine, task_id)
        if task.state == "archived":
            raise ResultStateError(
                str(task_id),
                "this task has been cleaned up; its workspace is gone and "
                "nothing further can be captured from it",
            )
        clone_path = Path(task.clone_path).resolve()
        task_root = self._config.task_dir_root.expanduser().resolve()
        if task_root not in clone_path.parents:
            raise CaptureError(
                f"refusing to read {clone_path}: outside the task root {task_root}"
            )
        # The same guard review, drafting, delivery and cleanup use. Capture
        # never interrupts a writer; it refuses, so the operator's work is not
        # disturbed by their own capture request.
        self._guard.assert_available(task_id)
        return task

    async def capture(
        self, task_id: int, *, paths: list[str], request_id: str
    ) -> dict[str, Any]:
        """Admit a capture, record its identity, and supervise it to a result.

        Returns the metadata projection immediately. The operation outlives the
        request: a browser that disconnects still gets a committed `ready` or
        `failed` revision, recoverable from the task's result history rather
        than by capturing different bytes under a new identity.
        """
        task = self._admit(task_id)
        selection = normalize_selection(paths)
        result, created = open_capture(
            self._engine,
            task_id=task_id,
            request_id=request_id,
            selection=selection,
        )
        if not created:
            # A replay. The original operation — including its failure — is
            # the answer; nothing new is started.
            return self.projection(task_id)
        projection = self.publish(task_id)
        job = asyncio.create_task(self._run_capture(task, result.id, selection))
        self._jobs.add(job)
        job.add_done_callback(self._jobs.discard)
        return projection

    async def capture_workflow(
        self,
        task: Task,
        *,
        workflow_seq: int,
        paths: list[str],
        allowlist: tuple[str, ...],
        provenance: dict[str, Any],
    ) -> TaskResult:
        """Capture an attempt-owned declaration and return that exact result.

        No REST request can enter here: the runner supplies its persisted
        sequence and frozen producer binding. A retry of the same attempt
        returns the operation already linked to it and never rereads the clone.
        """
        selection = normalize_selection(paths)
        for path in selection:
            if not any(path.startswith(root + "/") for root in allowlist):
                raise InvalidSelectionError(
                    path, "is outside this workflow capture's declared allowlist"
                )
        self._admit(task.id)
        result, created = open_workflow_capture(
            self._engine,
            task_id=task.id,
            workflow_seq=workflow_seq,
            selection=selection,
            provenance=provenance,
        )
        if created:
            self.publish(task.id)
            await self._run_capture(task, result.id, selection)
        return get_result(self._engine, result.id)

    async def _run_capture(
        self, task: Task, result_id: str, selection: tuple[str, ...]
    ) -> None:
        """Own the workspace, read the bytes, and commit exactly one outcome."""
        try:
            async with self._guard.hold(task.id, CAPTURE_OWNER):
                # Re-read at the last moment: cleanup may have archived the
                # task between admission and acquiring the guard.
                self._admit(task.id)
                captured = await self._read_bounded(Path(task.clone_path), selection)
                result = get_result(self._engine, result_id)
                provenance = await self._provenance(
                    task, workflow=result.workflow_provenance
                )
                manifest = build_manifest(
                    result_id=result_id,
                    task_id=task.id,
                    project_name=task.project_name,
                    selection=selection,
                    files=[
                        ResultFile(
                            path=item.path,
                            length=len(item.data),
                            sha256=item.sha256,
                            media_type=item.media_type,
                        )
                        for item in captured
                    ],
                    predecessor_id=result.predecessor_id,
                    provenance=provenance,
                    captured_at=_now_iso(),
                    capture_actor="workflow" if result.workflow_seq is not None else "operator",
                    input_results=self._input_results(task),
                )
                finish_capture(
                    self._engine,
                    result_id,
                    manifest=manifest,
                    contents={item.path: item.data for item in captured},
                )
        except asyncio.CancelledError:
            # The worker thread is already finished — `shield` above keeps the
            # guard held until it is — so the capture is only ever recorded as
            # interrupted, never abandoned mid-read.
            fail_capture(
                self._engine,
                result_id,
                "the capture was cancelled before it finished; nothing was retained",
            )
            self.publish(task.id)
            raise
        except (CaptureError, InvalidSelectionError, WorkspaceBusyError,
                WorkspaceBlockedError, ResultStateError) as exc:
            fail_capture(self._engine, result_id, str(exc))
        except Exception as exc:  # noqa: BLE001 — one capture must not kill the daemon
            logger.warning("capture %s failed unexpectedly: %s", result_id, exc)
            fail_capture(self._engine, result_id, f"capture failed: {exc}")
        self.publish(task.id)

    async def _read_bounded(
        self, clone_path: Path, selection: tuple[str, ...]
    ) -> list[_CapturedFile]:
        """Run the filesystem read on a worker thread, and never release the
        workspace while that thread is still reading.

        A blocking `os.read` cannot be interrupted, so a cancelled capture must
        wait for its own worker before unwinding — otherwise the guard would be
        released, cleanup could start deleting the clone, and the thread would
        still be reading out of it. The wait is bounded by the capture deadline
        it is already subject to.
        """
        worker = asyncio.ensure_future(
            asyncio.to_thread(self._read_workspace, clone_path, selection)
        )
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(
                    asyncio.shield(worker), timeout=CAPTURE_DEADLINE_SECONDS + 5
                )
            raise

    def _read_workspace(
        self, clone_path: Path, selection: tuple[str, ...]
    ) -> list[_CapturedFile]:
        """The whole bounded filesystem read, off the event loop."""
        import hashlib

        deadline = time.monotonic() + CAPTURE_DEADLINE_SECONDS
        token_values = credential_token_values(self._config)
        captured: list[_CapturedFile] = []
        total = 0
        with _Walker(clone_path, deadline) as walker:
            entries = walker.collect(selection)
            try:
                for dir_fd, name, display, admitted in entries:
                    data = walker.read(dir_fd, name, display, admitted)
                    total += len(data)
                    if total > MAX_TOTAL_BYTES:
                        raise CaptureError(
                            f"selection exceeds the {MAX_TOTAL_BYTES}-byte total "
                            "limit; select fewer or smaller files"
                        )
                    validate_content(display, data, token_values)
                    captured.append(
                        _CapturedFile(
                            path=display,
                            data=data,
                            sha256=hashlib.sha256(data).hexdigest(),
                            media_type=MEDIA_TYPES[_extension_of(name)],
                        )
                    )
            finally:
                walker.close_collected()
        return captured

    @staticmethod
    def _input_results(task: Task) -> list[dict[str, Any]]:
        inputs = task.execution_inputs
        if inputs is None:
            return []
        return [
            {
                "producer_task_id": attachment.producer_task_id,
                "result_id": attachment.result_id,
                "manifest_id": attachment.manifest_id,
            }
            for attachment in inputs.result_attachments
        ]

    async def _provenance(
        self, task: Task, *, workflow: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Record capture-time observations without inferring authorship."""
        inputs = task.execution_inputs
        gaps: list[str] = []
        provenance: dict[str, Any] = {
            "capture_actor": "operator",
            "workflow_name": task.workflow_name,
            "workflow_revision": inputs.workflow_revision if inputs else None,
            "producing_run": "unknown",
            "producing_step": "unknown",
            "producing_session": "unknown",
        }
        if workflow is not None:
            provenance.update(workflow)
            provenance["capture_actor"] = "workflow"
        if inputs is None:
            gaps.append("launch_inputs")
            provenance["launch_base_branch"] = None
        else:
            provenance["launch_base_branch"] = inputs.workspace.base_branch
        if inputs is None or inputs.workflow_revision is None:
            gaps.append("workflow_revision")
        observation = await self._git_observation(
            task, provenance["launch_base_branch"]
        )
        provenance.update(observation)
        if observation.get("capture_head_commit") is None:
            gaps.append("capture_head_commit")
        if observation.get("capture_merge_base") is None:
            gaps.append("capture_merge_base")
        provenance["gaps"] = sorted(set(gaps))
        return provenance

    async def _git_observation(
        self, task: Task, base_branch: str | None
    ) -> dict[str, Any]:
        """Read-only Git plumbing, with the clone's own configuration disarmed.

        Both observations go through one path so they cannot disagree about
        what is disarmed. The clone is agent-writable, so every invocation runs
        with hooks pointed at nothing, system and global configuration ignored,
        and no terminal prompt — a capture must not become the moment
        repository-controlled configuration executes on the host.

        Failure here is a classified provenance gap, never a refusal to retain
        otherwise safe files: a workspace with no Git repository at all is a
        perfectly good place to have written a plan.
        """
        result: dict[str, Any] = {
            "capture_head_commit": None,
            "capture_merge_base": None,
            "git_observation": "unavailable",
        }
        head, error = await self._git_read(task, ["rev-parse", "HEAD"])
        if head is None:
            result["git_observation"] = f"unavailable: {error}"
            return result
        result["capture_head_commit"] = head
        result["git_observation"] = "capture-time observation"
        if not base_branch:
            return result
        merge_base, _error = await self._git_read(
            task, ["merge-base", "HEAD", base_branch]
        )
        result["capture_merge_base"] = merge_base
        return result

    async def _git_read(
        self, task: Task, args: list[str]
    ) -> tuple[str | None, str | None]:
        """One bounded, read-only Git command. Returns `(value, error)`.

        Never raises: an unreadable observation is a gap in the manifest, not a
        reason to discard files the operator can safely keep.
        """
        env = {
            # Nothing the clone or the host configures may execute during a
            # capture, and no credential helper may be consulted.
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
        }
        try:
            stdout, stderr, code = await run_git(
                safe_git(task.clone_path, *args),
                cwd=task.clone_path,
                timeout=self._config.spawn_step_timeout,
                step=f"result capture: git {args[0]}",
                env=env,
                check=False,
            )
        except Exception as exc:  # noqa: BLE001 — provenance gap, not a failure
            return None, str(exc)
        if code != 0 or not stdout.strip():
            return None, stderr.strip() or f"exit {code}"
        return stdout.strip(), None

    # -- inspection --

    def require_result(self, task_id: int, result_id: str) -> TaskResult:
        """Fetch a revision and prove it belongs to the task in the URL.

        A result addressed under the wrong task is a 404, not someone else's
        bundle: the task scope in the route is part of the authorization, not
        decoration.
        """
        result = get_result(self._engine, result_id)
        if result.task_id != task_id:
            raise ResultNotFoundError(result_id)
        return result

    def _verified_entry(self, result: TaskResult, path: str) -> ResultFile:
        try:
            entries = manifest_files(result.manifest)
        except DamagedManifestError as exc:
            reason = f"the retained manifest is unreadable: {exc}"
            mark_unavailable(self._engine, result.id, reason)
            self.publish(result.task_id)
            raise ResultUnavailableError(result.id, reason) from exc
        for entry in entries:
            if entry.path == path:
                return entry
        raise ResultNotFoundError(path)

    def read_file(self, result: TaskResult, path: str) -> tuple[bytes, ResultFile]:
        """One retained file's bytes, checked against the manifest first.

        Length and checksum are verified before a single byte is served, so a
        damaged store surfaces as a classified `Unavailable` — with its history
        intact — rather than as content the operator might reasonably believe
        is what they accepted.
        """
        self._require_available(result)
        entry = self._verified_entry(result, path)
        data = read_file_bytes(self._engine, result.id, path)
        self._verify(result, entry, data)
        assert data is not None
        return data, entry

    def read_bundle(self, result: TaskResult) -> dict[str, bytes]:
        """Every retained file, all verified. Used by acceptance and by ZIP
        download, because both are claims about the *whole* bundle."""
        self._require_available(result)
        try:
            entries = manifest_files(result.manifest)
        except DamagedManifestError as exc:
            reason = f"the retained manifest is unreadable: {exc}"
            mark_unavailable(self._engine, result.id, reason)
            self.publish(result.task_id)
            raise ResultUnavailableError(result.id, reason) from exc
        stored = read_all_files(self._engine, result.id)
        extra = sorted(set(stored) - {entry.path for entry in entries})
        if extra:
            self._fail_integrity(
                result, f"retained files not described by the manifest: {extra[0]}"
            )
        for entry in entries:
            self._verify(result, entry, stored.get(entry.path))
        return {entry.path: stored[entry.path] for entry in entries}

    def _require_available(self, result: TaskResult) -> None:
        if result.state == STATE_PURGED:
            raise ResultPurgedError(result.id)
        if result.state != STATE_READY:
            raise ResultStateError(
                result.id, f"is {result.state} and has no readable content"
            )
        if result.unavailable_reason is not None:
            raise ResultUnavailableError(result.id, result.unavailable_reason)

    def _verify(
        self, result: TaskResult, entry: ResultFile, data: bytes | None
    ) -> None:
        import hashlib

        if data is None:
            self._fail_integrity(result, f"{entry.path} is missing from the store")
        assert data is not None
        if len(data) != entry.length:
            self._fail_integrity(
                result, f"{entry.path} does not match its recorded length"
            )
        if hashlib.sha256(data).hexdigest() != entry.sha256:
            self._fail_integrity(
                result, f"{entry.path} does not match its recorded checksum"
            )

    def _fail_integrity(self, result: TaskResult, reason: str) -> None:
        mark_unavailable(self._engine, result.id, reason)
        self.publish(result.task_id)
        raise ResultUnavailableError(result.id, reason)

    # -- decisions --

    def accept(self, result: TaskResult, *, expected_manifest_id: str) -> dict[str, Any]:
        """Record acceptance of exactly this revision.

        The whole bundle is verified first: accepting a revision whose bytes
        are already damaged would record a decision about content nobody can
        read back. Acceptance grants nothing beyond retention — no workflow
        advances, no review is answered, and no publication becomes eligible.
        """
        self.read_bundle(result)
        accept_result(
            self._engine, result.id, expected_manifest_id=expected_manifest_id
        )
        return self.publish(result.task_id)

    def purge(
        self,
        result: TaskResult,
        *,
        expected_manifest_id: str,
        expected_version: int,
    ) -> dict[str, Any]:
        purge_result(
            self._engine,
            result.id,
            expected_manifest_id=expected_manifest_id,
            expected_version=expected_version,
        )
        return self.publish(result.task_id)

    # -- comparison --

    def diff(self, result: TaskResult) -> dict[str, Any]:
        """Compare this revision with its recorded predecessor.

        Omission is reported as a *bundle* difference. It says a path is not in
        this revision, which is never an instruction to delete anything from a
        workspace or a checkout.
        """
        import difflib

        if result.predecessor_id is None:
            return {
                "result_id": result.id,
                "predecessor_id": None,
                "predecessor_available": False,
                "predecessor_reason": "this is the first captured revision",
                "added": [],
                "changed": [],
                "omitted": [],
                "unchanged": [],
                "text": "",
                "truncated": False,
            }
        try:
            predecessor = get_result(self._engine, result.predecessor_id)
        except ResultNotFoundError:
            predecessor = None
        current = self.read_bundle(result)
        if predecessor is None or not predecessor.available:
            reason = (
                "the previous revision was purged"
                if predecessor is not None and predecessor.state == STATE_PURGED
                else "the previous revision is unavailable"
            )
            return {
                "result_id": result.id,
                "predecessor_id": result.predecessor_id,
                "predecessor_available": False,
                # Explicitly *not* treated as an empty bundle: reporting every
                # file as "added" against a revision nobody can read would be a
                # comparison Ompire cannot stand behind.
                "predecessor_reason": reason,
                "added": [],
                "changed": [],
                "omitted": [],
                "unchanged": sorted(current),
                "text": "",
                "truncated": False,
            }
        previous = self.read_bundle(predecessor)
        added = sorted(set(current) - set(previous))
        omitted = sorted(set(previous) - set(current))
        shared = sorted(set(current) & set(previous))
        changed = [path for path in shared if current[path] != previous[path]]
        unchanged = [path for path in shared if current[path] == previous[path]]
        lines: list[str] = []
        truncated = False
        size = 0
        for path in [*changed, *added]:
            before = (
                previous[path].decode("utf-8", errors="replace").splitlines(keepends=True)
                if path in previous
                else []
            )
            after = current[path].decode("utf-8", errors="replace").splitlines(
                keepends=True
            )
            for line in difflib.unified_diff(
                before,
                after,
                fromfile=f"a/{path}" if path in previous else "/dev/null",
                tofile=f"b/{path}",
            ):
                size += len(line.encode("utf-8"))
                if size > MAX_DIFF_BYTES:
                    truncated = True
                    break
                lines.append(line if line.endswith("\n") else line + "\n")
            if truncated:
                break
        return {
            "result_id": result.id,
            "predecessor_id": predecessor.id,
            "predecessor_available": True,
            "predecessor_reason": None,
            "added": added,
            "changed": changed,
            "omitted": omitted,
            "unchanged": unchanged,
            "text": "".join(lines),
            "truncated": truncated,
        }

    # -- download --

    def zip_bundle(self, result: TaskResult) -> bytes:
        """One ZIP of the retained bytes plus Ompire's manifest.

        Members are exactly the manifest's validated relative paths and the
        reserved manifest name — nothing derived from a directory listing — and
        every member is written non-executable. An unavailable file fails the
        whole archive rather than producing a partial one an operator might
        mistake for the complete result.
        """
        contents = self.read_bundle(result)
        buffer = io.BytesIO()
        manifest_bytes = json.dumps(
            result.manifest, indent=2, sort_keys=True, ensure_ascii=False
        ).encode("utf-8")
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(contents):
                info = zipfile.ZipInfo(path)
                # 0o644, regular file. Nothing downloaded from a result is
                # executable, whatever mode it had in the workspace.
                info.external_attr = (0o100644 & 0xFFFF) << 16
                archive.writestr(info, contents[path])
            manifest_info = zipfile.ZipInfo(RESERVED_MANIFEST_NAME)
            manifest_info.external_attr = (0o100644 & 0xFFFF) << 16
            archive.writestr(manifest_info, manifest_bytes)
        return buffer.getvalue()
