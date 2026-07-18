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

from ompire_daemon.auth import check_ws_token
from ompire_daemon.events import EventHub
from ompire_daemon.registry.projects import list_projects

router = APIRouter()


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
    await _send_envelope(websocket, next(seq), "snapshot", {"projects": projects_payload, "tasks": []})

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
