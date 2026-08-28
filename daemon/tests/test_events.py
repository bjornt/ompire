import asyncio
import threading
import time

from ompire_daemon.events import EventHub


def test_publish_fans_out_to_all_subscribers() -> None:
    async def run() -> None:
        hub = EventHub()
        q1 = hub.subscribe()
        q2 = hub.subscribe()

        hub.publish("project_created", {"name": "ompire"})

        e1 = await q1.get()
        e2 = await q2.get()
        assert e1.type == e2.type == "project_created"
        assert e1.payload == e2.payload == {"name": "ompire"}

    asyncio.run(run())


def test_unsubscribed_queue_receives_nothing() -> None:
    async def run() -> None:
        hub = EventHub()
        queue = hub.subscribe()
        hub.unsubscribe(queue)

        hub.publish("project_created", {"name": "ompire"})

        assert queue.empty()

    asyncio.run(run())


def test_publish_from_a_worker_thread_wakes_a_parked_subscriber() -> None:
    """Sync REST routes run in a threadpool, so `publish` is called off the
    loop while the forwarder is parked in `queue.get()`.

    The publish must land *after* the loop has parked in `select()`, which is
    the only shape that reproduces the defect: a plain `put_nowait` from
    another thread queues the item and marks the getter ready without waking
    the loop, so delivery waits for unrelated activity. The generous timeout
    with a tight latency assertion fails loudly either way — it cannot pass by
    the loop happening to wake for something else.
    """

    async def run() -> None:
        hub = EventHub()
        hub.bind_loop(asyncio.get_running_loop())
        queue = hub.subscribe()
        received = asyncio.Event()
        stamps: dict[str, float] = {}

        async def forwarder() -> None:
            await queue.get()
            stamps["received"] = time.monotonic()
            received.set()

        task = asyncio.create_task(forwarder())
        await asyncio.sleep(0.05)

        def publish_once_the_loop_is_parked() -> None:
            time.sleep(0.2)
            stamps["published"] = time.monotonic()
            hub.publish("project_created", {"name": "ompire"})

        thread = threading.Thread(target=publish_once_the_loop_is_parked)
        thread.start()
        try:
            await asyncio.wait_for(received.wait(), timeout=2.0)
        finally:
            thread.join()
            task.cancel()

        assert stamps["received"] - stamps["published"] < 0.5

    asyncio.run(run())


def test_worker_thread_publishes_arrive_in_publication_order() -> None:
    """Per-producer FIFO is the ordering clients depend on."""

    async def run() -> None:
        hub = EventHub()
        hub.bind_loop(asyncio.get_running_loop())
        queue = hub.subscribe()

        def publish_many() -> None:
            for index in range(20):
                hub.publish("project_updated", {"index": index})

        await asyncio.to_thread(publish_many)

        received = [await asyncio.wait_for(queue.get(), timeout=1.0) for _ in range(20)]
        assert [event.payload["index"] for event in received] == list(range(20))

    asyncio.run(run())


def test_unsubscribed_queue_receives_nothing_from_a_worker_thread() -> None:
    """Delivery reads the subscriber set on the loop thread, so a queue
    unsubscribed before delivery runs is simply gone by then."""

    async def run() -> None:
        hub = EventHub()
        hub.bind_loop(asyncio.get_running_loop())
        queue = hub.subscribe()

        def publish_once() -> None:
            hub.publish("project_created", {"name": "ompire"})

        thread = threading.Thread(target=publish_once)
        thread.start()
        thread.join()
        hub.unsubscribe(queue)
        await asyncio.sleep(0.05)

        assert queue.empty()

    asyncio.run(run())


def test_subscribe_binds_the_running_loop_when_the_hub_was_never_bound() -> None:
    """Safety net: a hub built outside the daemon lifespan still delivers
    across threads."""

    async def run() -> None:
        hub = EventHub()
        queue = hub.subscribe()

        await asyncio.to_thread(hub.publish, "project_created", {"name": "ompire"})

        event = await asyncio.wait_for(queue.get(), timeout=1.0)
        assert event.type == "project_created"

    asyncio.run(run())
