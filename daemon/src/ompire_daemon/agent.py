"""Supervised omp agents: `AgentHandle` wraps one `omp --mode rpc-ui` child,
`AgentSupervisor` maps task ids to live handles (design D-1).

Handles own the whole child lifecycle: spawn, ready handshake, request
correlation (via `rpc.RpcConnection`), event fan-out through a per-agent ring
buffer (design D-5), and exit watching (design D-6). Live handles are
in-memory only — the registry is untouched this chunk.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections import deque
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ompire_daemon import rpc
from ompire_daemon.config import Config
from ompire_daemon.events import Event, EventHub

if TYPE_CHECKING:
    from ompire_daemon.sessions import SessionTracker

logger = logging.getLogger(__name__)

_STDERR_CAPTURE_LIMIT = 200  # lines kept for start-failure reporting
_ASK_TIMEOUT_CHECK_TIMEOUT = 30

# Queued to event subscribers after the exit flush: no more events will come.
EVENT_STREAM_END = None


class AgentStartError(Exception):
    """The child could not be started (spawn error, pre-ready exit, timeout)."""

    def __init__(self, message: str, stderr: str = "") -> None:
        super().__init__(message)
        self.stderr = stderr


class AgentAlreadyRunningError(Exception):
    def __init__(self, task_id: int, session: str) -> None:
        super().__init__(f"task {task_id} session {session!r} already has a live agent")
        self.task_id = task_id
        self.session = session


class NoLiveAgentError(Exception):
    def __init__(self, task_id: int, session: str) -> None:
        super().__init__(f"task {task_id} session {session!r} has no live agent")
        self.task_id = task_id
        self.session = session


def build_agent_argv(
    clone_path: str,
    agent_env: Mapping[str, str],
    *,
    resume: str | None = None,
    model: str | None = None,
    thinking: str | None = None,
) -> list[str]:
    """The spike's spawn recipe (design D-2): sessions ON (no `--no-session`),
    no `-s` flag (nonexistent), credentials via an `env` prefix inside the
    container (design D-3). `resume` appends `--resume <session-id>`
    (crash-recovery capability, design D-1/D-3) — a bare session id, not a
    file path, confirmed against the omp source (see the
    `omp-rpc-field-assumptions` memory note). `model`/`thinking` append
    `--model`/`--thinking` only when set (templates capability; both flags
    verified against omp v17.2.12); unset means omp's defaults."""
    argv = [
        "workshop", "exec", "-p", clone_path, "--",
        "env", *[f"{key}={value}" for key, value in agent_env.items()],
        "omp", "--mode", "rpc-ui", "--no-title",
    ]
    if model is not None:
        argv += ["--model", model]
    if thinking is not None:
        argv += ["--thinking", thinking]
    if resume is not None:
        argv += ["--resume", resume]
    return argv


async def verify_ask_timeout(clone_path: str) -> None:
    """Fail loudly unless the container's omp config has `ask.timeout` 0.

    The spike found `-s ask.timeout=0` doesn't exist and the default is
    already 0; this assertion catches a future omp changing that default,
    which would leave agents blocked on interactive asks (design D-2).
    """
    try:
        process = await asyncio.create_subprocess_exec(
            "workshop", "exec", "-p", clone_path, "--",
            "omp", "config", "get", "ask.timeout",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise AgentStartError(f"cannot exec 'workshop': {exc}") from exc
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=_ASK_TIMEOUT_CHECK_TIMEOUT
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        raise AgentStartError(
            f"'omp config get ask.timeout' timed out after {_ASK_TIMEOUT_CHECK_TIMEOUT}s"
        ) from None
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    if process.returncode != 0:
        raise AgentStartError("cannot read ask.timeout from the container's omp config", stderr)
    # Tolerate both bare `0` and `ask.timeout = 0` output shapes.
    tokens = stdout_bytes.decode("utf-8", errors="replace").strip().split()
    value = tokens[-1] if tokens else ""
    if value != "0":
        raise AgentStartError(
            f"ask.timeout is {value!r} in the container's omp config, expected '0'; "
            "agents would block on interactive asks"
        )


class AgentHandle:
    """One supervised omp child: handshake, requests, event fan-out, exit."""

    def __init__(self, process: asyncio.subprocess.Process, ring_buffer_size: int) -> None:
        self._process = process
        self.events: deque[Event] = deque(maxlen=ring_buffer_size)
        self._subscribers: set[asyncio.Queue] = set()
        self._stderr_capture: deque[str] = deque(maxlen=_STDERR_CAPTURE_LIMIT)
        self._exited: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        assert process.stdout is not None
        assert process.stdin is not None
        self._conn = rpc.RpcConnection(process.stdout, process.stdin, self._publish_frame)
        self._stderr_task = asyncio.create_task(self._read_stderr())
        self._exit_watcher = asyncio.create_task(self._watch_exit())

    @classmethod
    async def start(
        cls,
        argv: list[str],
        *,
        env: Mapping[str, str] | None = None,
        ready_timeout: float,
        ring_buffer_size: int,
    ) -> AgentHandle:
        """Spawn and complete the ready handshake; on failure the child is
        dead and the captured stderr rides on the raised AgentStartError."""
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=rpc.STREAM_LIMIT,
                env={**os.environ, **env} if env else None,
            )
        except OSError as exc:
            raise AgentStartError(f"cannot exec {argv[0]!r}: {exc}") from exc
        handle = cls(process, ring_buffer_size)
        await handle._await_ready(ready_timeout)
        return handle

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    async def prompt(self, message: str) -> dict[str, Any]:
        return await self._conn.prompt(message)

    async def request(self, request_type: str, **fields: Any) -> dict[str, Any]:
        return await self._conn.request(request_type, **fields)

    async def read_session_id(self) -> str | None:
        """Capture the omp session id via `get_state` (crash-recovery
        capability, design D-2): best-effort — returns None without raising
        on any failure, so a capture miss never fails the caller. `sessionId`
        confirmed against the omp source, not a fake/guessed field (see the
        `omp-rpc-field-assumptions` memory note)."""
        try:
            response = await self.request("get_state")
        except Exception as exc:  # noqa: BLE001 — capture must never break the caller
            logger.warning("session id capture failed: get_state request failed: %s", exc)
            return None
        data = response.get("data")
        data = data if isinstance(data, dict) else {}
        session_id = data.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            logger.warning("session id capture failed: no sessionId in get_state response")
            return None
        return session_id

    async def respond_ui_request(self, request_id: str, payload: dict[str, Any]) -> None:
        """Reply to an agent-raised `extension_ui_request` (design D-5): this
        replies to the *agent's* request id, the reverse direction of
        `request()`, so there is no daemon-generated id or pending future to
        correlate — the frame is written and the turn simply continues. Frame
        shape (`extension_ui_response`, `id`, and `value`/`confirmed`/
        `cancelled` payload variants) confirmed against the omp source
        (`rpc-types.ts`) during dogfooding 2026-07-20; see the
        `omp-rpc-field-assumptions` memory note."""
        await self._conn.write_frame({"type": "extension_ui_response", "id": request_id, **payload})

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.add(queue)
        if self._exited.done():
            queue.put_nowait(EVENT_STREAM_END)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def snapshot(self) -> list[Event]:
        """The ring buffer's current contents, oldest first."""
        return list(self.events)

    async def kill(self) -> None:
        """Kill the child (idempotent) and wait for the exit flush."""
        if self._process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._process.kill()
        await self.wait_exited()

    async def terminate(self, grace: float) -> None:
        """Graceful stop (crash-recovery capability, design D-6): SIGTERM, a
        bounded wait, then SIGKILL as a fallback via `kill()` — reuses the
        same exit-flush wait either way. Idempotent, like `kill()`. SIGTERM
        is expected to let container-side `omp` flush its session file
        (confirmed against the omp source's teardown handlers, and signal
        propagation through `workshop exec` confirmed live — see the
        `omp-rpc-field-assumptions` memory note)."""
        if self._process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._process.terminate()
            try:
                await asyncio.wait_for(asyncio.shield(self._exited), timeout=grace)
                return
            except TimeoutError:
                pass
        await self.kill()

    async def wait_exited(self) -> int:
        """Block until the child has exited and both pipes are flushed."""
        return await asyncio.shield(self._exited)

    def _publish_frame(self, frame: dict[str, Any]) -> None:
        frame_type = frame.get("type")
        event_type = frame_type if isinstance(frame_type, str) else "unknown"
        self._fan_out(Event(type=event_type, payload=frame))

    def _fan_out(self, event: Event) -> None:
        self.events.append(event)
        for queue in self._subscribers:
            queue.put_nowait(event)

    async def _read_stderr(self) -> None:
        stderr = self._process.stderr
        assert stderr is not None
        while True:
            try:
                line = await stderr.readline()
            except ValueError:
                logger.warning("agent stderr line exceeded stream limit; dropped")
                continue
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip("\n")
            # Diagnostic gold on crashes (design D-5): kept for start-failure
            # reporting and wrapped as an event on the channel.
            self._stderr_capture.append(text)
            self._fan_out(Event(type="agent_stderr", payload={"line": text}))

    async def _watch_exit(self) -> None:
        code = await self._process.wait()
        # Drain both pipes to EOF so every event is flushed before the
        # channel-close sentinel goes out (design D-6).
        await self._conn.wait_closed()
        with contextlib.suppress(Exception):
            await self._stderr_task
        for queue in self._subscribers:
            queue.put_nowait(EVENT_STREAM_END)
        self._exited.set_result(code)

    async def _await_ready(self, timeout: float) -> None:
        futures: set[asyncio.Future[Any]] = {self._conn.ready, self._exited}
        done, _ = await asyncio.wait(
            futures,
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        ready = self._conn.ready
        if ready in done and not ready.cancelled() and ready.exception() is None:
            return
        # A ready future that completed with an exception means stdout hit
        # EOF before the ready frame (rpc sets AgentGoneError): the child is
        # gone even if the exit watcher hasn't populated _exited/returncode
        # yet. Check this first, or a dead child is misreported as a timeout.
        ready_failed = ready in done and not ready.cancelled()
        child_died_first = (
            ready_failed or self._exited in done or self._process.returncode is not None
        )
        await self.kill()
        # Consume/cancel the ready future so its AgentGoneError is never
        # reported as an unretrieved exception.
        if ready.done():
            if not ready.cancelled():
                ready.exception()
        else:
            ready.cancel()
        stderr = "\n".join(self._stderr_capture)
        if child_died_first:
            code = self._exited.result()  # kill() waited for the exit flush
            raise AgentStartError(f"agent exited before ready (exit code {code})", stderr)
        raise AgentStartError(f"no ready frame within {timeout}s", stderr)


class AgentSupervisor:
    """(Task id, session name) → live AgentHandle (workflow-engine design
    D-1); in-memory only — session identity persists via the `task_sessions`
    registry rows written by the workflow engine on lazy spawn."""

    def __init__(
        self, config: Config, hub: EventHub, tracker: SessionTracker | None = None
    ) -> None:
        self._config = config
        self._hub = hub
        self._tracker = tracker
        self._handles: dict[tuple[int, str], AgentHandle] = {}
        self._waiters: set[asyncio.Task] = set()
        self._ask_timeout_verified: set[int] = set()
        # Set once by `shutdown()` (crash-recovery capability, design D-6):
        # tells the exit watcher these exits are graceful, not crashes.
        self._shutting_down = False

    def get(self, task_id: int, session: str) -> AgentHandle | None:
        return self._handles.get((task_id, session))

    async def start(
        self,
        task_id: int,
        session: str,
        clone_path: str,
        *,
        resume: str | None = None,
        model: str | None = None,
        thinking: str | None = None,
    ) -> AgentHandle:
        key = (task_id, session)
        if key in self._handles:
            raise AgentAlreadyRunningError(task_id, session)
        if task_id not in self._ask_timeout_verified:
            await verify_ask_timeout(clone_path)
            self._ask_timeout_verified.add(task_id)
        argv = build_agent_argv(
            clone_path, self._config.agent_env, resume=resume, model=model, thinking=thinking
        )
        if self._tracker is not None and resume is None:
            # `starting` covers the spawn and ready handshake (design D-2). A
            # resumed start is already seeded `starting` with a recovery
            # reason by the caller (crash-recovery design D-4) — don't
            # clobber it with this generic one.
            self._tracker.agent_spawning(task_id, session)
        handle = await AgentHandle.start(
            argv,
            ready_timeout=self._config.agent_ready_timeout,
            ring_buffer_size=self._config.agent_ring_buffer_size,
        )
        if key in self._handles:
            # A concurrent start won the race while this one awaited spawn.
            await handle.kill()
            raise AgentAlreadyRunningError(task_id, session)
        self._handles[key] = handle
        if self._tracker is not None:
            self._tracker.watch(task_id, session, handle)
        waiter = asyncio.create_task(self._watch(task_id, session, handle))
        self._waiters.add(waiter)
        waiter.add_done_callback(self._waiters.discard)
        return handle

    async def stop(self, task_id: int, session: str) -> None:
        handle = self._handles.get((task_id, session))
        if handle is None:
            raise NoLiveAgentError(task_id, session)
        await handle.kill()

    async def shutdown(self) -> None:
        """Terminate every live agent gracefully on daemon shutdown
        (crash-recovery capability, design D-6): sets the shutting-down flag
        first so the exit watcher skips the `agent_exited` -> `failed`
        tracker call and event for these exits. Registry state is never
        written on agent exit (only in-memory session status is), so the
        tasks stay `created` and are recovered on the next startup."""
        self._shutting_down = True
        handles = list(self._handles.values())
        await asyncio.gather(
            *(handle.terminate(self._config.shutdown_grace) for handle in handles),
            return_exceptions=True,
        )

    async def _watch(self, task_id: int, session: str, handle: AgentHandle) -> None:
        code = await handle.wait_exited()
        if self._handles.get((task_id, session)) is handle:
            del self._handles[(task_id, session)]
        if self._shutting_down:
            # A graceful-shutdown exit is not a crash (design D-6): no
            # tracker call, no event — the task stays `created` for the next
            # startup's recovery pass.
            return
        # Interpretation first (session goes `failed`), then the raw fact.
        if self._tracker is not None:
            self._tracker.agent_exited(task_id, session, code)
        self._hub.publish(
            "agent_exited", {"task_id": task_id, "session": session, "exit_code": code}
        )
