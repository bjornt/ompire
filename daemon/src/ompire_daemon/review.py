"""Daemon-run host-side review authority.

Architecture: ADR-0011
(docs/adr/0011-keep-review-and-publishing-authority-outside-agent-sandbox.md)

`ReviewManager` owns candidate capture, the supervised llmvet subprocess, exit
interpretation, and the comment loopback to the live agent. It is the process
supervisor, not the record: review status and the ordered iteration history
are durable rows behind `registry/reviews.py` (ADR-0016's review slice), and
every transition is written there before it is broadcast.

Reviews are content-bound (ADR-0032). Starting one captures a protected
candidate — the task's whole publishable delta against its accepted base — and
llmvet reads an isolated checkout of *that*, not the task's live tree. The
approval therefore says what was approved, and an agent that keeps working
cannot change what is under review; it can only make its own approval unusable
for delivery, which is a visible refusal rather than a silent substitution.

Two things stay deliberately in memory, because they describe a process that
cannot outlive the daemon: the reviewer's URL and port. A restored review
therefore reports neither, and the UI offers no external link for it. The git
ref `refs/ompire/review-orig` written by the superseded reset dance is still
recognized and restored on startup, for clones parked by an older daemon.
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
from ompire_daemon.delivery import (
    DeliveryWorkspaceError,
    WorkspaceGuard,
    capture_candidate,
    prepare_review_view,
    remove_review_view,
)
from ompire_daemon.events import EventHub
from ompire_daemon.registry.reviews import (
    ReviewIterationRecord,
    append_iteration,
    clear_process_marker,
    get_review,
    list_interrupted_candidates,
    list_reviews,
    open_review,
)
from ompire_daemon.registry.ships import CandidateRecord
from ompire_daemon.registry.tasks import Task, require_task_inputs
from ompire_daemon.rpc import AgentGoneError, RequestFailedError
from ompire_daemon.spawn import Step, _run_step

if TYPE_CHECKING:

    from ompire_daemon.agent import AgentSupervisor
    from ompire_daemon.sessions import SessionTracker

logger = logging.getLogger(__name__)

REVIEW_GIT_REF = "refs/ompire/review-orig"


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


class ReviewContentError(ReviewError):
    """There is nothing safe to review: an empty delta, an unreachable base, or
    a clone configured in a way the daemon refuses to capture under. A refusal
    about the workspace, not a reviewer failure."""


class ReviewAlreadyOpenError(ReviewError):
    def __init__(self, task_id: int) -> None:
        super().__init__(f"task {task_id} already has an open review")
        self.task_id = task_id


@dataclass
class ReviewIteration:
    # approved | comments | aborted | error | interrupted
    outcome: str
    comment_count: int | None = None
    stderr: str | None = None
    candidate_id: str | None = None
    recorded_at: str = field(default_factory=_now_iso)


@dataclass
class ReviewState:
    """Composed read model: durable status and history from the registry,
    plus the live process's URL and port when one is running. Both are None
    for a review restored across a restart."""

    status: str  # open | approved | aborted | error
    url: str | None
    port: int | None
    candidate_id: str | None = None
    iterations: list[ReviewIteration] = field(default_factory=list)


class ReviewManager:
    def __init__(
        self,
        config: Config,
        engine: Engine,
        hub: EventHub,
        sessions: SessionTracker,
        agents: AgentSupervisor,
        guard: WorkspaceGuard,
    ) -> None:
        self._config = config
        self._engine = engine
        self._hub = hub
        self._sessions = sessions
        self._agents = agents
        self._guard = guard
        # Runtime only: the isolated checkout each open review is reading.
        self._views: dict[int, str] = {}
        # Runtime only: {task_id: (url, port)} for a live reviewer process.
        # Status and iterations live in the registry.
        self._runtime: dict[int, tuple[str, int]] = {}
        self._processes: dict[int, asyncio.subprocess.Process] = {}
        self._watchers: dict[int, asyncio.Task] = {}
        self._port_lock = asyncio.Lock()
        self._event_task: asyncio.Task | None = None

    def _primary_session(self, task: Task) -> str:
        """Review attaches to the primary session *this task's pinned
        definition* declares (workflow-engine design D-8, ADR-0028).

        Resolved through the task's own revision, never through the catalog's
        current definition of the same name: reviewing whatever "the primary
        session" means today would attach the reviewer to a conversation this
        task may never have had.
        """
        from ompire_daemon.taskdefinition import task_primary_session

        return task_primary_session(self._engine, task)

    def start(self) -> None:
        """Start the hub event consumer; idempotent. Must be called from a
        running event loop (app lifespan)."""
        if self._event_task is None:
            self._event_task = asyncio.create_task(self._consume_events())

    def snapshot(self) -> dict[int, dict[str, Any]]:
        """Current reviews for the WebSocket snapshot (design D-6), composed
        from durable history plus any live process's URL/port. A reconnect
        after a restart therefore serves the restored history, and tasks with
        no review are absent from the map."""
        payload: dict[int, dict[str, Any]] = {}
        for record in list_reviews(self._engine):
            url, port = self._runtime.get(record.task_id, (None, None))
            payload[record.task_id] = {
                "status": record.status,
                "url": url,
                "port": port,
                "candidate_id": record.candidate_id,
                "iterations": [
                    {
                        "outcome": it.outcome,
                        "comment_count": it.comment_count,
                        "stderr": it.stderr,
                        "candidate_id": it.candidate_id,
                        "recorded_at": it.recorded_at,
                    }
                    for it in record.iterations
                ],
            }
        return payload

    def get(self, task_id: int) -> ReviewState | None:
        record = get_review(self._engine, task_id)
        if record is None:
            return None
        url, port = self._runtime.get(task_id, (None, None))
        return ReviewState(
            status=record.status,
            url=url,
            port=port,
            candidate_id=record.candidate_id,
            iterations=[
                ReviewIteration(
                    outcome=it.outcome,
                    comment_count=it.comment_count,
                    stderr=it.stderr,
                    candidate_id=it.candidate_id,
                    recorded_at=it.recorded_at,
                )
                for it in record.iterations
            ],
        )

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
        """`<base>` for the reset dance: the base branch the task was
        accepted with (ADR-0026).

        There is no `main` fallback. A task without confirmed inputs never
        reaches here — the readiness guard refuses review before this runs —
        because resetting the wrong base is a silent way to review the wrong
        diff."""
        return require_task_inputs(task).workspace.base_branch

    # --- public lifecycle ---------------------------------------------------

    async def start_review(self, task: Task) -> ReviewState:
        """Capture what this task would publish, then review exactly that.

        The workspace guard is taken before the capture and held by the
        reviewer process, so nothing daemon-managed can write to the task while
        its candidate is being resolved. Ownership is explicit rather than
        scoped to this coroutine because the reviewer outlives the call; the
        watcher releases it.
        """
        task_id = task.id
        if task_id in self._processes:
            raise ReviewAlreadyOpenError(task_id)

        base_branch = self._base_branch(task)
        self._guard.acquire(task_id, "review")
        try:
            candidate = await capture_candidate(
                self._config, self._engine, task, base_branch=base_branch
            )
            view = await prepare_review_view(self._config, task_id, candidate)
            port = await self._allocate_port()
        except DeliveryWorkspaceError as exc:
            # A capture refusal is a review refusal, and says which content
            # problem stopped it rather than failing as an opaque 500.
            self._guard.release(task_id, "review")
            raise ReviewContentError(str(exc)) from exc
        except Exception:
            self._guard.release(task_id, "review")
            raise

        url = f"http://127.0.0.1:{port}"
        # Durable first: `open_review` upserts the row (re-review after
        # comments appends to the same history), binds it to the candidate it
        # is grading, and stamps the write-ahead process marker, so a crash
        # between here and the first frame is recoverable as an interrupted
        # review rather than a lost one.
        open_review(self._engine, task_id, candidate_id=candidate.candidate_id)
        self._runtime[task_id] = (url, port)
        self._views[task_id] = str(view)
        state = self.get(task_id)
        assert state is not None

        primary = self._primary_session(task)
        self._sessions.review_opened(task_id, primary, f"llmvet review on {url}")
        self._hub.publish(
            "review_started",
            {
                "task_id": task_id,
                "url": url,
                "port": port,
                "candidate_id": candidate.candidate_id,
            },
        )

        watcher = asyncio.create_task(
            self._watch_review(task_id, task, port, str(view), candidate)
        )
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
        state = self.get(task_id)
        if state is None:
            raise ReviewError(f"task {task_id} has no review state")
        return state

    async def cancel_and_drop(self, task_id: int) -> None:
        """Cancel an open review if one exists, then drop its runtime state.

        The cleanup path. The durable history is deliberately retained: a
        shipped, cleaned-up task keeps the review evidence explaining why it
        was allowed to publish (`VISION.md` principle 4, ADR-0016). Only
        purge deletes it.
        """
        if task_id in self._processes:
            with contextlib.suppress(ReviewError):
                await self.cancel_review(task_id)
        self.drop_review(task_id)
        # `drop_review` cancelled the watcher, so nothing else will record
        # the cancelled reviewer's outcome. Land it here instead: a retained
        # row left `open` with no process would show an archived task as
        # still under review. An uncleared marker is exactly the "a process
        # was running and its exit was never recorded" case, so this cannot
        # double-record an outcome the watcher already wrote.
        record = get_review(self._engine, task_id)
        if (
            record is not None
            and record.status == "open"
            and record.process_started_at is not None
        ):
            iteration = append_iteration(
                self._engine,
                task_id,
                outcome="aborted",
                status="aborted",
                candidate_id=record.candidate_id,
            )
            self._hub.publish(
                "review_iteration",
                {"task_id": task_id, "iteration": self._iteration_payload(iteration)},
            )
            self._hub.publish(
                "review_finished", {"task_id": task_id, "status": "aborted"}
            )
        # The clone is about to be deleted, so no process can be running and
        # no restart may later read this review as interrupted.
        clear_process_marker(self._engine, task_id)

    def drop_review(self, task_id: int) -> None:
        """Drop the review's runtime state — watcher, process handle, URL/port,
        the isolated candidate view, and the workspace hold. Rows are untouched
        here: cleanup retains them and `purge_task` deletes them. The task clone
        needs no restoration — the reviewer never wrote to it."""
        watcher = self._watchers.pop(task_id, None)
        if watcher is not None:
            watcher.cancel()
        self._processes.pop(task_id, None)
        self._runtime.pop(task_id, None)
        view = self._views.pop(task_id, None)
        if view is not None:
            remove_review_view(view)
        self._guard.release(task_id, "review")

    # --- internals ----------------------------------------------------------

    async def _watch_review(
        self,
        task_id: int,
        task: Task,
        port: int,
        view_path: str,
        candidate: CandidateRecord,
    ) -> None:
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
            # The reviewer runs in the isolated candidate view, never in the
            # task clone: what it reads is the content the approval will name.
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=view_path,
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
                candidate_id=candidate.candidate_id,
                stderr=f"failed to launch llmvet: {exc}",
                close_session=True,
            )
            return
        finally:
            self._processes.pop(task_id, None)
            # The process was observed exiting: drop its URL/port and clear
            # the write-ahead marker, so a later startup does not read this
            # review as interrupted. The isolated view goes with it — the
            # candidate's own protected store keeps the reviewed objects.
            self._runtime.pop(task_id, None)
            self._views.pop(task_id, None)
            clear_process_marker(self._engine, task_id)
            await asyncio.to_thread(remove_review_view, view_path)

        assert process is not None
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        code = process.returncode
        assert code is not None
        try:
            await self._interpret_exit(
                task_id, task, code, stdout, stderr, candidate.candidate_id
            )
        finally:
            self._guard.release(task_id, "review")

    async def _interpret_exit(
        self,
        task_id: int,
        task: Task,
        code: int,
        stdout: str,
        stderr: str,
        candidate_id: str | None = None,
    ) -> None:
        if get_review(self._engine, task_id) is None:
            return

        if code == 0:
            if not stdout.strip():
                await self._finalize(
                    task_id,
                    task,
                    outcome="approved",
                    comment_count=0,
                    candidate_id=candidate_id,
                    close_session=True,
                )
                return
            # Comments: count `> `-blockquoted segments as a best-effort
            # display number; fall back to a generic label.
            comment_count = stdout.count("> ")
            # Durable before broadcast. The review stays `open` — its comments
            # are with the agent — but its process marker is already cleared,
            # so a restart restores this as comments rather than interrupted.
            record = append_iteration(
                self._engine,
                task_id,
                outcome="comments",
                comment_count=comment_count if comment_count > 0 else None,
                candidate_id=candidate_id,
            )
            self._hub.publish(
                "review_iteration",
                {"task_id": task_id, "iteration": self._iteration_payload(record)},
            )
            # Comments loop back to the primary session (workflow-engine D-8),
            # on whatever model policy that session last applied (ADR-0027) —
            # taken inside the session boundary so a concurrent policy handoff
            # cannot hand back a child it is already retiring.
            # Ownership goes back before the agent is prompted. The reviewer
            # is finished with the workspace, and the correction turn has to be
            # admitted on its own — inheriting the reviewer's hold would let it
            # write during a review, and holding it would deadlock the handoff.
            with self._guard.released(task_id, "review"):
                handle = await self._agents.acquire(
                    task_id, self._primary_session(task)
                )
                if handle is None:
                    await self._finalize(
                        task_id,
                        task,
                        outcome="error",
                        candidate_id=candidate_id,
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
                        candidate_id=candidate_id,
                        stderr=f"failed to send review comments to agent: {exc}",
                        close_session=False,
                    )
            return

        if code == 130:
            await self._finalize(
                task_id,
                task,
                outcome="aborted",
                candidate_id=candidate_id,
                close_session=True,
            )
            return

        await self._finalize(
            task_id,
            task,
            outcome="error",
            candidate_id=candidate_id,
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
        candidate_id: str | None = None,
        close_session: bool,
    ) -> None:
        if get_review(self._engine, task_id) is None:
            return
        # One transaction for the terminal iteration and the status it
        # produced, written before either is broadcast. The iteration names
        # the candidate it graded, which is what makes an approval usable —
        # or, once the workspace moves on, visibly stale.
        record = append_iteration(
            self._engine,
            task_id,
            outcome=outcome,
            comment_count=comment_count,
            stderr=stderr,
            status=outcome,
            candidate_id=candidate_id,
        )
        self._hub.publish(
            "review_iteration",
            {"task_id": task_id, "iteration": self._iteration_payload(record)},
        )
        self._hub.publish("review_finished", {"task_id": task_id, "status": outcome})
        if close_session:
            self._sessions.review_closed(
                task_id, self._primary_session(task), f"review {outcome}"
            )

    @staticmethod
    def _iteration_payload(iteration: ReviewIteration | ReviewIterationRecord) -> dict[str, Any]:
        return {
            "outcome": iteration.outcome,
            "comment_count": iteration.comment_count,
            "stderr": iteration.stderr,
            "candidate_id": iteration.candidate_id,
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

    # --- startup crash-recovery helpers -------------------------------------

    @staticmethod
    async def restore_parked_clone(clone_path: str, timeout: int) -> str:
        """Restore a clone parked by the superseded in-clone review.

        Returns `absent`, `restored`, or `unsafe`, for the same reason the
        delivery equivalent does: a surviving ref Ompire could not honour is a
        reason to stop working on that task, and a boolean cannot tell it apart
        from having nothing to restore.

        Reviews no longer park the task clone at all — the reviewer reads an
        isolated checkout — so this only ever meets clones left by an older
        daemon.
        """
        try:
            parked = (
                await _run_git_output(
                    ["git", "-C", clone_path, "rev-parse", "--verify", REVIEW_GIT_REF],
                    clone_path,
                    timeout,
                    "review-ref-check",
                )
            ).strip()
        except ReviewError:
            return "absent"
        try:
            await _run_step(
                Step(
                    "review-startup-restore",
                    ["git", "-C", clone_path, "reset", "--mixed", REVIEW_GIT_REF],
                    timeout,
                )
            )
            head = (
                await _run_git_output(
                    ["git", "-C", clone_path, "rev-parse", "HEAD"],
                    clone_path,
                    timeout,
                    "review-startup-verify",
                )
            ).strip()
        except Exception:  # noqa: BLE001 — a bad clone must not stop startup
            logger.warning(
                "clone %s carries a legacy review-orig ref that could not be "
                "restored; leaving it in place",
                clone_path,
            )
            return "unsafe"
        if head != parked:
            logger.warning(
                "clone %s did not restore to its parked head; leaving the "
                "legacy review ref in place",
                clone_path,
            )
            return "unsafe"
        await _run_step(
            Step(
                "review-startup-delete-ref",
                ["git", "-C", clone_path, "update-ref", "-d", REVIEW_GIT_REF],
                timeout,
            )
        )
        return "restored"



def restore_reviews(engine: Engine) -> list[int]:
    """Close out reviews whose llmvet process died with the daemon.

    A review persisted `open` with an uncleared write-ahead process marker
    had a reviewer running when the daemon stopped. That process cannot be
    adopted and is never relaunched on the operator's behalf, so the review
    is closed honestly: an `interrupted` iteration is appended and the review
    lands `aborted`, leaving the recovered primary session free to start a
    fresh review that appends to the same history.

    A review left `open` because its comments went back to the agent has a
    cleared marker and is restored exactly as persisted. Returns the task ids
    that were interrupted.

    Must run before the first WebSocket snapshot is served, so a client never
    sees an open review the daemon is about to correct.
    """
    interrupted: list[int] = []
    for record in list_interrupted_candidates(engine):
        append_iteration(
            engine,
            record.task_id,
            outcome="interrupted",
            status="aborted",
        )
        clear_process_marker(engine, record.task_id)
        interrupted.append(record.task_id)
        logger.info(
            "review for task %d was interrupted by a daemon restart; recorded aborted",
            record.task_id,
        )
    return interrupted
