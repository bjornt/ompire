"""Supervised creation of a project's base checkout.

Architecture: ADR-0022
(docs/adr/0022-create-or-adopt-base-checkouts-without-mutating-them.md)

Clone mode is the one place Ompire creates a git repository outside its own
task root, so the job is deliberately narrow:

- the destination is *derived* (`<checkout root>/<project name>`), never
  supplied, and a pre-existing destination is refused before any work starts;
- the clone is assembled at a staging path Ompire owns and moved onto the
  destination with a single rename, so the destination only ever holds a
  finished checkout — there is no partial tree for a later run to mistake for
  a real one;
- the only thing this module ever deletes is its own staging tree. A base
  checkout, adopted or cloned, is never removed by Ompire.

The job's shape follows `spawn.py`: named steps, argument-list subprocesses,
captured stderr, `started`/`ok`/`failed` events. It differs in one way that
matters — the outcome is written to the project row before it is broadcast,
because a project card must render `cloning`/`failed` from a reconnect
snapshot alone, not from step events it may have missed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy import Engine

from ompire_daemon.config import Config
from ompire_daemon.events import EventHub
from ompire_daemon.projectcheckout import inspect_checkout, no_prompt_env
from ompire_daemon.registry.projects import (
    Project,
    ProjectNotFoundError,
    get_project,
    list_setup_pending,
    mark_setup_cloning,
    mark_setup_failed,
    mark_setup_ready,
)

logger = logging.getLogger(__name__)

_STDERR_LIMIT = 64 * 1024

# The remote `git clone` creates, and therefore the fetch remote of every
# checkout Ompire makes itself.
CLONE_FETCH_REMOTE = "origin"
# Second remote added when the project has a fork push target.
FORK_REMOTE_NAME = "fork"

INTERRUPTED_ERROR = (
    "clone interrupted by daemon restart; nothing was written to the checkout "
    "path — retry setup to start again"
)


class CloneStepFailedError(Exception):
    def __init__(self, step: str, stderr: str) -> None:
        super().__init__(f"project setup step {step!r} failed")
        self.step = step
        self.stderr = stderr


@dataclass(frozen=True)
class CloneTarget:
    destination: Path
    staging: Path


def clone_target(checkout_root: Path, name: str) -> CloneTarget:
    """Where a clone-mode project's checkout goes, and where it is built.

    The staging sibling is hidden and name-scoped so two concurrent
    registrations cannot collide, and so a leftover from a killed daemon is
    unambiguously identifiable as Ompire's.
    """
    root = Path(checkout_root).expanduser()
    return CloneTarget(root / name, root / f".ompire-clone-{name}")


class DestinationExistsError(Exception):
    def __init__(self, path: Path) -> None:
        super().__init__(
            f"{path} already exists; clone mode never writes into an existing "
            "path — remove it, or register it with 'use an existing checkout'"
        )
        self.path = path


async def _run(argv: list[str], timeout: int, step: str, cwd: str | None = None) -> None:
    """Run one setup subprocess. Raises `CloneStepFailedError` on any failure."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            env=no_prompt_env(),
        )
    except OSError as exc:
        raise CloneStepFailedError(step, f"cannot exec {argv[0]!r}: {exc}") from exc
    try:
        _, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.CancelledError:
        if process.returncode is None:
            process.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.wait(), timeout=5)
        raise
    except TimeoutError:
        process.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.wait(), timeout=5)
        raise CloneStepFailedError(step, f"timed out after {timeout}s") from None
    stderr = stderr_bytes[-_STDERR_LIMIT:].decode("utf-8", errors="replace")
    if process.returncode != 0:
        raise CloneStepFailedError(step, stderr or f"git exited {process.returncode}")


def _remove_staging(staging: Path) -> None:
    """Delete Ompire's own staging tree. Never called with any other path."""
    with contextlib.suppress(OSError):
        shutil.rmtree(staging)


class ProjectSetupManager:
    """Owns clone-mode setup jobs and their startup reconciliation."""

    def __init__(self, config: Config, engine: Engine, events: EventHub) -> None:
        self._config = config
        self._engine = engine
        self._events = events
        self._jobs: dict[str, asyncio.Task[None]] = {}

    # --- driving ------------------------------------------------------------

    def start(self, project: Project) -> None:
        """Launch the background clone for an already-`cloning` project row."""
        existing = self._jobs.get(project.name)
        if existing is not None and not existing.done():
            return
        job = asyncio.create_task(self._run_clone(project))
        self._jobs[project.name] = job
        job.add_done_callback(lambda _: self._jobs.pop(project.name, None))

    def retry(self, name: str) -> Project:
        """Re-arm and restart a failed clone. Raises if it is not retryable."""
        project = get_project(self._engine, name)
        if project.checkout_mode != "cloned":
            raise ValueError(
                f"project {name!r} adopted an existing checkout; there is "
                "nothing for Ompire to retry"
            )
        if project.setup_state == "cloning":
            raise ValueError(f"project {name!r} is already being set up")
        armed = mark_setup_cloning(self._engine, name)
        self._events.publish("project_updated", asdict(armed))
        self.start(armed)
        return armed

    async def shutdown(self) -> None:
        jobs = list(self._jobs.values())
        for job in jobs:
            job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)

    # --- the job ------------------------------------------------------------

    def _step(self, name: str, step: str, status: str, stderr: str = "") -> None:
        payload: dict[str, str] = {"project": name, "step": step, "status": status}
        if stderr:
            payload["stderr"] = stderr
        self._events.publish("project_setup_step", payload)

    def _fail(self, name: str, error: str) -> None:
        try:
            failed = mark_setup_failed(self._engine, name, error)
        except ProjectNotFoundError:
            return
        self._events.publish("project_updated", asdict(failed))

    async def _run_clone(self, project: Project) -> None:
        target = clone_target(Path(project.checkout_path).parent, project.name)
        # `checkout_path` was derived from the effective root at registration;
        # deriving the staging sibling from it keeps both in the same
        # directory even if the root setting changed since.
        timeout = self._config.project_clone_timeout
        try:
            await self._clone_steps(project, target, timeout)
        except asyncio.CancelledError:
            # Daemon shutdown. The row stays `cloning`; startup reconciliation
            # is what resolves it, so the two paths cannot disagree.
            _remove_staging(target.staging)
            raise
        except CloneStepFailedError as exc:
            _remove_staging(target.staging)
            self._step(project.name, exc.step, "failed", exc.stderr)
            self._fail(project.name, f"step {exc.step!r} failed:\n{exc.stderr}")
            return
        except Exception as exc:
            _remove_staging(target.staging)
            logger.exception("project setup for %r failed", project.name)
            self._fail(project.name, f"project setup failed: {exc}")
            return
        try:
            ready = mark_setup_ready(self._engine, project.name)
        except ProjectNotFoundError:
            return
        self._events.publish("project_updated", asdict(ready))

    async def _clone_steps(
        self, project: Project, target: CloneTarget, timeout: int
    ) -> None:
        self._step(project.name, "prepare", "started")
        if target.destination.exists():
            raise CloneStepFailedError(
                "prepare", str(DestinationExistsError(target.destination))
            )
        _remove_staging(target.staging)
        try:
            target.destination.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CloneStepFailedError(
                "prepare", f"cannot create {target.destination.parent}: {exc}"
            ) from exc
        self._step(project.name, "prepare", "ok")

        self._step(project.name, "clone", "started")
        await _run(
            ["git", "clone", "--", project.upstream_url, str(target.staging)],
            timeout,
            "clone",
        )
        self._step(project.name, "clone", "ok")

        if project.fork_url:
            self._step(project.name, "fork-remote", "started")
            await _run(
                [
                    "git", "-C", str(target.staging), "remote", "add",
                    FORK_REMOTE_NAME, "--", project.fork_url,
                ],
                self._config.spawn_step_timeout,
                "fork-remote",
            )
            self._step(project.name, "fork-remote", "ok")

        self._step(project.name, "finalize", "started")
        try:
            os.rename(target.staging, target.destination)
        except OSError as exc:
            raise CloneStepFailedError(
                "finalize", f"cannot move clone into {target.destination}: {exc}"
            ) from exc
        self._step(project.name, "finalize", "ok")

    # --- startup ------------------------------------------------------------

    async def reconcile_pending(self) -> None:
        """Resolve every project left `cloning` by a stopped daemon.

        The filesystem is the authority: a checkout that survived the restart
        makes the project ready, anything else makes it failed and removes the
        staging tree. A clone is never restarted automatically — the operator
        decides whether to retry.
        """
        for project in list_setup_pending(self._engine):
            target = clone_target(Path(project.checkout_path).parent, project.name)
            inspection = await inspect_checkout(
                str(target.destination),
                fetch_remote=project.fetch_remote,
                timeout=self._config.spawn_step_timeout,
            )
            if inspection.ok:
                ready = mark_setup_ready(self._engine, project.name)
                self._events.publish("project_updated", asdict(ready))
                logger.info("project %r checkout completed before restart", project.name)
                continue
            _remove_staging(target.staging)
            failed = mark_setup_failed(self._engine, project.name, INTERRUPTED_ERROR)
            self._events.publish("project_updated", asdict(failed))
            logger.info("project %r clone was interrupted; marked failed", project.name)
