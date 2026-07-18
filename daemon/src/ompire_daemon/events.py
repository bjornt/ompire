"""Async pub/sub hub: decouples event producers (e.g. project CRUD) from
WebSocket fan-out. Each subscriber (WS connection) gets its own queue.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Event:
    type: str
    payload: Any


class EventHub:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()

    def subscribe(self) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(queue)

    def publish(self, event_type: str, payload: Any) -> None:
        event = Event(type=event_type, payload=payload)
        for queue in self._subscribers:
            queue.put_nowait(event)
