"""Handoff inputs: the rules an attached result revision must satisfy, the
Git observation a launch is reviewed against, and the materialization that
puts reviewed bytes into a recipient's own clone.

One module, because launch resolution and the spawn pipeline have to agree
exactly. A preview that decided a destination was free and a materialization
that decided it was taken would be two different opinions about the same
question, and the operator only reviewed one of them.

Three boundaries live here.

**Bounds and destinations** are purely syntactic and are checked first. The
combined selection reuses the retained-result limits — a handoff is a small
bundle, not a package feed — and destinations are the manifest's *original*
paths. There is no remapping, so a collision is a refusal the operator resolves
by choosing differently, never a rename Ompire invents.

**The target observation** reads the commit the recipient's clone will actually
be built from and inspects that commit's tree with bounded, literal-path
plumbing. It is read-only: nothing fetches, checks out, or writes. What it
produces is bound into the reviewed fingerprint, so acceptance is checked
against the same immutable tree the operator saw.

**Materialization** copies retained bytes into the fresh clone through
descriptor-relative, `O_NOFOLLOW` traversal, creating files exclusively. It
refuses every existing entry rather than overwriting one, and it verifies the
complete installed set before the workshop starts. A failure leaves a failed,
inspectable task — never a runnable one with partial inputs.

Attached text is data. Nothing here parses it, executes it, or reads authority
out of it (ADR-0035).
"""

from __future__ import annotations

import hashlib
import os
import posixpath
import stat
from dataclasses import dataclass, field

from ompire_daemon.execution_inputs import (
    BASE_COMPARISON_DIFFERENT,
    BASE_COMPARISON_MATCH,
    BASE_COMPARISON_UNKNOWN,
    AttachedFile,
    BaseComparison,
    ResultAttachment,
)
from ompire_daemon.registry.results import (
    MAX_FILES,
    MAX_TOTAL_BYTES,
    validate_relative_path,
)

# At most this many bundles in one launch. The per-file and byte limits already
# bound the payload; this bounds the *selection*, so a preview cannot be made
# to do unbounded work by naming thousands of revisions.
MAX_ATTACHMENTS = 128

# Root files that configure the host-side launcher. An attachment may not
# create them under any circumstances: my-workshop reads `workshop.yaml` from
# the clone root before an agent exists, so a captured file landing there would
# let retained text change how the container itself is built.
RESERVED_ROOT_FILES = ("workshop.yaml", "workshop.my.yaml")

# The bounded base comparison. Both caps are stated to the operator whenever
# they bite: a truncated list is a partial answer, and presenting it as the
# whole difference would be the same lie as presenting no comparison as a match.
MAX_COMPARISON_ENTRIES = 128
MAX_COMPARISON_BYTES = 64 * 1024

# Git tree entry modes that are not an ordinary directory or file.
_MODE_SYMLINK = "120000"
_MODE_SUBMODULE = "160000"


class HandoffError(Exception):
    """One attached selection cannot be launched, and why.

    `reason` is a stable machine code the form keys its explanation off;
    `detail` is the sentence the operator reads. `path` is present whenever a
    specific destination is at fault, because "an attachment conflicts" is not
    a correction anyone can act on.
    """

    def __init__(self, reason: str, detail: str, *, path: str | None = None) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.path = path


@dataclass(frozen=True)
class DestinationPlan:
    """Every destination one launch would create, already checked against
    itself. Ordered, so a preview and an installation walk them identically."""

    paths: tuple[str, ...]
    total_bytes: int
    file_count: int


def _ancestors(path: str) -> tuple[str, ...]:
    """Every directory a destination needs, outermost first."""
    parts = path.split("/")[:-1]
    return tuple("/".join(parts[: index + 1]) for index in range(len(parts)))


def plan_destinations(attachments: list[ResultAttachment]) -> DestinationPlan:
    """The combined destination set, or the first refusal.

    All-or-nothing and order-independent: a selection is either wholly
    installable or it is refused with the offending path named. Overlap is
    checked both ways — one bundle's file cannot be another's parent directory,
    because only one of the two can exist on a filesystem.
    """
    if len(attachments) > MAX_ATTACHMENTS:
        raise HandoffError(
            "too-many-attachments",
            f"a launch can attach at most {MAX_ATTACHMENTS} result revisions; "
            f"{len(attachments)} were selected",
        )
    seen_revisions: set[str] = set()
    for attachment in attachments:
        if attachment.result_id in seen_revisions:
            raise HandoffError(
                "duplicate-revision",
                f"result {attachment.result_id} is attached more than once",
            )
        seen_revisions.add(attachment.result_id)
        if not attachment.files:
            raise HandoffError(
                "empty-attachment",
                f"result {attachment.result_id} has no files to attach",
            )

    owner: dict[str, str] = {}
    directories: dict[str, str] = {}
    total = 0
    count = 0
    for attachment in attachments:
        for entry in attachment.files:
            path = validate_relative_path(entry.path)
            if path in RESERVED_ROOT_FILES:
                raise HandoffError(
                    "reserved-destination",
                    f"{path!r} configures the workshop launcher and can never "
                    "be written by an attachment",
                    path=path,
                )
            previous = owner.get(path)
            if previous is not None:
                raise HandoffError(
                    "destination-collision",
                    f"{path!r} is attached by both {previous} and "
                    f"{attachment.result_id}; attach only one of them",
                    path=path,
                )
            owner[path] = attachment.result_id
            total += entry.length
            count += 1
        for entry in attachment.files:
            for ancestor in _ancestors(entry.path):
                directories.setdefault(ancestor, attachment.result_id)

    for path, holder in sorted(owner.items()):
        conflicting = directories.get(path)
        if conflicting is not None:
            raise HandoffError(
                "destination-collision",
                f"{path!r} is a file in {holder} and a directory in "
                f"{conflicting}; attach only one of them",
                path=path,
            )

    if count > MAX_FILES:
        raise HandoffError(
            "too-many-files",
            f"the attached bundles hold {count} files; a launch can install at "
            f"most {MAX_FILES}",
        )
    if total > MAX_TOTAL_BYTES:
        raise HandoffError(
            "attachments-too-large",
            f"the attached bundles hold {total} bytes; a launch can install at "
            f"most {MAX_TOTAL_BYTES}",
        )
    return DestinationPlan(
        paths=tuple(sorted(owner)), total_bytes=total, file_count=count
    )


# --- The target observation -------------------------------------------------


@dataclass(frozen=True)
class TreeEntry:
    mode: str
    kind: str
    path: str


@dataclass(frozen=True)
class TargetObservation:
    """What the recipient's base commit already contains at these destinations.

    Bound into the reviewed fingerprint by way of `commit` and `conflicts`: the
    commit is immutable, so a preview reviewed against it stays meaningful, and
    a ref that moved afterwards is a *different* observation the acceptance
    refuses rather than silently adopting.
    """

    commit: str
    conflicts: tuple[HandoffError, ...] = field(default=())

    @property
    def blocked(self) -> bool:
        return bool(self.conflicts)


def classify_target_entries(
    destinations: tuple[str, ...], entries: list[TreeEntry]
) -> tuple[HandoffError, ...]:
    """Turn a tree reading into the refusals it implies.

    Deliberately pure, so the rules can be exercised without a repository. A
    destination that is already tracked is refused *even when its bytes are
    identical*: publication protection is a destination contract, and a tracked
    path is one the recipient's ordinary Git result would carry.
    """
    by_path = {entry.path: entry for entry in entries}
    conflicts: list[HandoffError] = []
    for path in destinations:
        entry = by_path.get(path)
        if entry is not None:
            if entry.kind == "tree":
                conflicts.append(
                    HandoffError(
                        "destination-is-directory",
                        f"{path!r} is a directory on the target base; an "
                        "attachment installs a file there",
                        path=path,
                    )
                )
            elif entry.mode == _MODE_SYMLINK:
                conflicts.append(
                    HandoffError(
                        "destination-symlink",
                        f"{path!r} is a symlink on the target base",
                        path=path,
                    )
                )
            elif entry.mode == _MODE_SUBMODULE:
                conflicts.append(
                    HandoffError(
                        "destination-submodule",
                        f"{path!r} is a submodule on the target base",
                        path=path,
                    )
                )
            else:
                conflicts.append(
                    HandoffError(
                        "destination-tracked",
                        f"{path!r} is already tracked on the target base; "
                        "handoff inputs are never published, so they cannot be "
                        "installed over a tracked file — even an identical one",
                        path=path,
                    )
                )
            continue
        for ancestor in _ancestors(path):
            parent = by_path.get(ancestor)
            if parent is None or parent.kind == "tree":
                continue
            if parent.mode == _MODE_SYMLINK:
                reason, what = "ancestor-symlink", "a symlink"
            elif parent.mode == _MODE_SUBMODULE:
                reason, what = "ancestor-submodule", "a submodule"
            else:
                reason, what = "ancestor-not-a-directory", "a file"
            conflicts.append(
                HandoffError(
                    reason,
                    f"{path!r} cannot be installed: {ancestor!r} is {what} on "
                    "the target base",
                    path=path,
                )
            )
            break
    return tuple(conflicts)


def observation_paths(destinations: tuple[str, ...]) -> list[str]:
    """Every path the tree reading has to ask about: the destinations and each
    of their ancestor directories, deduplicated."""
    wanted: set[str] = set(destinations)
    for path in destinations:
        wanted.update(_ancestors(path))
    return sorted(wanted)


def compare_bases(
    *,
    result_id: str,
    target_commit: str,
    producer_observation: str | None,
    comparable: bool,
    changed_paths: tuple[str, ...] = (),
    truncated: bool = False,
    detail: str | None = None,
) -> BaseComparison:
    """Classify the producer's recorded observation against this target.

    `producer_observation` is the producer's `capture_merge_base` — what Git
    said when its files were captured, which is deliberately *not* the commit
    the producing task was launched from. Equality is evidence the plan was
    written against these files; it is not evidence the plan is correct.
    """
    if producer_observation is None:
        return BaseComparison(
            result_id=result_id,
            state=BASE_COMPARISON_UNKNOWN,
            target_commit=target_commit,
            producer_observation=None,
            detail=detail
            or "the producing capture recorded no base observation, so this "
            "plan cannot be compared with the target base",
        )
    if producer_observation == target_commit:
        return BaseComparison(
            result_id=result_id,
            state=BASE_COMPARISON_MATCH,
            target_commit=target_commit,
            producer_observation=producer_observation,
        )
    if not comparable:
        return BaseComparison(
            result_id=result_id,
            state=BASE_COMPARISON_DIFFERENT,
            target_commit=target_commit,
            producer_observation=producer_observation,
            detail=detail
            or "the producer's base commit is not present in this checkout, so "
            "the difference cannot be listed",
        )
    return BaseComparison(
        result_id=result_id,
        state=BASE_COMPARISON_DIFFERENT,
        target_commit=target_commit,
        producer_observation=producer_observation,
        changed_paths=changed_paths,
        truncated=truncated,
        detail=detail,
    )


# --- Materialization --------------------------------------------------------


class MaterializationError(Exception):
    """Installation refused, naming the destination that stopped it.

    Raised before or during writing. Nothing is rolled back: a failed clone is
    left exactly as it is, inspectable, and removed by ordinary cleanup. The
    alternative — deleting on the way out — risks removing work that was
    already there, which is a worse failure than a workspace that says why it
    is unusable.
    """

    def __init__(self, detail: str, *, path: str | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.path = path


def _open_directory(parent_fd: int, name: str, *, path: str, device: int) -> int:
    """Open one existing directory component without following a symlink.

    Also refuses a component on a different filesystem than the clone root. A
    bind mount is not a symlink, so `O_NOFOLLOW` says nothing about it: without
    this check a mount planted inside the workspace would be a place a handoff
    could be written that is not part of the workspace at all.
    """
    try:
        fd = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=parent_fd
        )
    except OSError as exc:
        raise MaterializationError(
            f"cannot open {path!r} as an ordinary directory in the workspace: {exc}",
            path=path,
        ) from exc
    if os.fstat(fd).st_dev != device:
        os.close(fd)
        raise MaterializationError(
            f"{path!r} is on a different filesystem than the task workspace",
            path=path,
        )
    return fd


def _descend(root_fd: int, path: str, device: int) -> int:
    """Open (creating as needed) every directory of `path`, no-follow at each
    step, and return the descriptor of the file's immediate parent.

    Resolving the whole string once and opening it would be a different
    function entirely: between the resolve and the open, any component can
    become a link pointing outside the clone. An existing *ordinary* directory
    is reused; anything else — a file, a symlink, a device — refuses, because
    `O_NOFOLLOW | O_DIRECTORY` will not open it.
    """
    current = os.dup(root_fd)
    walked: list[str] = []
    try:
        for component in path.split("/")[:-1]:
            walked.append(component)
            here = "/".join(walked)
            try:
                os.mkdir(component, mode=0o700, dir_fd=current)
            except FileExistsError:
                pass
            except OSError as exc:
                raise MaterializationError(
                    f"cannot create directory {here!r} in the workspace: {exc}",
                    path=here,
                ) from exc
            nxt = _open_directory(current, component, path=here, device=device)
            os.close(current)
            current = nxt
        return current
    except Exception:
        os.close(current)
        raise


def payload_key(result_id: str, path: str) -> str:
    """Key retained bytes by revision *and* path.

    Two bundles holding the same relative path in different revisions must not
    collide in the payload map before the destination check has had its say.
    """
    return f"{result_id}\0{path}"


def install_attachments(
    clone_path: str,
    attachments: list[ResultAttachment],
    payloads: dict[str, bytes],
) -> tuple[str, ...]:
    """Write every attached file into `clone_path`, or refuse.

    Files are created exclusively, mode `0600`, as ordinary regular files —
    never executable, never a link, never over an existing entry. After the
    last write the complete set is re-read and hashed against the manifests the
    launch was accepted with, so a task never starts on a workspace whose
    inputs merely *look* installed.
    """
    plan = plan_destinations(attachments)
    try:
        root_fd = os.open(clone_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    except OSError as exc:
        raise MaterializationError(
            f"cannot open the task workspace {clone_path}: {exc}"
        ) from exc
    installed: list[tuple[str, AttachedFile]] = []
    try:
        # Every component, and every created file, must stay on the clone's own
        # filesystem.
        device = os.fstat(root_fd).st_dev
        for attachment in attachments:
            for entry in attachment.files:
                data = payloads[payload_key(attachment.result_id, entry.path)]
                _install_one(root_fd, entry.path, data, device)
                installed.append((entry.path, entry))
        _verify_installed(root_fd, installed, device)
    finally:
        os.close(root_fd)
    return plan.paths


def _install_one(root_fd: int, path: str, data: bytes, device: int) -> None:
    parent_fd = _descend(root_fd, path, device)
    name = posixpath.basename(path)
    try:
        try:
            fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
        except FileExistsError as exc:
            raise MaterializationError(
                f"{path!r} already exists in the task workspace; an attachment "
                "never replaces an existing entry",
                path=path,
            ) from exc
        except OSError as exc:
            raise MaterializationError(
                f"cannot create {path!r} in the task workspace: {exc}", path=path
            ) from exc
        try:
            stats = os.fstat(fd)
            if stats.st_nlink != 1:  # pragma: no cover - defensive
                raise MaterializationError(
                    f"{path!r} is multiply linked in the workspace", path=path
                )
            if stats.st_dev != device:  # pragma: no cover - defensive
                raise MaterializationError(
                    f"{path!r} is on a different filesystem than the task "
                    "workspace",
                    path=path,
                )
            view = memoryview(data)
            written = 0
            while written < len(view):
                written += os.write(fd, view[written:])
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def _verify_installed(
    root_fd: int, installed: list[tuple[str, AttachedFile]], device: int
) -> None:
    """Re-read every installed file and compare it with the accepted manifest.

    The check that makes "prepared" mean something. It runs after the last
    write and before the pipeline reports success, so an interrupted or
    partially written workspace is a failure the operator sees rather than a
    task that starts with inputs nobody verified.
    """
    for path, entry in installed:
        parent_fd = _descend(root_fd, path, device)
        try:
            fd = os.open(
                posixpath.basename(path),
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
        except OSError as exc:
            raise MaterializationError(
                f"cannot re-read the installed {path!r}: {exc}", path=path
            ) from exc
        try:
            stats = os.fstat(fd)
            if not stat.S_ISREG(stats.st_mode) or stats.st_dev != device:
                raise MaterializationError(
                    f"{path!r} is not an ordinary file inside the task workspace "
                    "after installation",
                    path=path,
                )
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            data = b"".join(chunks)
        finally:
            os.close(fd)
            os.close(parent_fd)
        if len(data) != entry.length or hashlib.sha256(data).hexdigest() != entry.sha256:
            raise MaterializationError(
                f"the installed {path!r} does not match the accepted revision",
                path=path,
            )


# --- Reading the target base ------------------------------------------------
#
# Read-only Git, run against the *project's* checkout with the repository's own
# configuration disarmed. The recipient's clone does not exist yet at preview
# time, and this must never be the moment repository-controlled configuration
# executes on the host.

_GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
}


async def _git(
    checkout_path: str, args: list[str], timeout: int
) -> tuple[int, str, str]:
    import asyncio

    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        checkout_path,
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **_GIT_ENV},
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise HandoffError(
            "target-unreadable", f"reading the target base timed out after {timeout}s"
        ) from None
    return (
        process.returncode or 0,
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace"),
    )


async def _git_stdin(
    checkout_path: str, args: list[str], stdin: str, timeout: int
) -> tuple[int, str, str]:
    import asyncio

    process = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        checkout_path,
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **_GIT_ENV},
    )
    try:
        out, err = await asyncio.wait_for(
            process.communicate(stdin.encode("utf-8")), timeout=timeout
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        raise HandoffError(
            "target-unreadable", f"reading the target base timed out after {timeout}s"
        ) from None
    return (
        process.returncode or 0,
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace"),
    )


async def resolve_target_commit(
    checkout_path: str, base_branch: str, timeout: int
) -> str:
    """The exact commit the recipient's clone will be built from.

    Resolved through the same ref spawn's clone resolves — the checkout's own
    branch head, which becomes `origin/<base>` in the task clone. An attachment
    launch requires this: an unknown target is refused rather than offered as
    a base nobody can compare a plan against.
    """
    code, out, _err = await _git(
        checkout_path,
        ["rev-parse", "--verify", "--quiet", f"refs/heads/{base_branch}^{{commit}}"],
        timeout,
    )
    commit = out.strip()
    if code != 0 or not commit:
        raise HandoffError(
            "target-base-unresolved",
            f"the base branch {base_branch!r} does not resolve to a commit in "
            "this project's checkout, so there is no target to attach against",
        )
    return commit


def parse_tree_entries(stdout: str) -> list[TreeEntry]:
    """Parse `ls-tree -z` output: `<mode> SP <type> SP <object> TAB <path>`."""
    entries: list[TreeEntry] = []
    for record in stdout.split("\0"):
        if not record.strip():
            continue
        head, _tab, path = record.partition("\t")
        parts = head.split()
        if len(parts) < 3 or not path:
            continue
        entries.append(TreeEntry(mode=parts[0], kind=parts[1], path=path))
    return entries


def parse_batch_types(paths: list[str], stdout: str) -> dict[str, str]:
    """Zip `cat-file --batch-check` output back onto the paths that asked for it.

    One line per input line, in order, so the mapping is positional. Captured
    paths carry no control characters — the retained-path syntax rules refuse
    them — so newline-delimited input is unambiguous, which is what makes the
    positional reading safe.
    """
    lines = stdout.splitlines()
    found: dict[str, str] = {}
    for path, line in zip(paths, lines, strict=False):
        fields = line.split()
        if len(fields) >= 2 and fields[-1] not in {"missing", "ambiguous"}:
            found[path] = fields[1]
    return found


async def observe_target(
    *,
    checkout_path: str,
    base_branch: str,
    destinations: tuple[str, ...],
    timeout: int,
) -> TargetObservation:
    """What the target base already holds at these destinations.

    Two bounded readings, in this order and for this reason. `cat-file
    --batch-check` answers "does this exact path exist, and is it a tree?" in
    one command whose output is exactly one line per asked path — where
    `ls-tree` with a directory pathspec would list that directory's entire
    contents, so a destination three levels down a large tree would make the
    preview read an unbounded amount.

    Only the paths that turned out *not* to be trees are then read with
    `ls-tree`, which is what distinguishes a symlink from a submodule from an
    ordinary file. That second reading is bounded by the first one's answers.

    Literal pathspecs throughout: a captured filename containing Git glob
    metacharacters is a path, not a pattern, or a conflicting destination would
    be reported as absent.
    """
    commit = await resolve_target_commit(checkout_path, base_branch, timeout)
    if not destinations:
        return TargetObservation(commit=commit)
    wanted = observation_paths(destinations)
    code, out, err = await _git_stdin(
        checkout_path,
        ["cat-file", "--batch-check"],
        "".join(f"{commit}:{path}\n" for path in wanted),
        timeout,
    )
    if code != 0:
        raise HandoffError(
            "target-unreadable",
            f"could not read the target base's tree: {err.strip() or code}",
        )
    kinds = parse_batch_types(wanted, out)
    entries = [
        TreeEntry(mode="040000", kind="tree", path=path)
        for path, kind in kinds.items()
        if kind == "tree"
    ]
    non_trees = sorted(path for path, kind in kinds.items() if kind != "tree")
    if non_trees:
        code, out, err = await _git(
            checkout_path,
            ["--literal-pathspecs", "ls-tree", "-z", "--full-tree", commit, "--", *non_trees],
            timeout,
        )
        if code != 0:
            raise HandoffError(
                "target-unreadable",
                f"could not read the target base's tree: {err.strip() or code}",
            )
        entries.extend(parse_tree_entries(out))
    return TargetObservation(
        commit=commit,
        conflicts=classify_target_entries(destinations, entries),
    )


async def observe_base_difference(
    *,
    result_id: str,
    checkout_path: str,
    target_commit: str,
    producer_observation: str | None,
    timeout: int,
) -> BaseComparison:
    """Compare the producer's recorded base observation with this target.

    Never fetches. A producer object this checkout does not hold is a named
    comparison gap, not a network operation and not a silent "no differences".
    """
    if producer_observation is None or producer_observation == target_commit:
        return compare_bases(
            result_id=result_id,
            target_commit=target_commit,
            producer_observation=producer_observation,
            comparable=False,
        )
    code, _out, _err = await _git(
        checkout_path,
        ["cat-file", "-e", f"{producer_observation}^{{commit}}"],
        timeout,
    )
    if code != 0:
        return compare_bases(
            result_id=result_id,
            target_commit=target_commit,
            producer_observation=producer_observation,
            comparable=False,
        )
    code, out, err = await _git(
        checkout_path,
        [
            "--literal-pathspecs",
            "diff",
            "--name-status",
            "-z",
            producer_observation,
            target_commit,
        ],
        timeout,
    )
    if code != 0:
        return compare_bases(
            result_id=result_id,
            target_commit=target_commit,
            producer_observation=producer_observation,
            comparable=False,
            detail=f"the difference could not be read: {err.strip() or code}",
        )
    truncated = len(out.encode("utf-8")) > MAX_COMPARISON_BYTES
    fields = [field for field in out.split("\0") if field]
    changed: list[str] = []
    index = 0
    while index < len(fields) - 1:
        status = fields[index]
        # Rename and copy statuses carry two paths; the destination is what a
        # reader cares about here.
        if status[:1] in {"R", "C"}:
            changed.append(fields[index + 2] if index + 2 < len(fields) else fields[index + 1])
            index += 3
        else:
            changed.append(fields[index + 1])
            index += 2
    if len(changed) > MAX_COMPARISON_ENTRIES:
        changed = changed[:MAX_COMPARISON_ENTRIES]
        truncated = True
    return compare_bases(
        result_id=result_id,
        target_commit=target_commit,
        producer_observation=producer_observation,
        comparable=True,
        changed_paths=tuple(changed),
        truncated=truncated,
    )
