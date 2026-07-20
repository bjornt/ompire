"""NDJSON RPC protocol core for `omp --mode rpc-ui` children (design D-4).

One reader task per connection parses stdout lines as JSON frames. Frames of
an interpreted type (`ready`, `response`, `agent_start`, `agent_end`, and —
for ask/approval handling — `extension_ui_request`, `tool_execution_start`,
`tool_execution_end`) are validated with typed models; everything else passes
through opaque to the event callback (design D-3). `response` frames resolve
daemon-generated request ids; push events interleaved on the same stream are
never treated as responses (spike finding).
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
from typing import Any, Callable, Literal

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

# asyncio's 64 KiB default readline limit silently kills the reader on big
# frames (spike finding); omp tool-output frames can exceed 1 MiB.
STREAM_LIMIT = 4 * 1024 * 1024


class ReadyFrame(BaseModel):
    model_config = {"extra": "allow"}
    type: Literal["ready"]


class ResponseFrame(BaseModel):
    model_config = {"extra": "allow"}
    type: Literal["response"]
    id: str
    success: bool
    error: str | None = None


class AgentStartFrame(BaseModel):
    model_config = {"extra": "allow"}
    type: Literal["agent_start"]


class AgentEndFrame(BaseModel):
    model_config = {"extra": "allow"}
    type: Literal["agent_end"]


class ExtensionUiRequestFrame(BaseModel):
    """An `ask` question or approval gate mid-turn (design D-2/D-3). Only the
    reply-addressing id and the options (for the approval cross-check) are
    acted on; the rest — including any display text — passes through opaque
    for the frontend to render from the normalized payload instead."""

    model_config = {"extra": "allow"}
    type: Literal["extension_ui_request"]
    id: str
    options: list[str] | None = None


class ToolExecutionStartFrame(BaseModel):
    """Tracks in-flight tool executions (design D-1); for the `ask` tool the
    `args` payload (unvalidated here) carries the structured questions that
    become the normalized `PendingQuestion`. Field names confirmed against
    real omp during dogfooding 2026-07-20 (see the `omp-rpc-field-assumptions`
    memory note): `toolCallId` / `toolName`, not `toolUseId` / `name`."""

    model_config = {"extra": "allow"}
    type: Literal["tool_execution_start"]
    toolCallId: str
    toolName: str


class ToolExecutionEndFrame(BaseModel):
    model_config = {"extra": "allow"}
    type: Literal["tool_execution_end"]
    toolCallId: str


INTERPRETED_FRAMES: dict[str, type[BaseModel]] = {
    "ready": ReadyFrame,
    "response": ResponseFrame,
    "agent_start": AgentStartFrame,
    "agent_end": AgentEndFrame,
    "extension_ui_request": ExtensionUiRequestFrame,
    "tool_execution_start": ToolExecutionStartFrame,
    "tool_execution_end": ToolExecutionEndFrame,
}


class RequestFailedError(Exception):
    """A `response` frame reported `success: false`."""


class AgentGoneError(Exception):
    """The child's stdout closed before the request was answered."""


class RpcConnection:
    """Request writer plus the single stdout reader over a child's stdio.

    `ready` is a future resolved by the child's ready frame; requests are
    correlated by daemon-generated monotonic ids; every non-response frame
    is handed to `on_event` untouched.
    """

    def __init__(
        self,
        stdout: asyncio.StreamReader,
        stdin: asyncio.StreamWriter,
        on_event: Callable[[dict[str, Any]], None],
    ) -> None:
        self._stdout = stdout
        self._stdin = stdin
        self._on_event = on_event
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._ids = itertools.count(1)
        self.ready: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._reader_task = asyncio.create_task(self._read_loop())

    async def request(self, request_type: str, **fields: Any) -> dict[str, Any]:
        """Send a request frame and await its `response`; the ack frame is
        a receipt ("queued"), not turn completion (spike finding)."""
        request_id = f"req-{next(self._ids)}"
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self.write_frame({"type": request_type, "id": request_id, **fields})
            return await future
        finally:
            self._pending.pop(request_id, None)

    async def prompt(self, message: str) -> dict[str, Any]:
        # The field is `message` — wrong names crash omp's handler with an
        # opaque internal error instead of a validation message (spike
        # finding), so it is hard-coded here.
        return await self.request("prompt", message=message)

    async def write_frame(self, frame: dict[str, Any]) -> None:
        self._stdin.write(json.dumps(frame).encode("utf-8") + b"\n")
        await self._stdin.drain()

    async def wait_closed(self) -> None:
        """Return once the reader has drained stdout to EOF."""
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(self._reader_task)

    async def aclose(self) -> None:
        self._reader_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._reader_task

    async def _read_loop(self) -> None:
        try:
            while True:
                try:
                    line = await self._stdout.readline()
                except ValueError:
                    # Line exceeded the stream limit; readline discarded the
                    # buffered data — log and keep the reader alive.
                    logger.warning("agent frame exceeded %d-byte stream limit; dropped", STREAM_LIMIT)
                    continue
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    frame = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("unparseable agent frame dropped: %.200s", line)
                    continue
                if not isinstance(frame, dict):
                    logger.warning("non-object agent frame dropped: %.200s", line)
                    continue
                self._dispatch(frame)
        finally:
            self._fail_pending()

    def _dispatch(self, frame: dict[str, Any]) -> None:
        frame_type = frame.get("type")
        model = INTERPRETED_FRAMES.get(frame_type) if isinstance(frame_type, str) else None
        if model is not None:
            try:
                model.model_validate(frame)
            except ValidationError as exc:
                # Contained: the reader survives and the frame still reaches
                # the channel opaquely, it just can't drive interpreted logic.
                logger.warning("malformed %r frame passed through opaque: %s", frame_type, exc)
                self._on_event(frame)
                return

        if frame_type == "ready":
            if not self.ready.done():
                self.ready.set_result(frame)
            return
        if frame_type == "response":
            future = self._pending.pop(frame["id"], None)
            if future is None or future.done():
                logger.warning("response for unknown request id %r dropped", frame["id"])
                return
            if frame["success"]:
                future.set_result(frame)
            else:
                future.set_exception(RequestFailedError(frame.get("error") or "request failed"))
            return
        self._on_event(frame)

    def _fail_pending(self) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(AgentGoneError("agent stdout closed"))
        self._pending.clear()
        if not self.ready.done():
            self.ready.set_exception(AgentGoneError("agent stdout closed before ready"))
