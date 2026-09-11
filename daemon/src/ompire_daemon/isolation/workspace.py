"""Workspace lifecycle operations over resolved values.

Everything here answers one question: where may untrusted work execute? A
frozen `WorkspaceSpec` carries only what the operations need — an opaque owner
identity, paths, an accepted source checkout and fetch remote, a rendered
branch, an optional pinned source commit, an additions selection, supplied
protected destinations, and the launcher argv and deadlines. The values are
resolved by the caller; nothing is re-read from a project, profile, or
configuration during preparation (ADR-0026), and this module never consults a
task registry, workflow, or HTTP state — an arbitrary integer owner with no
task row can use every operation (ADR-0039).

The operations are deliberately staged rather than one provisioning function:
`prepare_clone` builds the clone (fetch, hardlink clone, exclusions, branch),
`verify_pinned_source` checks the cloned base against the reviewed commit,
`launch_workshop` stages the selected additions around the launcher and
returns the container identity, and `destroy_workspace` performs the
confined, container-first teardown. Recording what happened — task state,
events, workflow start — belongs to the application coordinator, not here.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ompire_daemon.isolation import additions, excludes
from ompire_daemon.isolation.workshop import remove_workshop
from ompire_daemon.platform.processes import (
    ProcessStep,
    ProcessStepError,
    run_process_step,
)


class WorkspaceOperationError(Exception):
    """A workspace resource operation failed.

    `step` names the preparation phase the failure belongs to (`fetch`,
    `clone`, `branch`, `inputs`, `workshop`); `detail` is the diagnosis the
    caller shows. Every failure in a preparation sequence is one of these —
    nothing else may escape a resource operation and leave work looking
    in-progress.
    """

    def __init__(self, step: str, detail: str) -> None:
        super().__init__(f"step {step!r} failed:\n{detail}")
        self.step = step
        self.detail = detail


class WorkspaceDeletionRefused(Exception):
    """The resolved clone is not strictly below the owned task root."""

    def __init__(self, clone_path: Path, task_root: Path) -> None:
        super().__init__(
            f"refusing to delete {clone_path}: outside task root {task_root}"
        )
        self.clone_path = clone_path
        self.task_root = task_root


@dataclass(frozen=True)
class WorkspaceSpec:
    """Everything a workspace's resource operations need, already resolved.

    `owner_id` is an opaque integer token — not a database foreign key — and
    `protected_destinations` are supplied, already-decided paths; neither is
    derived or looked up here.
    """

    owner_id: int
    clone_path: str
    task_root: Path
    checkout_path: str
    fetch_remote: str
    base_branch: str
    branch: str
    source_commit: str | None
    additions_source: str
    protected_destinations: tuple[str, ...]
    data_dir: Path
    launcher_argv: tuple[str, ...]
    git_timeout: int
    workshop_timeout: int


@dataclass(frozen=True)
class WorkspaceRef:
    """A reference to one prepared workspace: its owner token and clone path."""

    owner_id: int
    clone_path: str


@dataclass(frozen=True)
class PhaseProgress:
    """One preparation phase transition (`started` / `ok`), reported at the
    operation boundary — never as a buffered list published afterwards."""

    step: str
    status: str


def _confine(clone_path: str, task_root: Path) -> Path:
    """The resolved clone path, refusing anything not strictly below the root.

    The root itself and paths outside it are refused: both at creation and at
    destruction, without consulting any registry.
    """
    root = task_root.expanduser().resolve()
    resolved = Path(clone_path).resolve()
    if root not in resolved.parents:
        raise WorkspaceDeletionRefused(resolved, root)
    return resolved


def _git_steps(spec: WorkspaceSpec) -> list[ProcessStep]:
    """The clone-building steps, entirely from the workspace's resolved values.

    The base checkout's own fetch remote, which is not necessarily `origin`
    (ADR-0022), is fetched first; the local source path then gives the task
    clone a near-instant hardlink clone whose `origin` points at that
    checkout. An attached launch branches from the pinned source commit, not
    from wherever the base branch points by the time the clone lands
    (ADR-0035).
    """
    timeout = spec.git_timeout
    return [
        ProcessStep(
            "fetch",
            ["git", "-C", spec.checkout_path, "fetch", spec.fetch_remote],
            timeout,
        ),
        ProcessStep(
            "clone", ["git", "clone", spec.checkout_path, spec.clone_path], timeout
        ),
        ProcessStep(
            "branch",
            [
                "git", "-C", spec.clone_path, "checkout", "-b", spec.branch,
                spec.source_commit
                if spec.source_commit
                else f"origin/{spec.base_branch}",
            ],
            timeout,
        ),
    ]


async def prepare_clone(
    spec: WorkspaceSpec,
    report_progress: Callable[[PhaseProgress], None],
) -> WorkspaceRef:
    """Build the clone: confinement and destination checks, then fetch, local
    clone, exclusions, and branch creation.

    An existing destination is refused loudly before any Git runs — crash
    residue from an earlier attempt is never reused (design D-4). The
    daemon-owned exclude entries and the supplied protected destinations are
    installed right after the clone exists and before any work is branched.
    """
    try:
        _confine(spec.clone_path, spec.task_root)
    except WorkspaceDeletionRefused as exc:
        raise WorkspaceOperationError("clone", str(exc)) from exc
    if Path(spec.clone_path).exists():
        raise WorkspaceOperationError(
            "clone", f"target directory already exists: {spec.clone_path}"
        )
    for step in _git_steps(spec):
        report_progress(PhaseProgress(step.name, "started"))
        try:
            await run_process_step(step)
            if step.name == "clone":
                await asyncio.to_thread(
                    excludes.ensure_git_excludes,
                    spec.clone_path,
                    protected=spec.protected_destinations,
                )
        except (ProcessStepError, excludes.ExcludeUpdateError) as exc:
            detail = exc.stderr if isinstance(exc, ProcessStepError) else exc.detail
            raise WorkspaceOperationError(step.name, detail) from exc
        report_progress(PhaseProgress(step.name, "ok"))
    return WorkspaceRef(owner_id=spec.owner_id, clone_path=spec.clone_path)


async def verify_pinned_source(
    clone_path: str,
    *,
    base_branch: str,
    source_commit: str,
    timeout: int,
) -> None:
    """Refuse the launch if the cloned base no longer resolves to the reviewed
    commit.

    SQLite cannot freeze a Git ref, and nothing pretends it did: the reviewed
    resolution named an immutable commit, and this is where the promise is
    checked against the world. A ref that moved between acceptance and the
    clone fails the preparation — it never repins the task to whatever is
    there now.
    """
    ref = f"origin/{base_branch}"
    try:
        process = await asyncio.create_subprocess_exec(
            "git", "-C", clone_path, "rev-parse", "--verify", "--quiet",
            f"{ref}^{{commit}}",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise WorkspaceOperationError("inputs", f"cannot run git: {exc}") from exc
    try:
        stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        # Every failure here has to be a `WorkspaceOperationError`: the
        # coordinator turns those into a visible failed task, and anything
        # else would escape the background job and leave the task looking as
        # though it were still spawning.
        process.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.wait(), timeout=5)
        raise WorkspaceOperationError(
            "inputs", f"reading the cloned base timed out after {timeout}s"
        ) from None
    resolved = stdout.decode("utf-8", errors="replace").strip()
    if resolved != source_commit:
        raise WorkspaceOperationError(
            "inputs",
            f"the base {ref} now resolves to {resolved or 'nothing'}, not the "
            f"reviewed {source_commit}; review the launch again rather "
            "than starting it against a base nobody approved",
        )


def _read_workshop_lock(clone_path: str) -> str:
    """Return the non-empty lock id from `.workshop.lock`, or fail.

    The file's format belongs to my-workshop; the daemon stores its content
    verbatim (stripped), interpreting nothing beyond non-emptiness (design
    D-2).
    """
    lock_path = Path(clone_path) / excludes.WORKSHOP_LOCK_FILENAME
    try:
        lock_id = lock_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise WorkspaceOperationError(
            "workshop", f"launch succeeded but {lock_path} is unreadable: {exc}"
        ) from exc
    if not lock_id:
        raise WorkspaceOperationError(
            "workshop", f"launch succeeded but {lock_path} is missing or empty"
        )
    return lock_id


async def launch_workshop(
    spec: WorkspaceSpec,
    workspace: WorkspaceRef,
    report_additions: Callable[[str, str], None],
) -> str:
    """Launch the workspace's container and return its nonempty identity.

    my-workshop creates/augments workshop.yaml, hides it from git, and
    launches the container. Its contract with the daemon is only "exit 0 and
    leave a `.workshop.lock`" (design D-1).

    The accepted additions source is staged around that call and undone
    afterwards either way, so the launcher applies the source the operator
    chose and the clone an agent later sees is unchanged. The disclosure —
    which source applied, and whether the selected one was simply absent — is
    reported through `report_additions` before the launcher runs; it is a fact
    about the workspace, not a pipeline step outcome.

    The identity is returned, not recorded: writing it down is the
    coordinator's job.
    """
    try:
        staged = additions.stage(
            spec.data_dir,
            spec.owner_id,
            spec.clone_path,
            spec.additions_source,
        )
    except additions.WorkshopAdditionsError as exc:
        raise WorkspaceOperationError("workshop", str(exc)) from exc
    report_additions(staged.source, staged.note)
    try:
        try:
            await run_process_step(
                ProcessStep(
                    "workshop",
                    [*spec.launcher_argv],
                    spec.workshop_timeout,
                    cwd=spec.clone_path,
                )
            )
        finally:
            additions.restore(spec.data_dir, staged)
    except ProcessStepError as exc:
        raise WorkspaceOperationError("workshop", exc.stderr) from exc
    return _read_workshop_lock(spec.clone_path)


async def destroy_workspace(
    clone_path: str,
    task_root: Path,
    *,
    workshop_id: str | None,
    workshop_timeout: int,
) -> None:
    """Tear the workspace down: the container first, then the clone.

    The resolved clone must be strictly below `task_root`; the root itself and
    anything outside it cannot be deleted here, with no task lookup involved.
    A recorded container identity is removed before the clone under it goes
    (design D-4): an already-gone workshop is success, any other removal
    failure aborts with the clone retained (`WorkshopRemoveError`). Removing
    an already-absent clone is an idempotent success.
    """
    resolved = _confine(clone_path, task_root)
    if workshop_id is not None:
        await remove_workshop(str(resolved), workshop_timeout)
    await asyncio.to_thread(shutil.rmtree, resolved, ignore_errors=True)
