"""Async pub/sub hub: decouples event producers (e.g. project CRUD) from
WebSocket fan-out. Each subscriber (WS connection) gets its own queue.

Publishing is safe from any thread. Synchronous REST routes run in FastAPI's
threadpool, and `asyncio.Queue.put_nowait` called from there queues the item
but wakes the waiting getter through a non-thread-safe `call_soon`, which
never signals the loop's self-pipe. The event then waits for unrelated
activity to wake the loop — which is why a created project could take until
the next event or a reconnect to appear. Fan-out therefore always runs on the
daemon's event loop: directly when already there, and via
`call_soon_threadsafe` when not.

Delivery preserves each producer's publication order. Ordering *between*
concurrent producers is not defined, and no client depends on it.
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
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Bind the loop that owns fan-out. Called once from the app lifespan."""
        self._loop = loop

    def subscribe(self) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue()
        if self._loop is None:
            # Safety net for a hub built outside the daemon's lifespan: a
            # subscriber created on a loop names the loop that will serve it.
            # Subscribing with no loop at all is fine — that hub has no
            # cross-thread delivery to arrange.
            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                pass
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(queue)

    def publish(self, event_type: str, payload: Any) -> None:
        event = Event(type=event_type, payload=payload)
        loop = self._loop
        if loop is None or self._on_loop_thread(loop):
            self._deliver(event)
            return
        try:
            loop.call_soon_threadsafe(self._deliver, event)
        except RuntimeError:
            # The loop closed while this thread was publishing; there is no
            # subscriber left to serve.
            pass

    @staticmethod
    def _on_loop_thread(loop: asyncio.AbstractEventLoop) -> bool:
        try:
            return asyncio.get_running_loop() is loop
        except RuntimeError:
            return False

    def _deliver(self, event: Event) -> None:
        """Always runs on the bound loop, so the subscriber set is read and
        mutated from one thread and a queue unsubscribed before delivery is
        already gone."""
        for queue in self._subscribers:
            queue.put_nowait(event)
