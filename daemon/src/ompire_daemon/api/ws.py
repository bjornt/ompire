"""WebSocket endpoint: snapshot-then-deltas over /api/ws. No commands accepted
here — REST is the only way to mutate state (SPEC Decision 2).
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, WebSocket
from sqlalchemy import Engine

from ompire_daemon.agent import EVENT_STREAM_END, AgentSupervisor
from ompire_daemon.auth import check_ws_token
from ompire_daemon.events import EventHub
from ompire_daemon.registry.projects import list_projects
from ompire_daemon.registry.tasks import list_tasks

router = APIRouter()

# Custom close code for "no live agent behind this channel".
NO_LIVE_AGENT_CLOSE_CODE = 4404


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


async def _send_envelope(websocket: WebSocket, seq: int, event_type: str, payload: Any) -> None:
    await websocket.send_json({"seq": seq, "ts": _now_iso(), "type": event_type, "payload": payload})


async def _forward_events(
    websocket: WebSocket, queue: asyncio.Queue, seq: itertools.count
) -> None:
    while True:
        event = await queue.get()
        await _send_envelope(websocket, next(seq), event.type, event.payload)


@router.websocket("/api/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    if not check_ws_token(websocket):
        await websocket.close(code=1008)
        return

    await websocket.accept()

    engine: Engine = websocket.app.state.engine
    events: EventHub = websocket.app.state.events

    seq = itertools.count()
    projects_payload = [asdict(p) for p in list_projects(engine)]
    tasks_payload = [asdict(t) for t in list_tasks(engine)]
    # Session statuses ride separately from task rows (design D-4); JSON
    # object keys are strings, so task ids are stringified here.
    sessions_payload = {
        str(task_id): info for task_id, info in websocket.app.state.sessions.snapshot().items()
    }
    # Active attention entries (design: a reconnecting client sees them
    # without replaying `attention` events).
    attention_payload = {
        str(task_id): entry
        for task_id, entry in websocket.app.state.notifications.snapshot().items()
    }
    await _send_envelope(
        websocket,
        next(seq),
        "snapshot",
        {
            "projects": projects_payload,
            "tasks": tasks_payload,
            "sessions": sessions_payload,
            "attention": attention_payload,
        },
    )

    queue = events.subscribe()
    forwarder = asyncio.create_task(_forward_events(websocket, queue, seq))
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
    finally:
        forwarder.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await forwarder
        events.unsubscribe(queue)


async def _forward_agent_events(
    websocket: WebSocket, queue: asyncio.Queue, seq: itertools.count
) -> None:
    """Forward buffered-then-live agent events; returns on the exit sentinel."""
    while True:
        event = await queue.get()
        if event is EVENT_STREAM_END:
            return
        await _send_envelope(websocket, next(seq), event.type, event.payload)


async def _receive_until_disconnect(websocket: WebSocket) -> None:
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return


@router.websocket("/api/ws/agents/{task_id}")
async def agent_websocket_endpoint(websocket: WebSocket, task_id: int) -> None:
    """Per-agent event channel: replay the ring buffer, then stream live
    (design D-5). Same no-commands rule as the main socket."""
    if not check_ws_token(websocket):
        await websocket.close(code=1008)
        return

    supervisor: AgentSupervisor = websocket.app.state.agents
    handle = supervisor.get(task_id)
    if handle is None:
        await websocket.accept()
        await websocket.close(code=NO_LIVE_AGENT_CLOSE_CODE, reason=f"no live agent for task {task_id}")
        return

    await websocket.accept()

    # Snapshot and subscribe with no await between them, so no event can fall
    # into a gap or be duplicated between replay and live.
    replay = handle.snapshot()
    queue = handle.subscribe()
    seq = itertools.count()
    try:
        for event in replay:
            await _send_envelope(websocket, next(seq), event.type, event.payload)

        forwarder = asyncio.create_task(_forward_agent_events(websocket, queue, seq))
        receiver = asyncio.create_task(_receive_until_disconnect(websocket))
        done, pending = await asyncio.wait({forwarder, receiver}, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if forwarder in done and forwarder.exception() is None:
            # Agent exited and the buffer is flushed: close the channel.
            await websocket.close(code=1000, reason="agent exited")
    except RuntimeError:
        # Client went away mid-send; nothing left to deliver.
        pass
    finally:
        handle.unsubscribe(queue)
