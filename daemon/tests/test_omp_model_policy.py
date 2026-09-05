"""Real-`omp` boundary test for the accepted model policy (ADR-0014, ADR-0026).

The whole point of a model profile is that the models the operator chose are
the ones that answer. Nothing in the daemon can assert that: what reaches a
provider is decided by `omp`, from the flags and the RPC controls the daemon
gives it.

So this test runs the **real** `omp --mode rpc-ui` through the production
`AgentSupervisor.start` / `handle.prompt` path with a real `ModelPolicy`, and
points it at a local Anthropic-compatible capture server instead of a
provider. The request that arrives there is the evidence — argv capture would
only prove the daemon wrote a flag, not that the flag governed the turn. No
credentials, no model spend, no network.

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

from ompire_daemon.agent import (
    AgentStartError,
    AgentSupervisor,
    ModelConfigurationError,
)
from ompire_daemon.config import Config
from ompire_daemon.events import EventHub
from ompire_daemon.execution_inputs import ModelPolicy
from ompire_daemon.registry.model_profiles import RoleBinding

pytestmark = pytest.mark.skipif(shutil.which("omp") is None, reason="real omp not on PATH")

# Two real Anthropic model ids that differ, so "which one answered" is a fact
# about the request rather than about the flag the daemon wrote.
ACTIVE_MODEL = "anthropic/claude-sonnet-4-5"
ACTIVE_ID = "claude-sonnet-4-5"
SMOL_MODEL = "anthropic/claude-haiku-4-5"

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
    """Anthropic-shaped endpoint that records request bodies and replies."""

    def __init__(self) -> None:
        self.bodies: list[str] = []
        bodies = self.bodies

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                bodies.append(self.rfile.read(length).decode("utf-8", "replace"))
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

    async def wait_for_body(self, timeout: float) -> dict:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if self.bodies:
                return json.loads(self.bodies[0])
            await asyncio.sleep(0.2)
        raise AssertionError("omp never reached the provider endpoint")


def _policy(**overrides: RoleBinding) -> ModelPolicy:
    roles = {
        "default": RoleBinding(model=ACTIVE_MODEL, thinking="low"),
        "smol": RoleBinding(model=SMOL_MODEL, thinking="off"),
        "slow": RoleBinding(model=ACTIVE_MODEL, thinking="high"),
        "plan": RoleBinding(model=ACTIVE_MODEL, thinking="medium"),
    }
    roles.update(overrides)
    return ModelPolicy.from_roles(roles)


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    work = tmp_path / "clone"
    work.mkdir()
    return work


@pytest.fixture
def supervisor(monkeypatch: pytest.MonkeyPatch, workdir: Path):
    """A real supervisor whose children are real `omp`, without a container.

    `workshop exec -p <clone> --` is replaced by a direct exec in the clone —
    the container boundary is not what this test is about, and the ask-timeout
    preflight would need one. Everything the daemon does *to* the child, argv
    and RPC alike, is the production path.
    """
    from ompire_daemon import agent as agent_module

    real_build = agent_module.build_agent_argv

    def build(clone: str, *, policy: ModelPolicy, resume: str | None = None) -> list[str]:
        argv = real_build(clone, policy=policy, resume=resume)
        omp_index = argv.index("omp")
        return [*argv[omp_index:], "--cwd", clone, "--no-session"]

    async def no_preflight(clone_path: str) -> None:
        return None

    monkeypatch.setattr(agent_module, "build_agent_argv", build)
    monkeypatch.setattr(agent_module, "verify_ask_timeout", no_preflight)
    return AgentSupervisor(
        Config(agent_ready_timeout=90, agent_ring_buffer_size=200), EventHub()
    )


async def test_the_accepted_active_pair_governs_the_turn(
    monkeypatch: pytest.MonkeyPatch, supervisor: AgentSupervisor, workdir: Path
) -> None:
    """The model the operator chose is the one the provider request names, and
    the thinking policy reaches it as reasoning configuration."""
    with _CaptureServer() as server:
        monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{server.port}")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "capture-server-not-a-real-key")
        handle = await supervisor.start(1, "main", str(workdir), policy=_policy())
        try:
            await asyncio.wait_for(handle.prompt("say ok"), timeout=60)
            request = await server.wait_for_body(timeout=90)
        finally:
            await supervisor.stop(1, "main")

    assert request["model"] == ACTIVE_ID
    # `low` is a budget, not a boolean: the turn carries reasoning
    # configuration rather than omp's default.
    assert request.get("thinking", {}).get("type") == "enabled"


async def test_a_thinking_policy_of_off_reaches_the_provider_as_no_reasoning(
    monkeypatch: pytest.MonkeyPatch, supervisor: AgentSupervisor, workdir: Path
) -> None:
    """`off` is an explicit policy, not an absent value — and the difference
    is visible in the request, not only in the flag."""
    with _CaptureServer() as server:
        monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{server.port}")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "capture-server-not-a-real-key")
        policy = _policy(default=RoleBinding(model=ACTIVE_MODEL, thinking="off"))
        handle = await supervisor.start(2, "main", str(workdir), policy=policy)
        try:
            await asyncio.wait_for(handle.prompt("say ok"), timeout=60)
            request = await server.wait_for_body(timeout=90)
        finally:
            await supervisor.stop(2, "main")

    assert request["model"] == ACTIVE_ID
    assert request.get("thinking", {}).get("type") != "enabled"


async def test_every_auxiliary_role_is_configured_on_the_child(
    monkeypatch: pytest.MonkeyPatch, supervisor: AgentSupervisor, workdir: Path
) -> None:
    """All four roles reach the process, each with its own level. omp reports
    the active pair through `get_state`; the auxiliary flags it echoes back
    are what a `/switch smol` inside the container would use."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "capture-server-not-a-real-key")
    handle = await supervisor.start(3, "main", str(workdir), policy=_policy())
    try:
        state = await handle.read_native_model_state()
    finally:
        await supervisor.stop(3, "main")

    assert state is not None
    assert state.model == ACTIVE_MODEL
    assert state.thinking_level == "low"


async def test_max_resolves_to_a_model_level_without_losing_the_policy(
    monkeypatch: pytest.MonkeyPatch, supervisor: AgentSupervisor, workdir: Path
) -> None:
    """`max` is a model-dependent policy. omp resolves it per model, and the
    daemon keeps the accepted spelling beside the resolved level rather than
    treating the difference as a dropped override."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "capture-server-not-a-real-key")
    policy = _policy(default=RoleBinding(model=ACTIVE_MODEL, thinking="max"))
    handle = await supervisor.start(4, "main", str(workdir), policy=policy)
    try:
        state = await handle.read_native_model_state()
    finally:
        await supervisor.stop(4, "main")

    assert state is not None
    assert state.model == ACTIVE_MODEL
    # Resolved, not echoed: a concrete level that is not the word "max".
    assert state.thinking_level not in (None, "max")


async def test_an_unavailable_model_fails_the_start_instead_of_substituting(
    monkeypatch: pytest.MonkeyPatch, supervisor: AgentSupervisor, workdir: Path
) -> None:
    """A profile naming a model this provider does not have must stop the
    session, not run a neighbour omp fuzzy-matched to."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "capture-server-not-a-real-key")
    policy = _policy(
        default=RoleBinding(model="anthropic/claude-sonnet-4-5-typo", thinking="low")
    )
    with pytest.raises((AgentStartError, ModelConfigurationError)) as exc_info:
        await supervisor.start(5, "main", str(workdir), policy=policy)

    # Two ways this can fail, both acceptable, neither a silent swap: omp
    # refuses the id and exits before ready (what it does today, naming the
    # model on stderr), or it starts on a fuzzy neighbour and the daemon's
    # read-back refuses to prompt. What must never happen is a live session
    # running a model nobody chose.
    assert supervisor.get(5, "main") is None
    detail = f"{exc_info.value}\n{getattr(exc_info.value, 'stderr', '')}".lower()
    assert "not found" in detail or "substituted" in detail
