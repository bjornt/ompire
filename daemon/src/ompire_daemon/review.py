"""Daemon-run host-side review authority.

Architecture: ADR-0011
(docs/adr/0011-keep-review-and-publishing-authority-outside-agent-sandbox.md)

`ReviewManager` owns the per-task review state, the reset dance, the
supervised llmvet subprocess, exit interpretation, and the comment loopback
to the live agent. Review state is in-memory only (D-1); the only durable
artifact is the git ref `refs/ompire/review-orig` the reset dance writes in
the clone, which makes crash recovery a git operation.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import socket
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import Engine

from ompire_daemon.config import Config
from ompire_daemon.events import EventHub
from ompire_daemon.registry.tasks import Task
from ompire_daemon.registry.templates import get_template
from ompire_daemon.rpc import AgentGoneError, RequestFailedError
from ompire_daemon.spawn import Step, _run_step

if TYPE_CHECKING:

    from ompire_daemon.agent import AgentSupervisor
    from ompire_daemon.sessions import SessionTracker

logger = logging.getLogger(__name__)

REVIEW_GIT_REF = "refs/ompire/review-orig"
_REVIEW_ORIGIN_NAME = "origin"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


async def _run_git_output(argv: list[str], cwd: str, timeout: int, step_name: str) -> str:
    """Run a git command and return its stdout; raise ReviewError on failure."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=timeout
        )
    except TimeoutError as exc:
        raise ReviewError(f"{step_name} timed out after {timeout}s") from exc
    except OSError as exc:
        raise ReviewError(f"{step_name} cannot exec {argv[0]!r}: {exc}") from exc
    if process.returncode != 0:
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        raise ReviewError(f"{step_name} failed: {stderr}")
    return stdout_bytes.decode("utf-8", errors="replace")


class ReviewError(Exception):
    """Base for review-manager errors that should surface as review outcomes."""


class ReviewAlreadyOpenError(ReviewError):
    def __init__(self, task_id: int) -> None:
        super().__init__(f"task {task_id} already has an open review")
        self.task_id = task_id


@dataclass
class ReviewIteration:
    outcome: str  # approved | comments | aborted | error
    comment_count: int | None = None
    stderr: str | None = None
    recorded_at: str = field(default_factory=_now_iso)


@dataclass
class ReviewState:
    status: str  # open | approved | aborted | error
    url: str
    port: int
    iterations: list[ReviewIteration] = field(default_factory=list)


class ReviewManager:
    def __init__(
        self,
        config: Config,
        engine: Engine,
        hub: EventHub,
        sessions: SessionTracker,
        agents: AgentSupervisor,
    ) -> None:
        self._config = config
        self._engine = engine
        self._hub = hub
        self._sessions = sessions
        self._agents = agents
        self._reviews: dict[int, ReviewState] = {}
        self._processes: dict[int, asyncio.subprocess.Process] = {}
        self._watchers: dict[int, asyncio.Task] = {}
        self._port_lock = asyncio.Lock()
        self._event_task: asyncio.Task | None = None

    @staticmethod
    def _primary_session(task: Task) -> str:
        """Review attaches to the task's workflow-declared primary session
        (workflow-engine design D-8)."""
        from ompire_daemon.workflows import get_workflow

        return get_workflow(task.workflow_name).primary

    def start(self) -> None:
        """Start the hub event consumer; idempotent. Must be called from a
        running event loop (app lifespan)."""
        if self._event_task is None:
            self._event_task = asyncio.create_task(self._consume_events())

    def snapshot(self) -> dict[int, dict[str, Any]]:
        """Current reviews for the WebSocket snapshot (design D-6)."""
        return {
            task_id: {
                "status": state.status,
                "url": state.url,
                "port": state.port,
                "iterations": [
                    {
                        "outcome": it.outcome,
                        "comment_count": it.comment_count,
                        "stderr": it.stderr,
                        "recorded_at": it.recorded_at,
                    }
                    for it in state.iterations
                ],
            }
            for task_id, state in self._reviews.items()
        }

    def get(self, task_id: int) -> ReviewState | None:
        return self._reviews.get(task_id)

    async def shutdown(self) -> None:
        """Cancel every open review on daemon shutdown: SIGINT each llmvet,
        which records an aborted iteration and restores the clone. Idempotent.
        """
        if self._event_task is not None:
            self._event_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._event_task
            self._event_task = None
        # If start() was never called there should be no open reviews, but
        # be defensive on shutdown.
        for task_id in list(self._processes):
            with contextlib.suppress(ReviewError):
                await self.cancel_review(task_id)
        if self._watchers:
            await asyncio.gather(*list(self._watchers.values()), return_exceptions=True)

    # --- port allocation ----------------------------------------------------

    async def _allocate_port(self) -> int:
        """Probe the configured port range with an ephemeral localhost bind.

        The loop races with concurrent reviews; an async lock serializes the
        probe, and `SO_REUSEADDR` tolerates ports in TIME_WAIT. The first
        successful bind wins. On total exhaustion the resulting OSError is
        allowed to land the review in `error`.
        """
        low, high = self._config.review_port_range
        async with self._port_lock:
            for port in range(low, high + 1):
                sock: socket.socket | None = None
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    sock.bind(("127.0.0.1", port))
                    return sock.getsockname()[1]
                except OSError:
                    continue
                finally:
                    if sock is not None:
                        sock.close()
        raise ReviewError(
            f"no free port in review_port_range [{low}, {high}]"
        )

    # --- reset dance --------------------------------------------------------

    def _base_branch(self, task: Task) -> str:
        """`<base>` for the reset dance: the task's template base branch
        (templates capability, design D-3). Tasks that predate templates
        (null `template_name`) fall back to `main`."""
        if task.template_name is None:
            return "main"
        return get_template(self._engine, task.template_name).base_branch

    async def _fetch(self, clone_path: str, timeout: int) -> None:
        await _run_step(
            Step(
                "review-fetch",
                ["git", "-C", clone_path, "fetch", _REVIEW_ORIGIN_NAME],
                timeout,
            )
        )

    async def _save_review_orig(self, clone_path: str) -> str:
        """Return the original HEAD rev and save it under the durable ref."""
        orig = (
            await _run_git_output(
                ["git", "-C", clone_path, "rev-parse", "HEAD"],
                clone_path,
                self._config.spawn_step_timeout,
                "review-save-orig",
            )
        ).strip()
        if not orig:
            raise ReviewError("could not capture current HEAD for review restore")
        await _run_step(
            Step(
                "review-update-ref",
                ["git", "-C", clone_path, "update-ref", REVIEW_GIT_REF, orig],
                self._config.spawn_step_timeout,
            )
        )
        return orig

    async def _reset_to_merge_base(self, clone_path: str, base_branch: str) -> None:
        base = (
            await _run_git_output(
                [
                    "git",
                    "-C",
                    clone_path,
                    "merge-base",
                    f"{_REVIEW_ORIGIN_NAME}/{base_branch}",
                    "HEAD",
                ],
                clone_path,
                self._config.spawn_step_timeout,
                "review-merge-base",
            )
        ).strip()
        await _run_step(
            Step(
                "review-park-head",
                ["git", "-C", clone_path, "reset", "--mixed", base],
                self._config.spawn_step_timeout,
            )
        )

    async def _restore(self, clone_path: str) -> None:
        """Restore HEAD to the saved ref and delete the marker. Idempotent."""
        await _run_step(
            Step(
                "review-restore",
                ["git", "-C", clone_path, "reset", "--mixed", REVIEW_GIT_REF],
                self._config.spawn_step_timeout,
            )
        )
        await _run_step(
            Step(
                "review-delete-ref",
                ["git", "-C", clone_path, "update-ref", "-d", REVIEW_GIT_REF],
                self._config.spawn_step_timeout,
            )
        )

    # --- public lifecycle ---------------------------------------------------

    async def start_review(self, task: Task) -> ReviewState:
        task_id = task.id
        if task_id in self._processes:
            raise ReviewAlreadyOpenError(task_id)

        port = await self._allocate_port()
        base_branch = self._base_branch(task)
        clone_path = task.clone_path
        timeout = self._config.spawn_step_timeout

        await self._fetch(clone_path, timeout)
        await self._save_review_orig(clone_path)
        await self._reset_to_merge_base(clone_path, base_branch)

        url = f"http://127.0.0.1:{port}"
        state = self._reviews.get(task_id)
        if state is None:
            state = ReviewState(status="open", url=url, port=port)
            self._reviews[task_id] = state
        else:
            # Re-review after comments: new subprocess, same history.
            state.status = "open"
            state.url = url
            state.port = port

        primary = self._primary_session(task)
        self._sessions.review_opened(task_id, primary, f"llmvet review on {url}")
        self._hub.publish("review_started", {"task_id": task_id, "url": url, "port": port})

        watcher = asyncio.create_task(self._watch_review(task_id, task, port))
        self._watchers[task_id] = watcher
        watcher.add_done_callback(lambda t: self._pop_watcher(task_id, t))
        return state

    async def cancel_review(self, task_id: int) -> ReviewState:
        process = self._processes.get(task_id)
        if process is None:
            raise ReviewError(f"task {task_id} has no open review")
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.send_signal(signal.SIGINT)
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        # The watcher will record the aborted iteration and restore the clone.
        state = self._reviews.get(task_id)
        if state is None:
            raise ReviewError(f"task {task_id} has no review state")
        return state

    async def cancel_and_drop(self, task_id: int) -> None:
        """Cancel an open review if one exists, then drop in-memory state.
        Used by the cleanup path, which deletes the clone afterwards.
        """
        if task_id in self._processes:
            with contextlib.suppress(ReviewError):
                await self.cancel_review(task_id)
        self.drop_review(task_id)

    def drop_review(self, task_id: int) -> None:
        """Drop in-memory review state (cleanup/purge path). Does not restore
        the clone — cleanup already deletes the directory."""
        watcher = self._watchers.pop(task_id, None)
        if watcher is not None:
            watcher.cancel()
        self._processes.pop(task_id, None)
        self._reviews.pop(task_id, None)

    # --- internals ----------------------------------------------------------

    async def _watch_review(
        self, task_id: int, task: Task, port: int
    ) -> None:
        clone_path = task.clone_path
        process: asyncio.subprocess.Process | None = None
        try:
            argv = [
                *self._config.llmvet_command,
                "-no-open",
                "-host",
                "127.0.0.1",
                "-port",
                str(port),
            ]
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=clone_path,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._processes[task_id] = process
            stdout_bytes, stderr_bytes = await process.communicate()
        except Exception as exc:
            logger.exception("llmvet spawn failed for task %d", task_id)
            await self._finalize(
                task_id,
                task,
                outcome="error",
                stderr=f"failed to launch llmvet: {exc}",
                close_session=True,
            )
            return
        finally:
            self._processes.pop(task_id, None)
            await self._restore(clone_path)

        assert process is not None
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        code = process.returncode
        assert code is not None
        await self._interpret_exit(task_id, task, code, stdout, stderr)

    async def _interpret_exit(
        self, task_id: int, task: Task, code: int, stdout: str, stderr: str
    ) -> None:
        state = self._reviews.get(task_id)
        if state is None:
            return

        if code == 0:
            if not stdout.strip():
                await self._finalize(
                    task_id,
                    task,
                    outcome="approved",
                    comment_count=0,
                    close_session=True,
                )
                return
            # Comments: count `> `-blockquoted segments as a best-effort
            # display number; fall back to a generic label.
            comment_count = stdout.count("> ")
            iteration = ReviewIteration(
                outcome="comments",
                comment_count=comment_count if comment_count > 0 else None,
            )
            state.iterations.append(iteration)
            self._hub.publish(
                "review_iteration",
                {"task_id": task_id, "iteration": self._iteration_payload(iteration)},
            )
            # Comments loop back to the primary session (workflow-engine D-8).
            handle = self._agents.get(task_id, self._primary_session(task))
            if handle is None or handle.returncode is not None:
                await self._finalize(
                    task_id,
                    task,
                    outcome="error",
                    stderr="no live agent to receive review comments",
                    close_session=False,
                )
                return
            try:
                await handle.prompt(stdout)
            except (AgentGoneError, RequestFailedError) as exc:
                await self._finalize(
                    task_id,
                    task,
                    outcome="error",
                    stderr=f"failed to send review comments to agent: {exc}",
                    close_session=False,
                )
            return

        if code == 130:
            await self._finalize(
                task_id,
                task,
                outcome="aborted",
                close_session=True,
            )
            return

        await self._finalize(
            task_id,
            task,
            outcome="error",
            stderr=stderr if stderr.strip() else f"llmvet exited with code {code}",
            close_session=True,
        )

    async def _finalize(
        self,
        task_id: int,
        task: Task,
        *,
        outcome: str,
        comment_count: int | None = None,
        stderr: str | None = None,
        close_session: bool,
    ) -> None:
        state = self._reviews.get(task_id)
        if state is None:
            return
        iteration = ReviewIteration(
            outcome=outcome, comment_count=comment_count, stderr=stderr
        )
        state.iterations.append(iteration)
        state.status = outcome
        self._hub.publish(
            "review_iteration",
            {"task_id": task_id, "iteration": self._iteration_payload(iteration)},
        )
        self._hub.publish("review_finished", {"task_id": task_id, "status": outcome})
        if close_session:
            self._sessions.review_closed(
                task_id, self._primary_session(task), f"review {outcome}"
            )

    @staticmethod
    def _iteration_payload(iteration: ReviewIteration) -> dict[str, Any]:
        return {
            "outcome": iteration.outcome,
            "comment_count": iteration.comment_count,
            "stderr": iteration.stderr,
            "recorded_at": iteration.recorded_at,
        }

    async def _consume_events(self) -> None:
        """Watch for the reviewed (primary) session's exit while a review is
        open and tear down the review: exit always wins over review (design
        D-2). Other sessions of the task failing does not touch the review."""
        queue = self._hub.subscribe()
        try:
            while True:
                event = await queue.get()
                if event.type != "status_changed":
                    continue
                payload = event.payload
                if payload.get("to") != "failed":
                    continue
                task_id = payload.get("task_id")
                session = payload.get("session")
                if not isinstance(task_id, int) or not isinstance(session, str):
                    continue
                if task_id not in self._processes:
                    continue
                task = self._task(task_id)
                if task is None or session != self._primary_session(task):
                    continue
                logger.info(
                    "primary session for task %d failed while review was open; "
                    "cancelling review",
                    task_id,
                )
                with contextlib.suppress(ReviewError):
                    await self.cancel_review(task_id)
        finally:
            self._hub.unsubscribe(queue)

    def _task(self, task_id: int) -> Task | None:
        from ompire_daemon.registry.tasks import get_task

        try:
            return get_task(self._engine, task_id)
        except Exception:  # noqa: BLE001 — purged mid-review; nothing to cancel against
            return None

    def _pop_watcher(self, task_id: int, task: asyncio.Task) -> None:
        if self._watchers.get(task_id) is task:
            self._watchers.pop(task_id, None)
        if not task.cancelled() and task.exception() is not None:
            logger.error(
                "review watcher for task %d failed", task_id, exc_info=task.exception()
            )

    # --- startup crash-recovery helper --------------------------------------

    @staticmethod
    async def restore_parked_clone(clone_path: str, timeout: int) -> bool:
        """If `refs/ompire/review-orig` exists in the clone, reset to it and
        delete the ref. Returns True when a restore actually happened.
        """
        try:
            await _run_git_output(
                ["git", "-C", clone_path, "rev-parse", "--verify", REVIEW_GIT_REF],
                clone_path,
                timeout,
                "review-ref-check",
            )
        except ReviewError:
            return False
        await _run_step(
            Step(
                "review-startup-restore",
                ["git", "-C", clone_path, "reset", "--mixed", REVIEW_GIT_REF],
                timeout,
            )
        )
        await _run_step(
            Step(
                "review-startup-delete-ref",
                ["git", "-C", clone_path, "update-ref", "-d", REVIEW_GIT_REF],
                timeout,
            )
        )
        return True
