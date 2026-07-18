import asyncio

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
