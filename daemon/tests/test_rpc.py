"""Protocol-core tests: `rpc.RpcConnection` against the fake omp fixture."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from ompire_daemon import rpc

FAKE_OMP = Path(__file__).parent / "fake_omp.py"


def fake_omp_argv(scenario: str = "happy") -> list[str]:
    return [sys.executable, "-u", str(FAKE_OMP), scenario]


async def spawn_fake(scenario: str = "happy") -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        *fake_omp_argv(scenario),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=rpc.STREAM_LIMIT,
    )


async def wait_for_event(events: list[dict], event_type: str, timeout: float = 5.0) -> dict:
    """Poll `events` until a frame of `event_type` shows up."""
    async with asyncio.timeout(timeout):
        while True:
            for frame in events:
                if frame.get("type") == event_type:
                    return frame
            await asyncio.sleep(0.01)


@pytest.fixture
async def connection():
    """A ready RpcConnection over a happy fake omp, torn down afterwards."""
    process = await spawn_fake()
    events: list[dict] = []
    conn = rpc.RpcConnection(process.stdout, process.stdin, events.append)
    try:
        yield conn, events, process
    finally:
        await conn.aclose()
        if process.returncode is None:
            process.kill()
        await process.wait()


async def test_ready_frame_resolves(connection) -> None:
    conn, _, _ = connection
    frame = await asyncio.wait_for(conn.ready, timeout=5)
    assert frame["type"] == "ready"


async def test_prompt_ack_with_interleaved_events(connection) -> None:
    conn, events, _ = connection
    await asyncio.wait_for(conn.ready, timeout=5)
    response = await asyncio.wait_for(conn.prompt("hi"), timeout=5)
    assert response["success"] is True
    # The push event emitted before the ack was dispatched as an event, not
    # mistaken for the response.
    assert events[0]["type"] == "extension_ui_request"
    await wait_for_event(events, "agent_end")
    types = [frame["type"] for frame in events]
    assert "response" not in types  # responses resolve futures, never fan out
    assert types.index("agent_start") < types.index("agent_end")


async def test_response_failure_raises(connection) -> None:
    conn, _, _ = connection
    await asyncio.wait_for(conn.ready, timeout=5)
    with pytest.raises(rpc.RequestFailedError, match="Unknown command"):
        await asyncio.wait_for(conn.request("bogus"), timeout=5)


async def test_big_frame_survives_stream_limit(connection) -> None:
    conn, events, _ = connection
    await asyncio.wait_for(conn.ready, timeout=5)
    await asyncio.wait_for(conn.prompt("big"), timeout=5)
    frame = await wait_for_event(events, "tool_output")
    assert len(frame["data"]) == 200_000  # > asyncio's 64 KiB default limit
    await wait_for_event(events, "agent_end")  # reader survived the big frame


async def test_parse_error_does_not_kill_reader(connection) -> None:
    conn, events, _ = connection
    await asyncio.wait_for(conn.ready, timeout=5)
    await asyncio.wait_for(conn.prompt("garbage"), timeout=5)
    # The unparseable line was logged and skipped; frames after it flow on.
    await wait_for_event(events, "note")
    await wait_for_event(events, "agent_end")


async def test_eof_fails_pending_requests(connection) -> None:
    conn, _, process = connection
    await asyncio.wait_for(conn.ready, timeout=5)
    with pytest.raises(rpc.AgentGoneError):
        await asyncio.wait_for(conn.prompt("die"), timeout=5)
    assert await process.wait() == 23
