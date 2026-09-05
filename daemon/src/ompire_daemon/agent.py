"""Supervised omp agents: `AgentHandle` wraps one `omp --mode rpc-ui` child,
`AgentSupervisor` maps task ids to live handles (design D-1).

Architecture: ADR-0007 (docs/adr/0007-use-native-omp-rpc.md)

Handles own the whole child lifecycle: spawn, ready handshake, request
correlation (via `rpc.RpcConnection`), event fan-out through a per-agent ring
buffer (design D-5), and exit watching (design D-6). Live handles are
in-memory only — the registry is untouched this chunk.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ompire_daemon import rpc
from ompire_daemon.config import Config
from ompire_daemon.events import Event, EventHub
from ompire_daemon.execution_inputs import ModelPolicy, split_model_identifier
from ompire_daemon.registry.model_profiles import RoleBinding

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


class ModelConfigurationError(Exception):
    """The child did not end up running the accepted model policy.

    Raised instead of prompting: a turn sent under a substituted model or a
    dropped thinking policy is not the run the operator reviewed, and omp
    resolves `--model` fuzzily, so "it started" is not proof it obeyed
    (native probe, v18.1.10). The caller kills the child and fails the step.
    """


@dataclass(frozen=True)
class NativeModelState:
    """What the child reports it is actually running.

    `thinking_level` is omp's *resolved* level, which legitimately differs
    from the accepted policy: `max` resolves per model (observed `xhigh` on
    `anthropic/claude-sonnet-4-5`) and `auto` resolves to a concrete level
    (observed `high`). Keeping both means the UI can show the policy the
    operator chose next to the level the model is using, instead of
    presenting normalization as a lost override.
    """

    model: str  # provider-qualified, as the child reports it
    thinking_level: str | None


def role_flag_value(binding: RoleBinding) -> str:
    """`provider/model-id:LEVEL` — the native encoding the auxiliary role
    flags take (one argument each, verified against omp v18.1.10)."""
    return f"{binding.model}:{binding.thinking}"


def build_agent_argv(
    clone_path: str,
    *,
    policy: ModelPolicy,
    resume: str | None = None,
) -> list[str]:
    """The spike's spawn recipe (design D-2): sessions ON (no `--no-session`),
    no `-s` flag (nonexistent), and no environment-injection prefix
    (ADR-0015). `resume` appends `--resume <session-id>`
    (crash-recovery capability, design D-1/D-3) — a bare session id, not a
    file path, confirmed against the omp source (see the
    `omp-rpc-field-assumptions` memory note).

    The model policy is not optional (ADR-0026). Every process — a fresh
    session, a lazily spawned one, the judge, a resumed one — carries the
    task's accepted active pair *and* all three auxiliary role pairs, each
    with its own thinking level. There is no "unset means omp's default"
    any more: inheriting the host's model settings is exactly what a global
    profile exists to prevent. Flags and their one-argument
    `provider/model-id:LEVEL` encoding verified against omp v18.1.10.
    """
    argv = [
        "workshop", "exec", "-p", clone_path, "--",
        "omp", "--mode", "rpc-ui", "--no-title",
        "--model", policy.active.model,
        "--thinking", policy.active.thinking,
        "--smol", role_flag_value(policy.smol),
        "--slow", role_flag_value(policy.slow),
        "--plan", role_flag_value(policy.plan),
    ]
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
        # The accepted policy this child was started and verified under, set
        # by the supervisor once the model handshake succeeds. Lives on the
        # handle rather than in the engine so it survives a restart that
        # rebuilds the runner over already-resumed sessions.
        self.policy: ModelPolicy | None = None
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
        ready_timeout: float,
        ring_buffer_size: int,
    ) -> AgentHandle:
        """Spawn and complete the ready handshake; on failure the child is
        dead and the captured stderr rides on the raised AgentStartError.

        The launcher inherits the daemon's process environment and nothing
        else (ADR-0015)."""
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=rpc.STREAM_LIMIT,
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

    async def read_native_model_state(self) -> NativeModelState | None:
        """The child's actual model and resolved thinking level via
        `get_state`. `data.model.{provider,id}` and `data.thinkingLevel`
        verified against omp v18.1.10 in an isolated rpc-ui process; returns
        None only when the response has no model at all."""
        response = await self.request("get_state")
        data = response.get("data")
        data = data if isinstance(data, dict) else {}
        model = data.get("model")
        model = model if isinstance(model, dict) else {}
        provider = model.get("provider")
        model_id = model.get("id")
        if not isinstance(provider, str) or not isinstance(model_id, str):
            return None
        level = data.get("thinkingLevel")
        return NativeModelState(
            model=f"{provider}/{model_id}",
            thinking_level=level if isinstance(level, str) else None,
        )

    async def set_active_model(self, provider: str, model_id: str) -> None:
        """`set_model` with the split identifier. omp answers `success: false`
        with "Model not found: …" and leaves the previous model in place when
        the id is unknown (v18.1.10 probe), so a failure here must never be
        swallowed: it means the next prompt would run on the wrong model."""
        try:
            await self.request("set_model", provider=provider, modelId=model_id)
        except rpc.RequestFailedError as exc:
            raise ModelConfigurationError(
                f"omp refused model {provider}/{model_id}: {exc}"
            ) from exc

    async def set_thinking_level(self, level: str) -> None:
        try:
            await self.request("set_thinking_level", level=level)
        except rpc.RequestFailedError as exc:
            raise ModelConfigurationError(
                f"omp refused thinking level {level!r}: {exc}"
            ) from exc

    async def apply_model_policy(
        self, policy: ModelPolicy, *, reassert: bool
    ) -> NativeModelState:
        """Make sure this child is running the accepted active pair, and
        report what it resolved to.

        A resumed process is restored from its session file, so `--model` on
        the argv is not by itself proof (crash-recovery). `reassert` sends the
        acknowledged `set_model`/`set_thinking_level` controls before any
        prompt or resume nudge goes out. Either way the active model identity
        is then read back and compared exactly: omp fuzzy-matches `--model`,
        so a typo or a retired id would otherwise silently run a neighbour.
        """
        provider, model_id = split_model_identifier(policy.active.model)
        if reassert:
            await self.set_active_model(provider, model_id)
            await self.set_thinking_level(policy.active.thinking)
        state = await self.read_native_model_state()
        if state is None:
            raise ModelConfigurationError(
                "omp did not report an active model; refusing to prompt under "
                "unknown model settings"
            )
        if state.model != policy.active.model:
            raise ModelConfigurationError(
                f"omp resolved model {policy.active.model!r} to {state.model!r}; "
                "refusing to prompt under a substituted model"
            )
        return state

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
        policy: ModelPolicy,
        resume: str | None = None,
    ) -> AgentHandle:
        key = (task_id, session)
        if key in self._handles:
            raise AgentAlreadyRunningError(task_id, session)
        if task_id not in self._ask_timeout_verified:
            await verify_ask_timeout(clone_path)
            self._ask_timeout_verified.add(task_id)
        argv = build_agent_argv(clone_path, policy=policy, resume=resume)
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
        # Between ready and the first prompt: assert the accepted policy and
        # read back what the child actually runs (ADR-0026). A child that
        # cannot be put on the accepted model is killed here rather than
        # prompted under substituted settings.
        try:
            native = await handle.apply_model_policy(policy, reassert=resume is not None)
        except (ModelConfigurationError, rpc.RequestFailedError, rpc.AgentGoneError) as exc:
            await handle.kill()
            if self._tracker is not None:
                self._tracker.session_start_failed(
                    task_id, session, f"model configuration failed: {exc}"
                )
            raise ModelConfigurationError(str(exc)) from exc
        except TimeoutError as exc:
            await handle.kill()
            raise ModelConfigurationError(
                "omp did not answer the model configuration handshake"
            ) from exc
        handle.policy = policy
        if self._tracker is not None:
            self._tracker.record_native_model(
                task_id,
                session,
                model=native.model,
                thinking_level=native.thinking_level,
                accepted_thinking=policy.active.thinking,
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
