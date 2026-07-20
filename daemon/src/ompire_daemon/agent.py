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
    def __init__(self, task_id: int) -> None:
        super().__init__(f"task {task_id} already has a live agent")
        self.task_id = task_id


class NoLiveAgentError(Exception):
    def __init__(self, task_id: int) -> None:
        super().__init__(f"task {task_id} has no live agent")
        self.task_id = task_id


def build_agent_argv(clone_path: str, agent_env: Mapping[str, str]) -> list[str]:
    """The spike's spawn recipe (design D-2): sessions ON (no `--no-session`),
    no `-s` flag (nonexistent), credentials via an `env` prefix inside the
    container (design D-3)."""
    return [
        "workshop", "exec", "-p", clone_path, "--",
        "env", *[f"{key}={value}" for key, value in agent_env.items()],
        "omp", "--mode", "rpc-ui", "--no-title",
    ]


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
        while True:
            try:
                line = await self._process.stderr.readline()
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
        done, _ = await asyncio.wait(
            {self._conn.ready, self._exited},
            timeout=timeout,
            return_when=asyncio.FIRST_COMPLETED,
        )
        ready = self._conn.ready
        if ready in done and not ready.cancelled() and ready.exception() is None:
            return
        child_died_first = self._exited in done or self._process.returncode is not None
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
    """Task id → live AgentHandle (design D-1); in-memory only, no registry
    writes — persistence questions belong to later chunks."""

    def __init__(
        self, config: Config, hub: EventHub, tracker: SessionTracker | None = None
    ) -> None:
        self._config = config
        self._hub = hub
        self._tracker = tracker
        self._handles: dict[int, AgentHandle] = {}
        self._waiters: set[asyncio.Task] = set()
        self._ask_timeout_verified: set[int] = set()

    def get(self, task_id: int) -> AgentHandle | None:
        return self._handles.get(task_id)

    async def start(self, task_id: int, clone_path: str) -> AgentHandle:
        if task_id in self._handles:
            raise AgentAlreadyRunningError(task_id)
        if task_id not in self._ask_timeout_verified:
            await verify_ask_timeout(clone_path)
            self._ask_timeout_verified.add(task_id)
        argv = build_agent_argv(clone_path, self._config.agent_env)
        if self._tracker is not None:
            # `starting` covers the spawn and ready handshake (design D-2).
            self._tracker.agent_spawning(task_id)
        handle = await AgentHandle.start(
            argv,
            ready_timeout=self._config.agent_ready_timeout,
            ring_buffer_size=self._config.agent_ring_buffer_size,
        )
        if task_id in self._handles:
            # A concurrent start won the race while this one awaited spawn.
            await handle.kill()
            raise AgentAlreadyRunningError(task_id)
        self._handles[task_id] = handle
        if self._tracker is not None:
            self._tracker.watch(task_id, handle)
        waiter = asyncio.create_task(self._watch(task_id, handle))
        self._waiters.add(waiter)
        waiter.add_done_callback(self._waiters.discard)
        return handle

    async def stop(self, task_id: int) -> None:
        handle = self._handles.get(task_id)
        if handle is None:
            raise NoLiveAgentError(task_id)
        await handle.kill()

    async def _watch(self, task_id: int, handle: AgentHandle) -> None:
        code = await handle.wait_exited()
        if self._handles.get(task_id) is handle:
            del self._handles[task_id]
        # Interpretation first (session goes `failed`), then the raw fact.
        if self._tracker is not None:
            self._tracker.agent_exited(task_id, code)
        self._hub.publish("agent_exited", {"task_id": task_id, "exit_code": code})
