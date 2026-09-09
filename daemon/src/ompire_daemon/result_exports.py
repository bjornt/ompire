"""Trusted export of a retained result revision into the operator's checkout.

This is the one place in Ompire that writes into a directory the operator owns
(ADR-0036). Ordinary task execution never does; capture reads a workspace and
handoff writes a disposable clone. So the boundary here is drawn differently
from both.

**Create-only.** A destination that already exists is never replaced, chmodded,
truncated, or merged — not after approval, and not when its bytes differ. An
existing file with exactly the approved content is a no-op. Anything else is a
conflict the operator resolves by deselecting the file, choosing another
prefix, or fixing the checkout by hand. There is no force.

**Preview is read-only and binds the approval.** It observes the retained
revision, the registered root, every relevant ancestor, and each destination,
then hashes a canonical document of exactly those observations. The daemon
recomputes that document at confirmation from a fresh observation; a client
cannot supply a verdict, and a changed anything is a refusal rather than a
silently retargeted write.

**Installation is atomic per file and honest about the bundle.** Bytes are
staged in a daemon-named directory under the approved root, verified, fsynced,
and then moved into place with `renameat2(RENAME_NOREPLACE)` through directory
descriptors. A kernel or filesystem without that primitive refuses before any
destination is touched; there is no check-then-rename fallback, because that
races exactly the concurrent writer this whole module exists to protect. Files
are individually atomic. A bundle is not, and nothing here pretends otherwise:
a failure stops the remaining work and leaves what was installed in place,
recorded, rather than deleting an operator's files to fake a rollback.

**Recovery classifies; it never repeats.** Startup and an explicit recheck
re-observe the filesystem read-only. A staged inode found at its destination
proves the rename happened; matching bytes alone prove nothing about who wrote
them and are reported as `unknown`. No destination is written, no effect is
rolled back, and an acknowledgement records that an operator read the
uncertainty without changing it.

Durable state lives in `registry/result_exports.py`. Nothing here holds a
SQLite write reservation across a filesystem call or an `await`.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import difflib
import errno
import hashlib
import logging
import os
import platform
import posixpath
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Engine

from ompire_daemon.config import Config
from ompire_daemon.events import EventHub
from ompire_daemon.handoff import RESERVED_ROOT_FILES
from ompire_daemon.registry.projects import ProjectNotFoundError, get_project
from ompire_daemon.registry.result_exports import (
    CLASS_CONFLICT,
    CLASS_CREATE,
    CLASS_IDENTICAL,
    OUTCOME_CREATED,
    OUTCOME_IDENTICAL,
    OUTCOME_NOT_INSTALLED,
    OUTCOME_PENDING,
    OUTCOME_UNKNOWN,
    PREVIEW_FORMAT,
    STATE_COMPLETED,
    STATE_INCOMPLETE,
    STATE_UNRESOLVED,
    ApprovedDestination,
    ExportRecord,
    ExportRequestMismatchError,
    ExportStateError,
    acknowledge_export,
    admit_export,
    export_payload,
    find_replay,
    finish_export,
    list_task_exports,
    list_unsettled_exports,
    mark_started,
    preview_token,
    record_created_directories,
    record_file_outcome,
    record_reconciliation,
    record_staged_file,
    record_staging,
    require_export,
    staging_name_for,
)
from ompire_daemon.registry.results import (
    MAX_DIFF_BYTES,
    MAX_FILE_BYTES,
    InvalidSelectionError,
    ResultFile,
    TaskResult,
    selection_fingerprint,
    validate_relative_path,
)
from ompire_daemon.registry.tasks import get_task
from ompire_daemon.results import (
    ResultManager,
    credential_token_values,
    validate_content,
)

logger = logging.getLogger(__name__)


class ExportError(Exception):
    """One export request cannot proceed, and why.

    `reason` is the stable machine code the form keys its explanation off;
    `detail` is the sentence the operator reads; `path` names the destination
    at fault whenever exactly one is. "The export is refused" is not a
    correction anyone can act on.
    """

    def __init__(self, reason: str, detail: str, *, path: str | None = None) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.path = path


class ExportUnsupportedError(ExportError):
    """The host cannot perform a non-replacing atomic install.

    A separate type because the answer is different from every other refusal:
    nothing about the request is wrong, and no selection change would help.
    """


# --- The safe-install primitive ---------------------------------------------
#
# `os.replace` and `os.rename` both silently clobber an existing destination,
# and "stat, then rename" is precisely the race a create-only guarantee has to
# survive. `renameat2(RENAME_NOREPLACE)` makes the check and the move one
# operation the kernel performs, which is the only version of this that is
# actually true under a concurrent writer.

RENAME_NOREPLACE = 1

# `renameat2` has no portable syscall number. Only the architectures this
# daemon is shipped and tested on are listed; anywhere else the libc symbol has
# to exist, or export refuses rather than guessing a number.
_RENAMEAT2_SYSCALLS = {"x86_64": 316, "aarch64": 276}


def _load_renameat2():
    """Bind the narrowest possible wrapper, or return None.

    Prefers glibc's exported symbol and falls back to the raw syscall on the
    architectures whose number is known. Nothing here emulates the flag.
    """
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError:  # pragma: no cover - no libc is not a supported host
        return None
    fn = getattr(libc, "renameat2", None)
    if fn is not None:
        fn.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        fn.restype = ctypes.c_int
        return fn
    number = _RENAMEAT2_SYSCALLS.get(platform.machine())
    if number is None:
        return None
    syscall = libc.syscall
    syscall.restype = ctypes.c_long

    def _call(old_fd: int, old: bytes, new_fd: int, new: bytes, flags: int) -> int:
        return int(
            syscall(
                ctypes.c_long(number),
                ctypes.c_int(old_fd),
                ctypes.c_char_p(old),
                ctypes.c_int(new_fd),
                ctypes.c_char_p(new),
                ctypes.c_uint(flags),
            )
        )

    return _call


_RENAMEAT2 = _load_renameat2()


def rename_noreplace(old_fd: int, old_name: str, new_fd: int, new_name: str) -> None:
    """Move a staged file into place without ever replacing an existing entry.

    Raises `FileExistsError` when the destination was taken between approval
    and now — which is a real answer, not a failure to handle: the operator's
    file wins, and this export reports that it did not install.
    """
    if _RENAMEAT2 is None:
        raise ExportUnsupportedError(
            "unsupported-platform",
            "this host has no renameat2(RENAME_NOREPLACE), so a file cannot be "
            "installed without risking replacement of an existing one; export "
            "is refused rather than falling back to a racy rename",
        )
    ctypes.set_errno(0)
    rc = _RENAMEAT2(
        old_fd,
        old_name.encode("utf-8"),
        new_fd,
        new_name.encode("utf-8"),
        RENAME_NOREPLACE,
    )
    if rc == 0:
        return
    code = ctypes.get_errno()
    if code == errno.EEXIST:
        raise FileExistsError(code, os.strerror(code), new_name)
    if code in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
        raise ExportUnsupportedError(
            "unsupported-platform",
            "this kernel or filesystem does not support "
            f"renameat2(RENAME_NOREPLACE) ({os.strerror(code)}); export is "
            "refused rather than falling back to a racy rename",
        )
    raise OSError(code, os.strerror(code), new_name)


# --- Selection and destinations ---------------------------------------------


def normalize_prefix(prefix: str) -> str:
    """Validate the optional checkout-relative destination prefix.

    Empty means "keep the manifest's own repository-relative paths". Anything
    else is checked with the same purely syntactic rules a captured path faces,
    so traversal, absolute paths, and dot-prefixed names are refused here
    rather than being caught later by a descriptor walk that should never have
    been started.
    """
    text = prefix.strip()
    if text.startswith("/"):
        raise ExportError(
            "invalid-prefix",
            "the destination prefix must be relative to the checkout root; "
            "absolute paths are not accepted",
        )
    text = text.rstrip("/")
    if not text:
        return ""
    try:
        return validate_relative_path(text)
    except InvalidSelectionError as exc:
        raise ExportError("invalid-prefix", f"the destination prefix {exc}") from exc


def plan_selection(
    result: TaskResult, paths: Sequence[str], prefix: str
) -> tuple[tuple[ResultFile, ...], tuple[ResultFile, ...], dict[str, str]]:
    """Resolve a manifest subset and its destinations.

    Returns `(selected, omitted, destinations)`. The selection grammar is
    deliberately thin: literal manifest paths only, no directories and no
    globs. A second capture-like path language here would be a second thing to
    get wrong, and the manifest already says exactly which files exist.
    """
    entries = {entry.path: entry for entry in result.files}
    if not paths:
        raise ExportError(
            "empty-selection",
            "select at least one file from this revision to export",
        )
    seen: set[str] = set()
    for path in paths:
        if path in seen:
            raise ExportError(
                "duplicate-selection",
                f"{path!r} is selected more than once",
                path=path,
            )
        seen.add(path)
        if path not in entries:
            raise ExportError(
                "unknown-file",
                f"{path!r} is not a file in this revision; the selection must "
                "name manifest paths exactly",
                path=path,
            )
    selected = tuple(sorted((entries[path] for path in seen), key=lambda e: e.path))
    omitted = tuple(
        entry for entry in result.files if entry.path not in seen
    )
    destinations: dict[str, str] = {}
    for entry in selected:
        combined = f"{prefix}/{entry.path}" if prefix else entry.path
        try:
            destination = validate_relative_path(combined)
        except InvalidSelectionError as exc:
            raise ExportError(
                "invalid-destination",
                f"{entry.path!r} would be exported to an unusable path: {exc}",
                path=entry.path,
            ) from exc
        if destination in RESERVED_ROOT_FILES:
            raise ExportError(
                "reserved-destination",
                f"{destination!r} configures the workshop launcher and is "
                "never written by an export",
                path=entry.path,
            )
        destinations[entry.path] = destination
    # Two manifest paths cannot collide under a single prefix, but a file and
    # another file's parent directory can. Only one of the two can exist.
    directories = {
        ancestor
        for destination in destinations.values()
        for ancestor in _ancestors(destination)
    }
    for path, destination in sorted(destinations.items()):
        if destination in directories:
            raise ExportError(
                "destination-collision",
                f"{destination!r} would have to be both a file and a "
                "directory in this selection; export them separately",
                path=path,
            )
    return selected, omitted, destinations


def _ancestors(path: str) -> tuple[str, ...]:
    parts = path.split("/")[:-1]
    return tuple("/".join(parts[: index + 1]) for index in range(len(parts)))


# --- The read-only observation ----------------------------------------------


@dataclass(frozen=True)
class RootHandle:
    """An opened, identity-checked checkout root. Closed by the caller."""

    fd: int
    device: int
    inode: int
    path: str
    project_name: str


@dataclass(frozen=True)
class Observation:
    """What the filesystem actually showed, for one preview or one recheck."""

    ancestors: dict[str, dict[str, Any]]
    destinations: dict[str, dict[str, Any]]
    # Destination path -> the retained-comparison text, kept out of the
    # canonical document and out of every fleet event.
    existing_text: dict[str, str]


def _stat_identity(info: os.stat_result) -> dict[str, Any]:
    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "mode": stat.S_IMODE(info.st_mode),
        "length": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
    }


def _open_child_dir(parent_fd: int, name: str, root_device: int) -> int:
    """One existing directory component, no-follow and on the root's device."""
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=parent_fd)
    try:
        info = os.fstat(fd)
        if info.st_dev != root_device:
            raise ExportError(
                "cross-device",
                f"{name!r} is on a different filesystem than the checkout root",
            )
        # A nested repository is a different working tree. Writing into one
        # would deliver files to a repository the operator did not name.
        try:
            os.stat(".git", dir_fd=fd, follow_symlinks=False)
        except OSError:
            pass
        else:
            raise ExportError(
                "nested-repository",
                f"{name!r} is a nested repository boundary inside the "
                "checkout; export refuses to write across it",
            )
    except Exception:
        os.close(fd)
        raise
    return fd


def observe(
    root: RootHandle,
    destinations: Mapping[str, str],
    expected: Mapping[str, ResultFile],
    token_values: tuple[str, ...] = (),
) -> Observation:
    """Inspect the checkout without changing a single byte of it.

    Every component is opened relative to the previous directory's descriptor
    with `O_NOFOLLOW`, so no window exists in which a resolved string could be
    made to point elsewhere. Nothing is created — not even a staging directory
    — and no Git command runs: conflict classification is about working-tree
    bytes, and a commit tree would answer a different question.
    """
    ancestors: dict[str, dict[str, Any]] = {}
    observed: dict[str, dict[str, Any]] = {}
    texts: dict[str, str] = {}
    for manifest_path in sorted(destinations):
        destination = destinations[manifest_path]
        entry = expected[manifest_path]
        parent_fd = os.dup(root.fd)
        missing_ancestor = False
        try:
            walked: list[str] = []
            for component in destination.split("/")[:-1]:
                walked.append(component)
                here = "/".join(walked)
                if ancestors.get(here, {}).get("present") is False:
                    missing_ancestor = True
                    break
                try:
                    nxt = _open_child_dir(parent_fd, component, root.device)
                except FileNotFoundError:
                    ancestors[here] = {"path": here, "present": False}
                    missing_ancestor = True
                    break
                except ExportError as exc:
                    observed[manifest_path] = _conflict(
                        destination, exc.reason, exc.detail
                    )
                    missing_ancestor = True
                    break
                except OSError as exc:
                    ancestors[here] = {"path": here, "present": True, "usable": False}
                    observed[manifest_path] = _conflict(
                        destination,
                        "ancestor-not-a-directory",
                        f"{here!r} is not an ordinary directory in the "
                        f"checkout ({exc.strerror})",
                    )
                    missing_ancestor = True
                    break
                os.close(parent_fd)
                parent_fd = nxt
                info = os.fstat(parent_fd)
                ancestors[here] = {
                    "path": here,
                    "present": True,
                    "usable": True,
                    "device": info.st_dev,
                    "inode": info.st_ino,
                }
            if missing_ancestor:
                observed.setdefault(
                    manifest_path,
                    {
                        "path": manifest_path,
                        "destination": destination,
                        "classification": CLASS_CREATE,
                        "reason": None,
                        "before": None,
                    },
                )
                continue
            classification, text = _observe_destination(
                parent_fd, destination, entry, root.device, token_values
            )
            observed[manifest_path] = classification
            if text is not None:
                texts[destination] = text
        finally:
            os.close(parent_fd)
    return Observation(ancestors=ancestors, destinations=observed, existing_text=texts)


def _conflict(destination: str, reason: str, detail: str) -> dict[str, Any]:
    return {
        "destination": destination,
        "classification": CLASS_CONFLICT,
        "reason": reason,
        "detail": detail,
        "before": None,
    }


def _observe_destination(
    parent_fd: int,
    destination: str,
    entry: ResultFile,
    root_device: int,
    token_values: tuple[str, ...],
) -> tuple[dict[str, Any], str | None]:
    """Classify one destination and, when it is safe to show, read its text."""
    name = posixpath.basename(destination)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except FileNotFoundError:
        return (
            {
                "destination": destination,
                "classification": CLASS_CREATE,
                "reason": None,
                "before": None,
            },
            None,
        )
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return (
                _conflict(
                    destination,
                    "symlink-destination",
                    "a symbolic link occupies this destination; export never "
                    "follows one or replaces it",
                ),
                None,
            )
        return (
            _conflict(
                destination,
                "unreadable-destination",
                f"the existing entry cannot be inspected ({exc.strerror})",
            ),
            None,
        )
    try:
        info = os.fstat(fd)
        if stat.S_ISDIR(info.st_mode):
            return (
                _conflict(
                    destination,
                    "directory-destination",
                    "a directory occupies this destination; export never "
                    "removes or writes through one",
                ),
                None,
            )
        if not stat.S_ISREG(info.st_mode):
            return (
                _conflict(
                    destination,
                    "special-destination",
                    "the existing entry is not an ordinary file",
                ),
                None,
            )
        if info.st_nlink != 1:
            return (
                _conflict(
                    destination,
                    "multiply-linked-destination",
                    "the existing file has other hard links; writing here "
                    "would affect a path nobody reviewed",
                ),
                None,
            )
        if info.st_dev != root_device:
            return (
                _conflict(
                    destination,
                    "cross-device",
                    "the existing file is on a different filesystem than the "
                    "checkout root",
                ),
                None,
            )
        identity = _stat_identity(info)
        if info.st_size > MAX_FILE_BYTES:
            return (
                _conflict(
                    destination,
                    "destination-too-large",
                    f"the existing file is {info.st_size} bytes, past the "
                    f"{MAX_FILE_BYTES}-byte bound this comparison reads",
                )
                | {"before": identity},
                None,
            )
        data = _read_all(fd, MAX_FILE_BYTES)
    finally:
        os.close(fd)
    identity["sha256"] = hashlib.sha256(data).hexdigest()
    if len(data) == entry.length and identity["sha256"] == entry.sha256:
        return (
            {
                "destination": destination,
                "classification": CLASS_IDENTICAL,
                "reason": None,
                "before": identity,
            },
            None,
        )
    conflict = _conflict(
        destination,
        "different-content",
        "a different file already exists at this destination; export never "
        "replaces one",
    ) | {"before": identity}
    try:
        text = validate_content(destination, data, token_values)
    except InvalidSelectionError as exc:
        # The destination is still a conflict, but its content is not shown.
        # A preview that displayed a checkout file's credentials back to the
        # browser would be a leak this feature invented.
        conflict["detail"] = (
            "a different file already exists at this destination, and its "
            f"content is not shown: {exc.reason}"
        )
        return conflict, None
    return conflict, text


def _read_all(fd: int, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total <= limit:
        chunk = os.read(fd, 256 * 1024)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


# --- The canonical approval document ----------------------------------------


def build_preview_document(
    *,
    result: TaskResult,
    task_id: int,
    root: RootHandle,
    prefix: str,
    selected: Sequence[ResultFile],
    omitted: Sequence[ResultFile],
    destinations: Mapping[str, str],
    observation: Observation,
) -> dict[str, Any]:
    """Everything the approval binds, and nothing a client could forge.

    Metadata and hashes only. Source text and diffs travel beside this document
    in the response; they are never persisted with the approval and never
    broadcast, because they can contain the contents of the operator's own
    checkout.
    """
    files = []
    for entry in sorted(selected, key=lambda item: item.path):
        record = observation.destinations[entry.path]
        files.append(
            {
                "path": entry.path,
                "destination": record["destination"],
                "length": entry.length,
                "sha256": entry.sha256,
                "media_type": entry.media_type,
                "classification": record["classification"],
                "reason": record.get("reason"),
                "detail": record.get("detail"),
                "before": record.get("before"),
            }
        )
    return {
        "format": PREVIEW_FORMAT,
        "task_id": task_id,
        "result_id": result.id,
        "manifest_id": result.manifest_id,
        "project_name": root.project_name,
        "checkout_path": root.path,
        "root": {"device": root.device, "inode": root.inode},
        "prefix": prefix,
        "selection": [entry.path for entry in sorted(selected, key=lambda i: i.path)],
        "omitted": [entry.path for entry in sorted(omitted, key=lambda i: i.path)],
        "ancestors": [
            observation.ancestors[key] for key in sorted(observation.ancestors)
        ],
        "files": files,
    }


def document_is_blocked(document: Mapping[str, Any]) -> bool:
    return any(
        entry["classification"] == CLASS_CONFLICT for entry in document["files"]
    )


# --- The manager ------------------------------------------------------------


class ResultExportManager:
    """Preview, admit, install, and reconcile checkout exports."""

    def __init__(
        self,
        config: Config,
        engine: Engine,
        events: EventHub,
        results: ResultManager,
    ) -> None:
        self._config = config
        self._engine = engine
        self._hub = events
        self._results = results
        self._jobs: set[asyncio.Task[Any]] = set()

    # -- startup / shutdown --

    def restore(self) -> list[ExportRecord]:
        """Settle every export a stopped daemon left running.

        Read-only re-observation, before any new export is admitted and before
        the first snapshot: a client must never see a `running` export owned by
        a process that no longer exists, and recovery must never write a
        destination the operator did not just approve.
        """
        settled: list[ExportRecord] = []
        for record in list_unsettled_exports(self._engine):
            try:
                settled.append(self.reconcile(record))
            except Exception:  # pragma: no cover - defensive
                logger.exception(
                    "export %s could not be reconciled at startup", record.id
                )
        return settled

    async def shutdown(self) -> None:
        jobs = list(self._jobs)
        for job in jobs:
            job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        # A cancelled installation never reached its own settlement. Classify
        # it here rather than leaving a running-looking row for the next start:
        # the rule is identical either way — observe, never rewrite.
        self.restore()

    # -- projection --

    def publish(self, task_id: int) -> dict[str, Any]:
        return self._results.publish(task_id)

    def export_payload(self, record: ExportRecord) -> dict[str, Any]:
        """One export's wire shape. Defined in the registry so the result
        projection can build it without importing this trusted module."""
        return export_payload(record)

    def list_exports(self, task_id: int) -> list[ExportRecord]:
        return list_task_exports(self._engine, task_id)

    def require_export(self, task_id: int, export_id: str) -> ExportRecord:
        return require_export(self._engine, task_id, export_id)

    # -- root resolution --

    def _open_root(self, task_id: int) -> RootHandle:
        """The one destination root an export may use.

        Resolved from the *task's* project registration, never from a client
        request: there is no field anywhere in this feature that names a host
        directory.
        """
        task = get_task(self._engine, task_id)
        try:
            project = get_project(self._engine, task.project_name)
        except ProjectNotFoundError as exc:
            raise ExportError(
                "project-missing",
                f"project {task.project_name!r} is no longer registered, so "
                "there is no checkout to export into",
            ) from exc
        if project.setup_state != "ready":
            raise ExportError(
                "checkout-not-ready",
                f"project {project.name!r} is {project.setup_state}; its "
                "checkout is not ready to receive an export",
            )
        if not project.checkout_path:
            raise ExportError(
                "checkout-missing",
                f"project {project.name!r} has no registered checkout path",
            )
        try:
            fd = os.open(
                project.checkout_path,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY,
            )
        except OSError as exc:
            raise ExportError(
                "checkout-unavailable",
                f"the registered checkout {project.checkout_path} cannot be "
                f"opened as an ordinary directory ({exc.strerror})",
            ) from exc
        try:
            info = os.fstat(fd)
            self._assert_root_allowed(Path(project.checkout_path))
        except Exception:
            os.close(fd)
            raise
        return RootHandle(
            fd=fd,
            device=info.st_dev,
            inode=info.st_ino,
            path=project.checkout_path,
            project_name=project.name,
        )

    def _assert_root_allowed(self, root: Path) -> None:
        """Refuse a root that is really Ompire's own storage.

        A project pointed at a task workspace or at the daemon's data directory
        would turn export into a way to write inside the isolation boundary
        (ADR-0006) or into the store itself.
        """
        resolved = root.expanduser().resolve()
        for label, forbidden in (
            ("task workspace root", self._config.task_dir_root),
            ("daemon data directory", self._config.data_dir),
        ):
            base = forbidden.expanduser().resolve()
            if resolved == base or base in resolved.parents:
                raise ExportError(
                    "forbidden-root",
                    f"the registered checkout {root} is inside the {label} "
                    f"{base}; export never writes there",
                )

    # -- preview --

    def preview(
        self,
        *,
        task_id: int,
        result_id: str,
        expected_manifest_id: str,
        paths: Sequence[str],
        prefix: str,
    ) -> dict[str, Any]:
        """Read the retained revision and the real checkout, and classify.

        Writes nothing, creates nothing, and runs no Git command. The returned
        document is the exact thing a confirmation is checked against.
        """
        result = self._results.require_result(task_id, result_id)
        self._require_exportable(result, expected_manifest_id)
        normalized_prefix = normalize_prefix(prefix)
        selected, omitted, destinations = plan_selection(
            result, paths, normalized_prefix
        )
        contents = self._results.read_bundle(result)
        expected = {entry.path: entry for entry in selected}
        root = self._open_root(task_id)
        try:
            observation = observe(
                root, destinations, expected, credential_token_values(self._config)
            )
            document = build_preview_document(
                result=result,
                task_id=task_id,
                root=root,
                prefix=normalized_prefix,
                selected=selected,
                omitted=omitted,
                destinations=destinations,
                observation=observation,
            )
        finally:
            os.close(root.fd)
        return {
            "preview": document,
            "preview_token": preview_token(document),
            "blocked": document_is_blocked(document),
            "source": {
                entry.path: contents[entry.path].decode("utf-8", errors="replace")
                for entry in selected
            },
            **_diff_payload(document, observation, contents),
        }

    def _require_exportable(self, result: TaskResult, expected_manifest_id: str) -> None:
        if result.manifest_id != expected_manifest_id:
            raise ExportError(
                "stale-revision",
                "this revision has changed since the form was opened; review "
                "the current revision before exporting",
            )
        if not result.accepted:
            raise ExportError(
                "not-accepted",
                "only an accepted revision can be exported; review and accept "
                "it first",
            )
        # `read_bundle` raises the precise unavailable/purged/state refusal.
        self._results._require_available(result)

    # -- confirmation --

    async def start(
        self,
        *,
        task_id: int,
        result_id: str,
        expected_manifest_id: str,
        paths: Sequence[str],
        prefix: str,
        token: str,
        request_id: str,
    ) -> tuple[ExportRecord, bool]:
        """Recompute the preview, admit the operation, and supervise it.

        The recomputation is the whole point: approval names a document the
        daemon derives, so a client that stored a token cannot use it against a
        checkout that has since changed, and a client that never previewed
        cannot invent one.

        A repeated `request_id` is answered from the journal *first*. A
        completed export has changed the destinations a fresh preview would
        classify, so re-deriving one would answer "did my request go through?"
        with "the checkout changed" — true, and useless.
        """
        replay = find_replay(self._engine, task_id, request_id)
        if replay is not None:
            if (
                replay.result_id != result_id
                or replay.manifest_id != expected_manifest_id
                or replay.preview_token != token
            ):
                raise ExportRequestMismatchError(request_id, replay.id)
            return replay, False
        result = self._results.require_result(task_id, result_id)
        self._require_exportable(result, expected_manifest_id)
        normalized_prefix = normalize_prefix(prefix)
        selected, omitted, destinations = plan_selection(
            result, paths, normalized_prefix
        )
        expected = {entry.path: entry for entry in selected}
        root = self._open_root(task_id)
        try:
            observation = observe(
                root, destinations, expected, credential_token_values(self._config)
            )
            document = build_preview_document(
                result=result,
                task_id=task_id,
                root=root,
                prefix=normalized_prefix,
                selected=selected,
                omitted=omitted,
                destinations=destinations,
                observation=observation,
            )
            recomputed = preview_token(document)
            if recomputed != token:
                raise ExportError(
                    "stale-preview",
                    "the revision, the selection, or the checkout changed "
                    "since this export was previewed; preview again and "
                    "review what would happen now",
                )
            if document_is_blocked(document):
                blocked = [
                    entry["destination"]
                    for entry in document["files"]
                    if entry["classification"] == CLASS_CONFLICT
                ]
                raise ExportError(
                    "conflicts-selected",
                    "this selection still conflicts with the checkout at "
                    + ", ".join(blocked)
                    + "; deselect those files, choose another prefix, or "
                    "resolve them in the checkout first",
                    path=blocked[0],
                )
            record, created = admit_export(
                self._engine,
                task_id=task_id,
                result_id=result_id,
                expected_manifest_id=expected_manifest_id,
                request_id=request_id,
                selection=[entry.path for entry in selected],
                selection_fingerprint=selection_fingerprint(
                    [entry.path for entry in selected]
                ),
                prefix=normalized_prefix,
                document=document,
                token=recomputed,
                project_name=root.project_name,
                checkout_path=root.path,
                root_device=root.device,
                root_inode=root.inode,
                destinations=[
                    ApprovedDestination(
                        manifest_path=entry["path"],
                        destination=entry["destination"],
                        classification=entry["classification"],
                        expected_length=entry["length"],
                        expected_sha256=entry["sha256"],
                        before=entry["before"],
                    )
                    for entry in document["files"]
                ],
            )
        finally:
            os.close(root.fd)
        if not created:
            return record, False
        self.publish(task_id)
        job = asyncio.create_task(self._run_export(record.id))
        self._jobs.add(job)
        job.add_done_callback(self._jobs.discard)
        return record, True

    async def _run_export(self, export_id: str) -> None:
        """Own the installation from staging to settlement, exactly once."""
        record = None
        try:
            record = mark_started(self._engine, export_id)
            contents = self._read_approved_bytes(record)
            await asyncio.to_thread(self._install, record, contents)
        except asyncio.CancelledError:
            # The shutdown path reconciles this row read-only. Nothing is
            # retried and nothing is undone.
            raise
        except ExportError as exc:
            finish_export(
                self._engine, export_id, state=STATE_INCOMPLETE, error=exc.detail
            )
        except Exception as exc:  # pragma: no cover - unexpected host failure
            logger.exception("export %s failed", export_id)
            finish_export(
                self._engine,
                export_id,
                state=STATE_UNRESOLVED,
                error=f"the export stopped unexpectedly: {exc}",
            )
        if record is not None:
            self.publish(record.task_id)

    def _read_approved_bytes(self, record: ExportRecord) -> dict[str, bytes]:
        """The retained bytes for exactly the approved selection.

        Read through the ordinary verified path, so a store that was damaged
        between approval and execution refuses instead of installing content
        nobody accepted.
        """
        result = self._results.require_result(record.task_id, record.result_id)
        bundle = self._results.read_bundle(result)
        if result.manifest_id != record.manifest_id:
            raise ExportError(
                "stale-revision",
                "the revision changed between approval and execution; nothing "
                "was written",
            )
        return {entry.manifest_path: bundle[entry.manifest_path] for entry in record.files}

    # -- installation --

    def _install(self, record: ExportRecord, contents: Mapping[str, bytes]) -> None:
        """Stage, verify, and install, on a worker thread.

        Ordered so that every effect is preceded by a durable statement of
        intent: staging identity before installation, per-file correlation
        before its rename, and the created-directory list before settlement.
        """
        root = self._reopen_root(record)
        staging_fd: int | None = None
        staging_error: str | None = None
        created_dirs: list[str] = []
        outcomes: dict[str, str] = {}
        try:
            staging_fd = self._create_staging(root, record)
            self._probe_safe_install(staging_fd)
            staged = self._stage(record, staging_fd, contents)
            for entry in record.files:
                if entry.classification == CLASS_IDENTICAL:
                    outcome = self._verify_identical(root, record, entry)
                else:
                    outcome = self._install_one(
                        root, record, entry, staging_fd, staged[entry.manifest_path],
                        created_dirs,
                    )
                outcomes[entry.manifest_path] = outcome
                if outcome not in (OUTCOME_CREATED, OUTCOME_IDENTICAL):
                    # Stop the remaining work: the checkout is not what the
                    # operator approved, and pressing on would install files
                    # into a target that has already surprised us once.
                    break
        finally:
            if created_dirs:
                record_created_directories(self._engine, record.id, created_dirs)
            if staging_fd is not None:
                staging_error = self._clean_staging(root, record, staging_fd)
            os.close(root.fd)
        complete = len(outcomes) == len(record.files) and all(
            value in (OUTCOME_CREATED, OUTCOME_IDENTICAL) for value in outcomes.values()
        )
        finish_export(
            self._engine,
            record.id,
            state=STATE_COMPLETED if complete else STATE_INCOMPLETE,
            error=None
            if complete
            else "the export stopped before installing every approved file; "
            "the per-file outcomes below say what happened",
            staging_error=staging_error,
        )

    def _reopen_root(self, record: ExportRecord) -> RootHandle:
        """Reopen and re-identify the approved root before touching anything.

        The database reservation says no other export owns this root. It says
        nothing about the directory itself, which may have been replaced on
        disk since — so the identity is checked against the approved document.
        """
        root = self._open_root(record.task_id)
        if (root.device, root.inode) != (record.root_device, record.root_inode):
            os.close(root.fd)
            raise ExportError(
                "root-replaced",
                f"the checkout at {record.checkout_path} is not the directory "
                "this export was approved against; nothing was written",
            )
        return root

    def _create_staging(self, root: RootHandle, record: ExportRecord) -> int:
        name = staging_name_for(record.id)
        try:
            os.mkdir(name, mode=0o700, dir_fd=root.fd)
        except FileExistsError as exc:
            raise ExportError(
                "staging-occupied",
                f"{name!r} already exists in the checkout; export refuses to "
                "reuse a staging directory it did not just create",
            ) from exc
        except OSError as exc:
            raise ExportError(
                "staging-failed",
                f"cannot create the staging directory in the checkout "
                f"({exc.strerror})",
            ) from exc
        fd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=root.fd
        )
        info = os.fstat(fd)
        record_staging(self._engine, record.id, device=info.st_dev, inode=info.st_ino)
        return fd

    def _probe_safe_install(self, staging_fd: int) -> None:
        """Prove the safe-install primitive works here, inside staging.

        Done before any destination exists in the plan's future, so a host
        without `renameat2(RENAME_NOREPLACE)` refuses having touched nothing
        outside a directory this export just made.
        """
        probe = os.open(
            "probe", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=staging_fd
        )
        os.close(probe)
        try:
            rename_noreplace(staging_fd, "probe", staging_fd, "probe-moved")
        finally:
            for name in ("probe", "probe-moved"):
                with contextlib.suppress(OSError):
                    os.unlink(name, dir_fd=staging_fd)

    def _stage(
        self, record: ExportRecord, staging_fd: int, contents: Mapping[str, bytes]
    ) -> dict[str, str]:
        """Write and verify every file this export will install.

        All of them, before any is installed: an export that cannot even stage
        its content should not have started delivering part of it.
        """
        staged: dict[str, str] = {}
        for entry in record.files:
            if entry.classification != CLASS_CREATE:
                continue
            name = f"{entry.seq:04d}"
            data = contents[entry.manifest_path]
            fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=staging_fd,
            )
            try:
                view = memoryview(data)
                written = 0
                while written < len(view):
                    written += os.write(fd, view[written:])
                os.fsync(fd)
                info = os.fstat(fd)
            finally:
                os.close(fd)
            check = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=staging_fd)
            try:
                verify_info = os.fstat(check)
                if not stat.S_ISREG(verify_info.st_mode) or verify_info.st_nlink != 1:
                    raise ExportError(
                        "staging-failed",
                        f"the staged copy of {entry.manifest_path!r} is not an "
                        "ordinary file",
                        path=entry.manifest_path,
                    )
                staged_bytes = _read_all(check, MAX_FILE_BYTES)
            finally:
                os.close(check)
            if (
                len(staged_bytes) != entry.expected_length
                or hashlib.sha256(staged_bytes).hexdigest() != entry.expected_sha256
            ):
                raise ExportError(
                    "staging-failed",
                    f"the staged copy of {entry.manifest_path!r} does not match "
                    "the accepted revision; nothing was installed",
                    path=entry.manifest_path,
                )
            record_staged_file(
                self._engine,
                record.id,
                entry.manifest_path,
                device=info.st_dev,
                inode=info.st_ino,
            )
            staged[entry.manifest_path] = name
        return staged

    def _install_one(
        self,
        root: RootHandle,
        record: ExportRecord,
        entry,
        staging_fd: int,
        staged_name: str,
        created_dirs: list[str],
    ) -> str:
        """Move one verified staged file into its approved destination."""
        approved = {
            item["path"]: item for item in record.preview["ancestors"]
        }
        try:
            parent_fd = self._descend_creating(
                root, entry.destination, approved, created_dirs
            )
        except ExportError as exc:
            record_file_outcome(
                self._engine,
                record.id,
                entry.manifest_path,
                outcome=OUTCOME_NOT_INSTALLED,
                error=exc.detail,
            )
            return OUTCOME_NOT_INSTALLED
        try:
            name = posixpath.basename(entry.destination)
            try:
                rename_noreplace(staging_fd, staged_name, parent_fd, name)
            except FileExistsError:
                record_file_outcome(
                    self._engine,
                    record.id,
                    entry.manifest_path,
                    outcome=OUTCOME_NOT_INSTALLED,
                    error=(
                        "a file appeared at this destination after the preview "
                        "was approved; it was left exactly as it is"
                    ),
                )
                return OUTCOME_NOT_INSTALLED
            os.fsync(parent_fd)
            check = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
            try:
                observed = _stat_identity(os.fstat(check))
            finally:
                os.close(check)
            record_file_outcome(
                self._engine,
                record.id,
                entry.manifest_path,
                outcome=OUTCOME_CREATED,
                observed=observed,
            )
            return OUTCOME_CREATED
        finally:
            os.close(parent_fd)

    def _descend_creating(
        self,
        root: RootHandle,
        destination: str,
        approved: Mapping[str, Mapping[str, Any]],
        created: list[str],
    ) -> int:
        """Open the destination's parent, creating only expected directories.

        Every existing ancestor's identity is re-checked against the approved
        observation immediately before the install, so a directory replaced
        since the preview refuses instead of silently standing in for the one
        the operator reviewed. Each directory this export creates is appended
        to `created` as it happens — a new directory in the operator's checkout
        is an effect, and it is recorded whether or not the file it was made
        for goes on to install.
        """
        current = os.dup(root.fd)
        walked: list[str] = []
        try:
            for component in destination.split("/")[:-1]:
                walked.append(component)
                here = "/".join(walked)
                expected = approved.get(here)
                fresh = False
                try:
                    os.mkdir(component, mode=0o700, dir_fd=current)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise ExportError(
                        "directory-failed",
                        f"cannot create {here!r} in the checkout "
                        f"({exc.strerror})",
                        path=here,
                    ) from exc
                else:
                    fresh = True
                    created.append(here)
                    if expected is not None and expected.get("present"):
                        raise ExportError(
                            "ancestor-replaced",
                            f"{here!r} existed when the export was previewed "
                            "and does not now; preview again",
                            path=here,
                        )
                try:
                    nxt = _open_child_dir(current, component, root.device)
                except OSError as exc:
                    raise ExportError(
                        "ancestor-unusable",
                        f"{here!r} is not an ordinary directory in the "
                        f"checkout ({exc.strerror})",
                        path=here,
                    ) from exc
                os.close(current)
                current = nxt
                info = os.fstat(current)
                if expected is not None and expected.get("present"):
                    if (
                        expected.get("device") != info.st_dev
                        or expected.get("inode") != info.st_ino
                    ):
                        raise ExportError(
                            "ancestor-replaced",
                            f"{here!r} is not the directory this export was "
                            "approved against; preview again",
                            path=here,
                        )
                elif not fresh and here not in created:
                    # A directory that was absent at preview and was not
                    # created by this export appeared in between. That is a
                    # changed observation, not an acceptable substitute.
                    raise ExportError(
                        "ancestor-appeared",
                        f"{here!r} appeared in the checkout after the preview; "
                        "preview again before exporting into it",
                        path=here,
                    )
            return current
        except Exception:
            os.close(current)
            raise

    def _verify_identical(
        self, root: RootHandle, record: ExportRecord, entry
    ) -> str:
        """Confirm an already-identical destination is still identical.

        It receives no write at all — not the bytes, not a mode change, not a
        touched timestamp. Re-reading it is the only way "identical" can be a
        recorded outcome rather than an assumption.
        """
        parent_fd = os.dup(root.fd)
        detail: str | None = None
        observed: dict[str, Any] | None = None
        outcome = OUTCOME_IDENTICAL
        try:
            try:
                for component in entry.destination.split("/")[:-1]:
                    nxt = _open_child_dir(parent_fd, component, root.device)
                    os.close(parent_fd)
                    parent_fd = nxt
                fd = os.open(
                    posixpath.basename(entry.destination),
                    os.O_RDONLY | os.O_NOFOLLOW,
                    dir_fd=parent_fd,
                )
                try:
                    data = _read_all(fd, MAX_FILE_BYTES)
                    observed = _stat_identity(os.fstat(fd))
                finally:
                    os.close(fd)
            except (OSError, ExportError) as exc:
                reason = exc.detail if isinstance(exc, ExportError) else exc.strerror
                outcome = OUTCOME_NOT_INSTALLED
                detail = (
                    "this destination held the approved content at preview and "
                    f"is no longer readable ({reason}); nothing was written"
                )
            else:
                observed["sha256"] = hashlib.sha256(data).hexdigest()
                if observed["sha256"] != entry.expected_sha256:
                    outcome = OUTCOME_NOT_INSTALLED
                    detail = (
                        "this destination held the approved content at preview "
                        "and has changed since; it was left exactly as it is"
                    )
        finally:
            with contextlib.suppress(OSError):
                os.close(parent_fd)
        record_file_outcome(
            self._engine,
            record.id,
            entry.manifest_path,
            outcome=outcome,
            observed=observed,
            error=detail,
        )
        return outcome

    def _clean_staging(
        self, root: RootHandle, record: ExportRecord, staging_fd: int
    ) -> str | None:
        """Remove this export's own staging directory, and nothing else.

        Guarded by the identity recorded when it was created: a directory whose
        device and inode do not match is left alone and reported, because a
        recursive removal of an unexpected path in the operator's checkout is
        exactly the kind of damage this module exists to avoid.
        """
        name = staging_name_for(record.id)
        try:
            info = os.fstat(staging_fd)
            live = os.stat(name, dir_fd=root.fd, follow_symlinks=False)
            if (live.st_dev, live.st_ino) != (info.st_dev, info.st_ino):
                return (
                    f"{name!r} in the checkout is not the staging directory "
                    "this export created; it was left untouched"
                )
            for child in os.listdir(staging_fd):
                os.unlink(child, dir_fd=staging_fd)
        except OSError as exc:
            return self._staging_cleanup_error(name, exc)
        finally:
            # Closed exactly once, and before the rmdir: a second close could
            # land on whatever fd number the runtime handed out since.
            os.close(staging_fd)
        try:
            os.rmdir(name, dir_fd=root.fd)
        except OSError as exc:
            return self._staging_cleanup_error(name, exc)
        return None

    @staticmethod
    def _staging_cleanup_error(name: str, exc: OSError) -> str:
        return (
            f"the staging directory {name!r} could not be removed from the "
            f"checkout ({exc.strerror}); it is safe to delete by hand"
        )

    # -- reconciliation --

    def reconcile(self, record: ExportRecord) -> ExportRecord:
        """Re-observe an interrupted export and classify what it did.

        Read-only, always. A staged inode found at its destination establishes
        that the rename happened; identical bytes under a different identity do
        not, and are reported `unknown` rather than counted as a success.
        """
        outcomes: dict[str, tuple[str, dict[str, Any] | None, str | None]] = {}
        pending = [
            entry for entry in record.files if entry.outcome == OUTCOME_PENDING
        ]
        if not pending:
            state = self._classify(record.files, outcomes)
            return record_reconciliation(
                self._engine, record.id, state=state, error=None, outcomes=outcomes
            )
        try:
            root = self._reopen_root(record)
        except ExportError as exc:
            for entry in pending:
                if entry.staged_device is None:
                    outcomes[entry.manifest_path] = (
                        OUTCOME_NOT_INSTALLED,
                        None,
                        "this file was never staged, so it was never installed",
                    )
                else:
                    outcomes[entry.manifest_path] = (
                        OUTCOME_UNKNOWN,
                        None,
                        f"the checkout could not be inspected: {exc.detail}",
                    )
            state = self._classify(record.files, outcomes)
            return record_reconciliation(
                self._engine,
                record.id,
                state=state,
                error=exc.detail,
                outcomes=outcomes,
            )
        try:
            for entry in pending:
                outcomes[entry.manifest_path] = self._observe_effect(root, entry)
        finally:
            os.close(root.fd)
        state = self._classify(record.files, outcomes)
        return record_reconciliation(
            self._engine,
            record.id,
            state=state,
            error=(
                None
                if state == STATE_COMPLETED
                else "this export did not finish; the per-file outcomes say "
                "what could and could not be established"
            ),
            outcomes=outcomes,
        )

    def _observe_effect(
        self, root: RootHandle, entry
    ) -> tuple[str, dict[str, Any] | None, str | None]:
        if entry.staged_device is None:
            # Staging precedes every installation, so a destination whose file
            # was never staged was never written.
            return (
                OUTCOME_NOT_INSTALLED,
                None,
                "this file was never staged, so it was never installed",
            )
        parent_fd = os.dup(root.fd)
        try:
            try:
                for component in entry.destination.split("/")[:-1]:
                    nxt = _open_child_dir(parent_fd, component, root.device)
                    os.close(parent_fd)
                    parent_fd = nxt
                fd = os.open(
                    posixpath.basename(entry.destination),
                    os.O_RDONLY | os.O_NOFOLLOW,
                    dir_fd=parent_fd,
                )
            except FileNotFoundError:
                return (
                    OUTCOME_NOT_INSTALLED,
                    None,
                    (
                        "nothing exists at this destination, so nothing was "
                        "installed here"
                    ),
                )
            except (OSError, ExportError) as exc:
                detail = exc.detail if isinstance(exc, ExportError) else exc.strerror
                return (
                    OUTCOME_UNKNOWN,
                    None,
                    f"this destination could not be inspected ({detail})",
                )
            try:
                info = os.fstat(fd)
                observed = _stat_identity(info)
                data = _read_all(fd, MAX_FILE_BYTES)
            finally:
                os.close(fd)
        finally:
            with contextlib.suppress(OSError):
                os.close(parent_fd)
        observed["sha256"] = hashlib.sha256(data).hexdigest()
        if (info.st_dev, info.st_ino) == (entry.staged_device, entry.staged_inode):
            return (
                OUTCOME_CREATED,
                observed,
                (
                    "the staged file was found in place; the move completed "
                    "before the journal was updated"
                ),
            )
        if observed["sha256"] == entry.expected_sha256:
            return (
                OUTCOME_UNKNOWN,
                observed,
                (
                    "a file with exactly the expected content is here, but it "
                    "is not the file this export staged; who wrote it cannot "
                    "be established"
                ),
            )
        return (
            OUTCOME_UNKNOWN,
            observed,
            (
                "a different file is here and this export's staged copy was "
                "not found; whether anything was installed cannot be "
                "established"
            ),
        )

    def _classify(
        self,
        files: Sequence[Any],
        outcomes: Mapping[str, tuple[str, dict[str, Any] | None, str | None]],
    ) -> str:
        settled = [
            outcomes[entry.manifest_path][0]
            if entry.manifest_path in outcomes
            else entry.outcome
            for entry in files
        ]
        if any(outcome == OUTCOME_UNKNOWN for outcome in settled):
            return STATE_UNRESOLVED
        if all(
            outcome in (OUTCOME_CREATED, OUTCOME_IDENTICAL) for outcome in settled
        ):
            return STATE_COMPLETED
        return STATE_INCOMPLETE

    def recheck(self, record: ExportRecord) -> ExportRecord:
        """Repeat the read-only classification for an unresolved export.

        Offered because a checkout can become available again — a mount
        returns, a permission is fixed. It writes no destination, retries no
        effect, and can only ever move an outcome from unknown to something
        established.
        """
        if record.state not in (STATE_UNRESOLVED, STATE_INCOMPLETE):
            raise ExportStateError(
                record.id, f"is {record.state} and has nothing to reconcile"
            )
        unknown = {
            entry.manifest_path
            for entry in record.files
            if entry.outcome in (OUTCOME_UNKNOWN, OUTCOME_PENDING)
        }
        if not unknown:
            raise ExportStateError(
                record.id, "has no uncertain destinations left to re-observe"
            )
        outcomes: dict[str, tuple[str, dict[str, Any] | None, str | None]] = {}
        try:
            root = self._reopen_root(record)
        except ExportError as exc:
            raise ExportError("checkout-unavailable", exc.detail) from exc
        try:
            for entry in record.files:
                if entry.manifest_path in unknown:
                    outcomes[entry.manifest_path] = self._observe_effect(root, entry)
        finally:
            os.close(root.fd)
        state = self._classify(record.files, outcomes)
        settled = record_reconciliation(
            self._engine,
            record.id,
            state=state,
            error=None if state == STATE_COMPLETED else record.error,
            outcomes=outcomes,
        )
        self.publish(record.task_id)
        return settled

    def acknowledge(self, record: ExportRecord, *, expected_version: int) -> ExportRecord:
        settled = acknowledge_export(
            self._engine, record.id, expected_version=expected_version
        )
        self.publish(record.task_id)
        return settled


def _diff_payload(
    document: Mapping[str, Any],
    observation: Observation,
    contents: Mapping[str, bytes],
) -> dict[str, Any]:
    """Bounded, inert differences for the conflicts a preview can safely show.

    Never persisted with the approval and never broadcast: this text can be the
    contents of the operator's own checkout, and it exists only for the browser
    that asked for this preview.
    """
    lines: list[str] = []
    truncated = False
    size = 0
    for entry in document["files"]:
        if entry["classification"] != CLASS_CONFLICT:
            continue
        existing = observation.existing_text.get(entry["destination"])
        if existing is None:
            continue
        retained = contents[entry["path"]].decode("utf-8", errors="replace")
        for line in difflib.unified_diff(
            existing.splitlines(keepends=True),
            retained.splitlines(keepends=True),
            fromfile=f"checkout/{entry['destination']}",
            tofile=f"result/{entry['path']}",
        ):
            size += len(line.encode("utf-8"))
            if size > MAX_DIFF_BYTES:
                truncated = True
                break
            lines.append(line if line.endswith("\n") else line + "\n")
        if truncated:
            break
    return {"diff": "".join(lines), "diff_truncated": truncated}
