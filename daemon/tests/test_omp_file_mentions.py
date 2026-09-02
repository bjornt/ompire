"""Real-`omp` boundary test for spawn-prompt `@file` mentions (ADR-0014).

The daemon's whole file-mention feature rests on one property of a tool it
does not own: that Omp parses `@path` out of the rpc-ui `prompt` request's
`message` and turns it into file context. Nothing in the daemon can assert
that; only Omp can demonstrate it.

So this test runs the **real** `omp --mode rpc-ui` through the production
`AgentHandle.start` / `handle.prompt` path, and points it at a local
Anthropic-compatible capture server instead of a provider. That keeps the
check deterministic and offline — no credentials, no model spend, no network
— while leaving the behavior under test entirely real.

Skipped when `omp` is not on PATH.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Self

import pytest

from ompire_daemon.agent import AgentHandle

pytestmark = pytest.mark.skipif(shutil.which("omp") is None, reason="real omp not on PATH")

MARKER = "ZQXMARKER_FILE_CONTENT_9f3a2b"

_SSE = b"""event: message_start
data: {"type":"message_start","message":{"id":"msg_1","type":"message","role":"assistant","model":"probe","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":1,"output_tokens":1}}}

event: content_block_start
data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"ok"}}

event: content_block_stop
data: {"type":"content_block_stop","index":0}

event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":1}}

event: message_stop
data: {"type":"message_stop"}

"""


class _CaptureServer:
    """Anthropic-shaped endpoint that records request bodies and replies once."""

    def __init__(self) -> None:
        self.bodies: list[str] = []
        bodies = self.bodies

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                bodies.append(body.decode("utf-8", "replace"))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(_SSE)

            def do_GET(self) -> None:
                payload = json.dumps({"data": []}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self._server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._server.shutdown()
        self._server.server_close()

    async def wait_for_body(self, timeout: float) -> str:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if self.bodies:
                return self.bodies[0]
            await asyncio.sleep(0.2)
        raise AssertionError("omp never reached the provider endpoint")


def _message_texts(body: str) -> str:
    """Every text block of every message in a captured provider request."""
    payload = json.loads(body)
    chunks: list[str] = []
    for message in payload.get("messages", []):
        content = message.get("content")
        if isinstance(content, str):
            chunks.append(content)
            continue
        for block in content or []:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                chunks.append(block["text"])
    return "\n".join(chunks)


def _argv(workdir: Path) -> list[str]:
    return [
        "omp", "--mode", "rpc-ui", "--no-title", "--no-session",
        "--cwd", str(workdir), "--model", "sonnet",
    ]


async def _prompt_real_omp(
    monkeypatch: pytest.MonkeyPatch, workdir: Path, message: str
) -> str:
    """Send one prompt through the production path; return the provider body."""
    with _CaptureServer() as server:
        monkeypatch.setenv(
            "ANTHROPIC_BASE_URL", f"http://127.0.0.1:{server.port}"
        )
        monkeypatch.setenv("ANTHROPIC_API_KEY", "capture-server-not-a-real-key")
        handle = await AgentHandle.start(
            _argv(workdir), ready_timeout=90, ring_buffer_size=200
        )
        try:
            response = await asyncio.wait_for(handle.prompt(message), timeout=60)
            assert response.get("success") is True, response
            return await server.wait_for_body(timeout=90)
        finally:
            await handle.kill()


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    work = tmp_path / "clone"
    work.mkdir()
    (work / "probe-target.txt").write_text(f"{MARKER}\nsecond line\n")
    return work


async def test_mention_in_the_rpc_message_reaches_the_model_as_file_context(
    monkeypatch: pytest.MonkeyPatch,
    workdir: Path,
) -> None:
    """The property the whole feature depends on: `@path` in the `message`
    field of a `prompt` request becomes file content in the model request."""
    texts = _message_texts(await _prompt_real_omp(monkeypatch, workdir, "What does @probe-target.txt say?"))

    assert MARKER in texts, "the mentioned file's content never reached the model"
    # Delivered as file context, not merely as the characters the operator typed.
    assert '<file path="probe-target.txt">' in texts


async def test_an_unresolvable_mention_is_dropped_silently_by_omp(
    monkeypatch: pytest.MonkeyPatch,
    workdir: Path,
) -> None:
    """The justification for the daemon's pre-flight check: Omp fails open.

    If this ever starts failing — Omp reporting the dangling mention instead
    of ignoring it — the daemon's own check becomes belt-and-braces rather
    than the only thing standing between the operator and silently missing
    context.
    """
    texts = _message_texts(await _prompt_real_omp(monkeypatch, workdir, "What does @no-such-file.txt say?"))

    assert "<file path=" not in texts
    # The turn still succeeded: nothing anywhere reports the missing file.
    assert "no-such-file.txt" in texts


async def test_an_email_address_is_not_treated_as_a_mention(
    monkeypatch: pytest.MonkeyPatch, workdir: Path
) -> None:
    """Omp's word-boundary rule matches the daemon's mention rule."""
    texts = _message_texts(await _prompt_real_omp(monkeypatch, workdir, "Mail someone@example.com about it."))

    assert "<file path=" not in texts
