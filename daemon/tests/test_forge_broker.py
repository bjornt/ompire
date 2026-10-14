"""Offline consumer/security tests; helpers also support the manual broker smoke."""

from __future__ import annotations

import base64
import http.client
import io
import json
import logging
import os
import queue
import re
import socket
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import pairwise
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from ompire_daemon.forge_broker import ForgeBroker, decide

TOKEN = "ghp_broker-test-secret-3b867f"
REPOS = frozenset({"acme/widgets"})
BASIC_CREDENTIAL = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()
DAEMON_ROOT = Path(__file__).resolve().parents[1]
BROKER_SCRIPT = DAEMON_ROOT / "src" / "ompire_daemon" / "forge_broker.py"


@dataclass(frozen=True)
class UpstreamRequest:
    method: str
    target: str
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class UpstreamReply:
    status: int = 200
    body: bytes = b"upstream response"
    headers: tuple[tuple[str, str], ...] = (("Content-Type", "text/plain"),)
    chunks: tuple[bytes, ...] = ()
    chunked: bool = False
    disconnect: bool = False


ReplyFactory = Callable[[UpstreamRequest], UpstreamReply]


@dataclass
class FakeUpstream:
    server: ThreadingHTTPServer
    requests: list[UpstreamRequest] = field(default_factory=list)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"


def _read_request_body(handler: BaseHTTPRequestHandler) -> bytes:
    if handler.headers.get("Transfer-Encoding", "").lower() != "chunked":
        return handler.rfile.read(int(handler.headers.get("Content-Length", "0")))
    chunks = []
    while True:
        size = int(handler.rfile.readline().split(b";", 1)[0].strip(), 16)
        if not size:
            while handler.rfile.readline() not in (b"\r\n", b"\n", b""):
                pass
            return b"".join(chunks)
        chunks.append(handler.rfile.read(size))
        assert handler.rfile.read(2) == b"\r\n"


@contextmanager
def serving(server: ThreadingHTTPServer) -> Iterator[ThreadingHTTPServer]:
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive(), "test HTTP server did not stop"


@contextmanager
def fake_upstream(reply: ReplyFactory | None = None) -> Iterator[FakeUpstream]:
    """Serve scripted HTTP replies on loopback; record the actual wire requests."""
    state: FakeUpstream

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args: object) -> None:
            pass

        def do_request(self) -> None:
            request = UpstreamRequest(
                self.command,
                self.path,
                {key.lower(): value for key, value in self.headers.items()},
                _read_request_body(self),
            )
            state.requests.append(request)
            response = reply(request) if reply else UpstreamReply()
            if response.disconnect:
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            self.send_response(response.status)
            for key, value in response.headers:
                self.send_header(key, value)
            pieces = response.chunks or (response.body,)
            if response.chunked:
                self.send_header("Transfer-Encoding", "chunked")
            elif not any(
                key.lower() == "content-length" for key, _ in response.headers
            ):
                self.send_header("Content-Length", str(sum(map(len, pieces))))
            self.end_headers()
            if self.command == "HEAD":
                return
            for piece in pieces:
                if not piece:
                    continue
                if response.chunked:
                    self.wfile.write(f"{len(piece):x}\r\n".encode())
                self.wfile.write(piece)
                if response.chunked:
                    self.wfile.write(b"\r\n")
                self.wfile.flush()
            if response.chunked:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()

        do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_request

    state = FakeUpstream(ThreadingHTTPServer(("127.0.0.1", 0), Handler))
    with serving(state.server):
        yield state


class AuditCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []
        self.changed = threading.Condition()

    def emit(self, record: logging.LogRecord) -> None:
        with self.changed:
            self.lines.append(record.getMessage())
            self.changed.notify_all()

    def records(self, count: int) -> list[dict]:
        with self.changed:
            assert self.changed.wait_for(lambda: len(self.lines) >= count, timeout=5), (
                f"expected {count} audit records, received {self.lines!r}"
            )
            records = [json.loads(line) for line in self.lines]
        assert len(records) == count, "each request must emit exactly one audit line"
        return records


@dataclass
class BrokerHarness:
    server: ForgeBroker
    audit: AuditCapture

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"


@contextmanager
def running_broker(
    upstream_base: str,
    *,
    token: str = TOKEN,
    repos: frozenset[str] = REPOS,
) -> Iterator[BrokerHarness]:
    """Run the production broker, not a mocked forwarding layer."""
    audit = AuditCapture()
    logger = logging.getLogger(f"forge-broker-test.{id(audit)}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(audit)
    server = ForgeBroker(
        token, repos, port=0, upstream_base=upstream_base, logger=logger
    )
    with serving(server):
        yield BrokerHarness(server, audit)
    logger.removeHandler(audit)
    audit.close()


def request(
    base_url: str,
    method: str,
    target: str,
    *,
    body: bytes | Iterator[bytes] | None = None,
    headers: Mapping[str, str] | None = None,
    chunked: bool = False,
) -> tuple[int, dict[str, str], bytes]:
    address = urlsplit(base_url)
    connection = http.client.HTTPConnection(address.hostname, address.port, timeout=10)
    try:
        connection.request(
            method,
            target,
            body=body,
            headers=dict(headers or {}),
            encode_chunked=chunked,
        )
        response = connection.getresponse()
        return (
            response.status,
            {key.lower(): value for key, value in response.getheaders()},
            response.read(),
        )
    finally:
        connection.close()


def assert_secret_free(*values: object, token: str = TOKEN) -> None:
    encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    for value in values:
        text = (
            value.decode(errors="replace") if isinstance(value, bytes) else str(value)
        )
        assert token not in text
        assert encoded not in text


def cli_environment() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "PYTHONPATH": str(DAEMON_ROOT / "src"),
        "PYTHONUNBUFFERED": "1",
        "LC_ALL": "C",
    }


def cli_command(*args: str, module: bool = False) -> list[str]:
    invocation = (
        ["-m", "ompire_daemon.forge_broker"] if module else [str(BROKER_SCRIPT)]
    )
    return [sys.executable, "-u", *invocation, *args]


def cli_failure(
    args: list[str],
    *,
    env: Mapping[str, str] | None = None,
    **kwargs: object,
) -> str:
    result = subprocess.run(
        cli_command(*args),
        cwd=DAEMON_ROOT,
        env=dict(env or cli_environment()),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
        **kwargs,
    )
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "forge broker ready" not in output
    assert_secret_free(output)
    return output.lower()


@dataclass
class BrokerProcess:
    process: subprocess.Popen[str]
    base_url: str
    stdout: list[str]
    stderr: list[str]


@contextmanager
def broker_process(
    args: list[str],
    *,
    env: Mapping[str, str] | None = None,
    module: bool = False,
) -> Iterator[BrokerProcess]:
    process = subprocess.Popen(
        cli_command(*args, module=module),
        cwd=DAEMON_ROOT,
        env=dict(env or cli_environment()),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    stdout: list[str] = []
    stderr: list[str] = []
    first_line: queue.Queue[str] = queue.Queue()

    def capture(stream: io.TextIOBase, lines: list[str], first: bool = False) -> None:
        for line in stream:
            lines.append(line.rstrip("\n"))
            if first:
                first_line.put(line.rstrip("\n"))
                first = False
        if first:
            first_line.put("")

    assert process.stdout is not None and process.stderr is not None
    threads = [
        threading.Thread(
            target=capture, args=(process.stdout, stdout, True), daemon=True
        ),
        threading.Thread(target=capture, args=(process.stderr, stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        try:
            line = first_line.get(timeout=10)
        except queue.Empty:
            pytest.fail(f"broker did not announce readiness; stderr={stderr!r}")
        match = re.fullmatch(r"forge broker ready (http://127\.0\.0\.1:([0-9]+))", line)
        assert match, f"invalid readiness line {line!r}; stderr={stderr!r}"
        assert 0 < int(match.group(2)) < 65536
        yield BrokerProcess(process, match.group(1), stdout, stderr)
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=5)
            assert not thread.is_alive(), "CLI output capture did not stop"
        process.stdout.close()
        process.stderr.close()


def test_cli_requires_exactly_one_nonempty_token_source(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text(TOKEN + "\n")
    missing = cli_failure(["--repo", "acme/widgets", "--port", "0"])
    assert "token" in missing
    assert "file" in missing or "ompire_forge_token" in missing
    both = cli_failure(
        ["--token-file", str(token_file), "--repo", "acme/widgets", "--port", "0"],
        env={**cli_environment(), "OMPIRE_FORGE_TOKEN": TOKEN},
    )
    assert "token" in both
    assert any(word in both for word in ("exactly", "both", "one", "exclusive"))
    empty_env = cli_failure(
        ["--repo", "acme/widgets", "--port", "0"],
        env={**cli_environment(), "OMPIRE_FORGE_TOKEN": ""},
    )
    assert "token" in empty_env
    for contents in ("", " \n\t"):
        token_file.write_text(contents)
        empty_file = cli_failure(
            ["--token-file", str(token_file), "--repo", "acme/widgets", "--port", "0"],
        )
        assert "token" in empty_file and "empty" in empty_file


def test_cli_missing_and_unreadable_token_file(tmp_path: Path) -> None:
    missing = tmp_path / "missing-token"
    output = cli_failure(["--token-file", str(missing), "--repo", "acme/widgets"])
    assert "token" in output and any(
        word in output for word in ("read", "no such", "exist")
    )
    directory = tmp_path / "not-a-token-file"
    directory.mkdir()
    output = cli_failure(["--token-file", str(directory), "--repo", "acme/widgets"])
    assert "token" in output and any(word in output for word in ("read", "directory"))
    token_file = tmp_path / "permission-denied-token"
    token_file.write_text(TOKEN)
    token_file.chmod(0)
    # Root can read mode-000 files: drop privilege in the child to exercise EACCES.
    identity = {"user": 65534, "group": 65534} if os.geteuid() == 0 else {}
    output = cli_failure(
        ["--token-file", str(token_file), "--repo", "acme/widgets"],
        **identity,
    )
    assert "token" in output and any(word in output for word in ("read", "permission"))


def test_cli_requires_repositories_and_rejects_bad_repository_shapes() -> None:
    env = {**cli_environment(), "OMPIRE_FORGE_TOKEN": TOKEN}
    assert "repo" in cli_failure(["--port", "0"], env=env)
    for repo in (
        "acme",
        "acme/",
        "/widgets",
        "acme/.git",
        "acme/widgets/extra",
        "acme/../widgets",
    ):
        assert "repo" in cli_failure(["--repo", repo, "--port", "0"], env=env)


@pytest.mark.parametrize(
    "bind", ["0.0.0.0", "::1", "127.0.0.2", "example.com", "localhost.evil"]
)
def test_cli_refuses_nonliteral_loopback_bind(bind: str) -> None:
    output = cli_failure(
        ["--repo", "acme/widgets", "--bind", bind, "--port", "0"],
        env={**cli_environment(), "OMPIRE_FORGE_TOKEN": TOKEN},
    )
    assert any(word in output for word in ("bind", "localhost", "127.0.0.1"))


@pytest.mark.parametrize(
    "upstream",
    [
        "http://example.com:8000",
        "https://127.0.0.1:8000",
        "http://127.0.0.1.evil:8000",
        "http://user:password@127.0.0.1:8000",
        "http://127.0.0.1:8000/path",
    ],
)
def test_cli_test_hook_cannot_select_external_or_ambiguous_upstreams(
    upstream: str,
) -> None:
    output = cli_failure(
        ["--repo", "acme/widgets", "--port", "0", "--upstream-base", upstream],
        env={**cli_environment(), "OMPIRE_FORGE_TOKEN": TOKEN},
    )
    assert "upstream" in output or "loopback" in output


def test_cli_bind_conflict_is_specific_and_token_free() -> None:
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        output = cli_failure(
            ["--repo", "acme/widgets", "--port", str(occupied.getsockname()[1])],
            env={**cli_environment(), "OMPIRE_FORGE_TOKEN": TOKEN},
        )
    assert "bind" in output or "address already in use" in output


@pytest.mark.parametrize("module,bind", [(False, "127.0.0.1"), (True, "localhost")])
def test_cli_readiness_health_and_repeatable_repositories(
    module: bool, bind: str
) -> None:
    with (
        fake_upstream() as upstream,
        broker_process(
            [
                "--repo",
                "acme/widgets",
                "--repo",
                "acme/second",
                "--bind",
                bind,
                "--port",
                "0",
                "--upstream-base",
                upstream.base_url,
            ],
            env={**cli_environment(), "OMPIRE_FORGE_TOKEN": TOKEN},
            module=module,
        ) as broker,
    ):
        for method in ("GET", "HEAD"):
            status, _, body = request(broker.base_url, method, "/healthz")
            assert status == 200
            if method == "HEAD":
                assert body == b""
        assert not upstream.requests
        assert request(broker.base_url, "GET", "/api/repos/acme/second")[0] == 200
    assert len(broker.stdout) == 1
    assert_secret_free(broker.stdout, broker.stderr)
    audit = [json.loads(line) for line in broker.stderr]
    assert len(audit) == 3
    assert all(record["upstream_status"] is None for record in audit[:2])


def test_restart_rereads_replaced_token_file(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    rotated = "ghp_replacement-secret-41d8"
    with fake_upstream() as upstream:
        args = [
            "--token-file",
            str(token_file),
            "--repo",
            "acme/widgets",
            "--port",
            "0",
            "--upstream-base",
            upstream.base_url,
        ]
        outputs = []
        for token in (TOKEN, rotated):
            token_file.write_text(token + "\n")
            with broker_process(args) as broker:
                assert (
                    request(broker.base_url, "GET", "/api/repos/acme/widgets")[0] == 200
                )
            outputs.extend([broker.stdout, broker.stderr])
        assert [item.headers["authorization"] for item in upstream.requests] == [
            f"Bearer {TOKEN}",
            f"Bearer {rotated}",
        ]
        assert_secret_free(outputs)
        assert_secret_free(outputs, token=rotated)


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize(
    "path,query,upstream",
    [
        ("/api/repos/acme/widgets", "", "https://api.github.com/repos/acme/widgets"),
        ("/api/repos/acme/widgets/", "", "https://api.github.com/repos/acme/widgets/"),
        (
            "/api/repos/acme/widgets/pulls",
            "state=all&per_page=50",
            "https://api.github.com/repos/acme/widgets/pulls?state=all&per_page=50",
        ),
        (
            "/api/repos/acme/widgets/pulls/42",
            "",
            "https://api.github.com/repos/acme/widgets/pulls/42",
        ),
        (
            "/api/repos/acme/widgets/commits/abc123",
            "",
            "https://api.github.com/repos/acme/widgets/commits/abc123",
        ),
        (
            "/api/repos/acme/widgets/commits/abc123/check-runs",
            "",
            "https://api.github.com/repos/acme/widgets/commits/abc123/check-runs",
        ),
        (
            "/api/repos/acme/widgets/commits/abc123/status",
            "",
            "https://api.github.com/repos/acme/widgets/commits/abc123/status",
        ),
        (
            "/git/acme/widgets/info/refs",
            "service=git-upload-pack",
            "https://github.com/acme/widgets.git/info/refs?service=git-upload-pack",
        ),
        (
            "/git/acme/widgets.git/info/refs",
            "service=git-upload-pack",
            "https://github.com/acme/widgets.git/info/refs?service=git-upload-pack",
        ),
    ],
)
def test_policy_allows_only_repository_reads_and_upload_discovery(
    method: str,
    path: str,
    query: str,
    upstream: str,
) -> None:
    decision = decide(method, path, query, REPOS)
    assert decision.allowed is True
    assert decision.upstream_url == upstream
    assert isinstance(decision.rule, str) and decision.rule


@pytest.mark.parametrize("suffix", ["", ".git"])
def test_policy_upload_rpc_and_case_insensitive_repository_identity(
    suffix: str,
) -> None:
    decision = decide("POST", f"/git/ACME/Widgets{suffix}/git-upload-pack", "", REPOS)
    assert decision.allowed and decision.rule
    assert (
        decision.upstream_url == "https://github.com/ACME/Widgets.git/git-upload-pack"
    )
    assert decide(
        "GET", "/api/repos/acme/widgets", "", frozenset({"Acme/Widgets"})
    ).allowed


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_policy_health_is_local(method: str) -> None:
    decision = decide(method, "/healthz", "", frozenset())
    assert decision.allowed and decision.rule
    assert decision.upstream_url is None


@pytest.mark.parametrize(
    "method,path,query",
    [
        ("POST", "/api/repos/acme/widgets/pulls", ""),
        ("PUT", "/api/repos/acme/widgets/contents/file", ""),
        ("PATCH", "/api/repos/acme/widgets/pulls/1", ""),
        ("DELETE", "/api/repos/acme/widgets", ""),
        ("OPTIONS", "/api/repos/acme/widgets", ""),
        ("TRACE", "/api/repos/acme/widgets", ""),
        ("CONNECT", "/api/repos/acme/widgets", ""),
        ("GET", "/api/user", ""),
        ("GET", "/api/user/keys", ""),
        ("GET", "/api/orgs/acme/members", ""),
        ("GET", "/api/graphql", ""),
        ("POST", "/api/graphql", ""),
        ("GET", "/graphql", ""),
        ("GET", "/", ""),
        ("GET", "/configuration", ""),
        ("GET", "/unknown", ""),
        ("GET", "/api/repos/acme/widgets-other", ""),
        ("GET", "/api/repos/acme/other", ""),
        ("GET", "/api/repos/other/widgets", ""),
        ("GET", "/api/repos/acme", ""),
        ("GET", "/api/repos/acme/widgets.git", ""),
        ("GET", "/git/acme/other.git/info/refs", "service=git-upload-pack"),
        ("GET", "/git/acme/widgets-other.git/info/refs", "service=git-upload-pack"),
        ("GET", "/git/acme/widgets.git/info/refs", "service=git-receive-pack"),
        ("POST", "/git/acme/widgets.git/git-receive-pack", ""),
        ("POST", "/git/acme/widgets/git-receive-pack", ""),
        ("GET", "/git/acme/widgets.git/info/refs", ""),
        ("GET", "/git/acme/widgets.git/info/refs", "service=git-upload-pack&extra=1"),
        (
            "GET",
            "/git/acme/widgets.git/info/refs",
            "service=git-upload-pack&service=git-receive-pack",
        ),
        ("HEAD", "/git/acme/widgets.git/info/refs", "service=git-receive-pack"),
        ("POST", "/git/acme/widgets.git/info/refs", "service=git-upload-pack"),
        ("GET", "/git/acme/widgets.git/git-upload-pack", ""),
        ("HEAD", "/git/acme/widgets.git/git-upload-pack", ""),
        ("POST", "/git/acme/widgets.git/git-upload-pack", "extra=1"),
        ("POST", "/git/acme/widgets.git/git-upload-pack/", ""),
        ("GET", "/git/acme/widgets.git/HEAD", ""),
        ("GET", "/git/acme/widgets.git/objects/ab/cdef", ""),
        ("POST", "/healthz", ""),
    ],
)
def test_policy_denies_other_capabilities(method: str, path: str, query: str) -> None:
    decision = decide(method, path, query, REPOS)
    assert decision.allowed is False
    assert decision.upstream_url is None
    assert isinstance(decision.rule, str) and decision.rule


@pytest.mark.parametrize(
    "path",
    [
        "/api/repos/acme/widgets/../other",
        "/api/repos/acme/widgets/./commits",
        "/api/repos/acme/widgets//commits",
        "/api/repos/acme//widgets",
        "/api/repos/acme/widgets/%2e%2e/other",
        "/api/repos/acme/widgets/%252e%252e/other",
        "/api/repos/acme/widgets%2f..%2fother",
        "/api/repos/acme/widgets%2Fother",
        "/api/repos/acme/widgets%5c..%5cother",
        "/api/repos/acme/widgets\\..\\other",
        "/api/repos/acme/widgets%3fignored",
        "/api/repos/acme/widgets%23ignored",
        "/api/repos/acme/widgets%00",
        "/api/repos/acme/widgets/commits/%ZZ",
        "/git/acme/widgets.git/../other.git/info/refs",
        "/git/acme/widgets.git/%2e%2e/info/refs",
        "/git/acme/widgets.git%2finfo/refs",
        "//api/repos/acme/widgets",
        "http://api.github.com/repos/acme/widgets",
    ],
)
def test_policy_denies_ambiguous_path_normalization(path: str) -> None:
    query = "service=git-upload-pack" if path.startswith("/git/") else ""
    assert not decide("GET", path, query, REPOS).allowed


@pytest.mark.parametrize("surface", ["api", "git"])
def test_proxy_replaces_authority_and_drops_untrusted_request_headers(
    surface: str,
) -> None:
    target = (
        "/api/repos/acme/widgets"
        if surface == "api"
        else "/git/acme/widgets.git/git-upload-pack"
    )
    method = "GET" if surface == "api" else "POST"
    body = None if surface == "api" else b"0000"
    caller = {
        "Authorization": "Bearer sandbox-injected",
        "Proxy-Authorization": "Basic injected",
        "Host": "attacker.invalid",
        "Cookie": "session=attacker",
        "Accept-Encoding": "gzip",
        "X-Ompire-Task": "spoofable-task",
        "X-Injected": "untrusted",
        "X-Forwarded-Host": "attacker.invalid",
        "X-Forwarded-For": "203.0.113.1",
        "Connection": "keep-alive, X-Injected",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/x-git-upload-pack-request",
        "User-Agent": "broker-test/1",
        "Git-Protocol": "version=2",
    }
    with fake_upstream() as upstream, running_broker(upstream.base_url) as broker:
        assert (
            request(broker.base_url, method, target, headers=caller, body=body)[0]
            == 200
        )
        received = upstream.requests[0]
        expected_auth = (
            f"Bearer {TOKEN}" if surface == "api" else f"Basic {BASIC_CREDENTIAL}"
        )
        assert received.headers["authorization"] == expected_auth
        assert received.headers["host"] == urlsplit(upstream.base_url).netloc
        for name in ("accept", "content-type", "user-agent", "git-protocol"):
            assert received.headers[name] == next(
                value for key, value in caller.items() if key.lower() == name
            )
        assert set(received.headers) <= {
            "authorization",
            "host",
            "accept",
            "content-type",
            "user-agent",
            "git-protocol",
            "content-length",
            "transfer-encoding",
            "connection",
            "accept-encoding",
        }
        assert received.headers.get("accept-encoding") in {None, "identity"}
        assert received.headers.get("connection") != caller["Connection"]
        assert received.body == (body or b"")
        records = broker.audit.records(1)
        assert records[0]["task"] == "spoofable-task"
        assert_secret_free(records)


def test_response_allowlist_scrubs_header_body_and_error_status() -> None:
    headers = (
        ("Content-Type", "text/plain"),
        ("Cache-Control", f"private, extension={TOKEN}"),
        ("Expires", "Thu, 01 Jan 1970 00:00:00 GMT"),
        ("Pragma", "no-cache"),
        ("X-GitHub-Request-Id", f"request-{TOKEN}"),
        ("X-RateLimit-Limit", "5000"),
        ("X-RateLimit-Remaining", "4999"),
        ("X-RateLimit-Reset", "2000000000"),
        ("X-RateLimit-Resource", f"core-{BASIC_CREDENTIAL}"),
        ("Set-Cookie", f"secret={TOKEN}"),
        ("WWW-Authenticate", f"Bearer {TOKEN}"),
        ("X-Upstream-Secret", TOKEN),
        ("Content-Encoding", "identity"),
    )
    data = f"begin echoed PAT={TOKEN}; Authorization: Basic {BASIC_CREDENTIAL}; end".encode()
    with (
        fake_upstream(
            lambda _: UpstreamReply(status=503, headers=headers, body=data)
        ) as upstream,
        running_broker(upstream.base_url) as broker,
    ):
        status, received, body = request(
            broker.base_url, "GET", "/api/repos/acme/widgets"
        )
        assert status == 503
        for key in (
            "content-type",
            "cache-control",
            "expires",
            "pragma",
            "x-github-request-id",
            "x-ratelimit-limit",
            "x-ratelimit-remaining",
            "x-ratelimit-reset",
            "x-ratelimit-resource",
        ):
            assert key in received
        assert received["x-ratelimit-remaining"] == "4999"
        for key in (
            "set-cookie",
            "www-authenticate",
            "x-upstream-secret",
            "content-encoding",
            "content-length",
        ):
            assert key not in received
        assert body.startswith(b"begin echoed PAT=") and body.endswith(b"; end")
        records = broker.audit.records(1)
        assert records[0]["upstream_status"] == 503 and records[0]["status"] == 503
        assert_secret_free(received, body, records)


@pytest.mark.parametrize("chunked", [False, True])
def test_stream_scrubber_handles_tokens_across_read_and_transfer_chunk_boundaries(
    chunked: bool,
) -> None:
    data = bytearray(b"stream-start|")
    for index, boundary in enumerate((4096, 8192, 16384, 65536, 131072)):
        data.extend(b"x" * (boundary - 5 - len(data)))
        data.extend(TOKEN.encode())
        data.extend(f"|raw-{index}|".encode())
        data.extend(BASIC_CREDENTIAL.encode())
        data.extend(f"|basic-{index}|".encode())
    data.extend(b"|stream-end")
    payload = bytes(data)
    # Transfer chunks intentionally split *inside* both credentials, not just between them.
    cuts = {0, len(payload)}
    for secret in (TOKEN.encode(), BASIC_CREDENTIAL.encode()):
        offset = 0
        while (offset := payload.find(secret, offset)) >= 0:
            cuts.add(offset + 3)
            cuts.add(offset + len(secret) - 2)
            offset += len(secret)
    ordered = sorted(cuts)
    pieces = tuple(payload[start:end] for start, end in pairwise(ordered))
    with (
        fake_upstream(
            lambda _: UpstreamReply(chunks=pieces, chunked=chunked)
        ) as upstream,
        running_broker(upstream.base_url) as broker,
    ):
        status, headers, body = request(
            broker.base_url, "GET", "/api/repos/acme/widgets"
        )
        assert status == 200
        assert body.startswith(b"stream-start|") and body.endswith(b"|stream-end")
        for index in range(5):
            assert f"|raw-{index}|".encode() in body
            assert f"|basic-{index}|".encode() in body
        assert "content-length" not in headers
        assert "transfer-encoding" not in headers
        assert_secret_free(body, headers, broker.audit.records(1))


def test_chunked_git_rpc_request_is_decoded_and_reframed_without_forwarding_trailers(
    tmp_path: Path,
) -> None:
    repository = seed_git_repository(tmp_path)
    pieces = [b"0014command=ls-refs\n", b"0001", b"0000"]
    with (
        git_http_upstream(repository) as upstream,
        running_broker(upstream.base_url) as broker,
    ):
        status, headers, body = request(
            broker.base_url,
            "POST",
            "/git/acme/widgets.git/git-upload-pack",
            body=iter(pieces),
            chunked=True,
            headers={
                "Content-Type": "application/x-git-upload-pack-request",
                "Git-Protocol": "version=2",
                "Trailer": "X-Untrusted",
                "X-Untrusted": "must-not-forward",
            },
        )
        assert (
            status == 200
            and headers["content-type"] == "application/x-git-upload-pack-result"
        )
        assert repository.revision.encode() + b" refs/heads/main\n" in body
        assert "trailer" not in upstream.requests[0].headers
        assert "x-untrusted" not in upstream.requests[0].headers


@pytest.mark.parametrize("relative", [False, True])
@pytest.mark.parametrize("surface", ["api", "git"])
def test_safe_redirect_is_rewritten_without_following_it(
    relative: bool, surface: str
) -> None:
    target = (
        "/api/repos/acme/widgets/pulls/42"
        if surface == "api"
        else "/git/acme/widgets.git/info/refs?service=git-upload-pack"
    )
    origin: list[str] = []

    def reply(item: UpstreamRequest) -> UpstreamReply:
        if "redirected=1" in item.target:
            return UpstreamReply(body=b"arrived")
        destination = target
        if surface == "api":
            destination += "?redirected=1"
        location = destination if relative else origin[0] + destination
        return UpstreamReply(
            status=302, headers=(("Location", location),), body=b"redirect body"
        )

    with fake_upstream(reply) as upstream:
        origin.append(upstream.base_url)
        with running_broker(upstream.base_url) as broker:
            status, headers, _ = request(broker.base_url, "GET", target)
            assert status == 302
            assert len(upstream.requests) == 1, (
                "broker must not follow upstream redirects"
            )
            location = headers["location"]
            parsed = urlsplit(location)
            assert (
                not parsed.netloc or parsed.netloc == urlsplit(broker.base_url).netloc
            )
            assert parsed.path == urlsplit(target).path
            assert_secret_free(headers)
            if surface == "api":
                assert (
                    request(broker.base_url, "GET", parsed.path + "?" + parsed.query)[2]
                    == b"arrived"
                )


@pytest.mark.parametrize(
    "destination",
    [
        "/api/repos/acme/other/pulls/1",
        "/api/user",
        "/api/graphql",
        "/api/repos/acme/widgets/../other",
        "//attacker.invalid/api/repos/acme/widgets",
        "https://attacker.invalid/api/repos/acme/widgets",
        "http://127.0.0.1:1/api/repos/acme/widgets",
        "https://api.github.com/repos/acme/widgets",
        "/git/acme/widgets.git/info/refs?service=git-receive-pack",
        "http://[broken",
        "http://127.0.0.1:not-a-port/api/repos/acme/widgets",
        "",
        None,
    ],
)
def test_unsafe_or_missing_redirect_location_is_denied(destination: str | None) -> None:
    def reply(_: UpstreamRequest) -> UpstreamReply:
        headers = () if destination is None else (("Location", destination),)
        return UpstreamReply(
            status=302, headers=headers, body=b"untrusted upstream detail"
        )

    with fake_upstream(reply) as upstream, running_broker(upstream.base_url) as broker:
        status, headers, body = request(
            broker.base_url, "GET", "/api/repos/acme/widgets"
        )
        assert status == 403
        assert "location" not in headers
        assert b"untrusted upstream detail" not in body
        assert len(upstream.requests) == 1
        audit = broker.audit.records(1)[0]
        assert audit["decision"] == "deny" and audit["upstream_status"] == 302


def test_redirect_to_other_loopback_upstream_does_not_receive_authority() -> None:
    with fake_upstream() as outsider:
        destination = outsider.base_url + "/api/repos/acme/widgets"
        with (
            fake_upstream(
                lambda _: UpstreamReply(
                    status=307, headers=(("Location", destination),)
                )
            ) as upstream,
            running_broker(upstream.base_url) as broker,
        ):
            assert request(broker.base_url, "GET", "/api/repos/acme/widgets")[0] == 403
        assert not outsider.requests


def test_allowed_redirect_location_is_scrubbed_including_encoded_basic_credential() -> (
    None
):
    location = f"/api/repos/acme/widgets/pulls?echo={TOKEN}&auth={BASIC_CREDENTIAL}"
    with (
        fake_upstream(
            lambda _: UpstreamReply(status=301, headers=(("Location", location),))
        ) as upstream,
        running_broker(upstream.base_url) as broker,
    ):
        status, headers, body = request(
            broker.base_url, "GET", "/api/repos/acme/widgets"
        )
        assert status == 301
        assert urlsplit(headers["location"]).path == "/api/repos/acme/widgets/pulls"
        assert_secret_free(headers, body, broker.audit.records(1))


@pytest.mark.parametrize(
    "method,target",
    [
        ("GET", "/api/user"),
        ("POST", "/api/repos/acme/widgets/pulls"),
        ("HEAD", "/api/repos/acme/other"),
        ("GET", "/git/acme/widgets.git/info/refs?service=git-receive-pack"),
        ("POST", "/git/acme/widgets.git/git-receive-pack"),
        ("GET", "/api/repos/acme/widgets/%2e%2e/other"),
    ],
)
def test_denials_are_generic_and_never_contact_upstream(
    method: str, target: str
) -> None:
    with fake_upstream() as upstream, running_broker(upstream.base_url) as broker:
        status, headers, body = request(broker.base_url, method, target)
        assert status == 403
        if method == "HEAD":
            assert body == b""
        else:
            assert b"github.com" not in body and b"127.0.0.1" not in body
        assert not upstream.requests
        audit = broker.audit.records(1)[0]
        assert audit["decision"] == "deny" and audit["upstream_status"] is None
        assert_secret_free(headers, body, audit)


@pytest.mark.parametrize("failure", ["refused", "disconnect"])
def test_upstream_transport_failure_is_bounded_and_does_not_leak_credentials(
    failure: str,
) -> None:
    if failure == "refused":
        # Retain a bound but non-listening socket so no other process can claim this port.
        with socket.socket() as unavailable:
            unavailable.bind(("127.0.0.1", 0))
            base = f"http://127.0.0.1:{unavailable.getsockname()[1]}"
            with running_broker(base) as broker:
                status, headers, body = request(
                    broker.base_url, "GET", "/api/repos/acme/widgets"
                )
                audit = broker.audit.records(1)[0]
    else:
        with (
            fake_upstream(lambda _: UpstreamReply(disconnect=True)) as upstream,
            running_broker(upstream.base_url) as broker,
        ):
            status, headers, body = request(
                broker.base_url, "GET", "/api/repos/acme/widgets"
            )
            audit = broker.audit.records(1)[0]
    assert status == 502
    assert audit["status"] == 502 and audit["upstream_status"] is None
    assert b"Traceback" not in body
    assert_secret_free(headers, body, audit)


def test_audit_json_fields_labels_and_untrusted_secret_echoes() -> None:
    data = f"PAT={TOKEN}; Basic {BASIC_CREDENTIAL}".encode()
    with (
        fake_upstream(lambda _: UpstreamReply(status=500, body=data)) as upstream,
        running_broker(upstream.base_url) as broker,
    ):
        calls = [
            ("GET", "/healthz", {}, 200, "allow", None),
            ("GET", "/api/user", {}, 403, "deny", None),
            (
                "GET",
                f"/api/repos/acme/widgets/commits/{TOKEN}",
                {"X-Ompire-Task": f"task-{TOKEN}-{BASIC_CREDENTIAL}"},
                500,
                "allow",
                500,
            ),
            (
                "HEAD",
                "/api/repos/acme/widgets",
                {"X-Ompire-Task": "forged-label"},
                500,
                "allow",
                500,
            ),
        ]
        for method, path, headers, expected, _, _ in calls:
            status, response_headers, body = request(
                broker.base_url, method, path, headers=headers
            )
            assert status == expected
            assert_secret_free(response_headers, body)
        records = broker.audit.records(len(calls))
        for record, (method, path, _, status, decision, upstream_status) in zip(
            records, calls, strict=True
        ):
            assert set(record) >= {
                "timestamp",
                "task",
                "method",
                "path",
                "decision",
                "rule",
                "upstream_status",
                "status",
            }
            assert record["timestamp"]
            assert record["method"] == method
            if TOKEN not in path:
                assert record["path"] == path
            assert record["status"] == status
            assert record["decision"] == decision
            assert isinstance(record["rule"], str) and record["rule"]
            assert record["upstream_status"] == upstream_status
        assert records[0]["task"] == records[1]["task"] == "unlabeled"
        assert records[2]["task"].startswith("task-")
        assert records[3]["task"] == "forged-label", (
            "task attribution is deliberately spoofable"
        )
        assert_secret_free(records, broker.audit.lines)


def isolated_git_environment(root: Path) -> dict[str, str]:
    """No inherited credentials, config, proxies, helpers, signing, or SSH transport."""
    home = root / "git-home"
    home.mkdir(exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / "config"),
        "LC_ALL": "C",
        "LANG": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "false",
        "SSH_ASKPASS": "false",
        "GIT_SSH_COMMAND": "false",
        "GIT_ALLOW_PROTOCOL": "file:http",
        "GIT_PROTOCOL_FROM_USER": "0",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }


def run_git(
    cwd: Path,
    *args: str,
    env: Mapping[str, str],
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "git",
            "-c",
            "protocol.version=2",
            "-c",
            "core.hooksPath=" + os.devnull,
            "-c",
            "commit.gpgSign=false",
            "-c",
            "tag.gpgSign=false",
            "-c",
            "credential.helper=",
            "-c",
            "credential.interactive=false",
            "-c",
            "user.name=Broker Test",
            "-c",
            "user.email=broker@example.invalid",
            "-c",
            "http.proxy=",
            "-c",
            "http.extraHeader=",
            "-c",
            "http.cookieFile=",
            "-c",
            "http.saveCookies=false",
            *args,
        ],
        cwd=cwd,
        env=dict(env),
        capture_output=True,
        text=True,
        timeout=25,
        check=check,
    )


@dataclass(frozen=True)
class GitFixture:
    projects: Path
    bare: Path
    seed: Path
    env: Mapping[str, str]
    revision: str


def seed_git_repository(root: Path) -> GitFixture:
    """Create acme/widgets.git with a real initial commit for tests/manual smoke."""
    env = isolated_git_environment(root)
    projects = root / "projects"
    bare = projects / "acme" / "widgets.git"
    bare.parent.mkdir(parents=True)
    seed = root / "seed"
    seed.mkdir()
    run_git(root, "init", "--bare", "--initial-branch=main", str(bare), env=env)
    run_git(seed, "init", "--initial-branch=main", env=env)
    (seed / "payload.txt").write_text("initial payload\n")
    run_git(seed, "add", "payload.txt", env=env)
    run_git(seed, "commit", "-m", "initial", env=env)
    run_git(seed, "remote", "add", "origin", str(bare), env=env)
    run_git(seed, "push", "origin", "main", env=env)
    # A proxy policy regression must not be masked by a read-only fake backend.
    run_git(bare, "config", "http.receivepack", "true", env=env)
    revision = run_git(seed, "rev-parse", "HEAD", env=env).stdout.strip()
    return GitFixture(projects, bare, seed, env, revision)


@contextmanager
def git_http_upstream(repository: GitFixture) -> Iterator[FakeUpstream]:
    """Bridge real smart-HTTP requests to git http-backend via its CGI contract."""

    def reply(item: UpstreamRequest) -> UpstreamReply:
        parsed = urlsplit(item.target)
        assert parsed.path.startswith("/git/")
        path = parsed.path.removeprefix("/git")
        if path.startswith("/acme/widgets/"):
            path = path.replace("/acme/widgets/", "/acme/widgets.git/", 1)
        env = {
            **repository.env,
            "GIT_PROJECT_ROOT": str(repository.projects),
            "GIT_HTTP_EXPORT_ALL": "1",
            "PATH_INFO": path,
            "QUERY_STRING": parsed.query,
            "REQUEST_METHOD": item.method,
            "CONTENT_TYPE": item.headers.get("content-type", ""),
            "CONTENT_LENGTH": str(len(item.body)),
            "SERVER_PROTOCOL": "HTTP/1.1",
            "SERVER_NAME": "127.0.0.1",
            "SERVER_PORT": "80",
            "REMOTE_ADDR": "127.0.0.1",
            "REMOTE_USER": "trusted-broker",
            "HTTP_GIT_PROTOCOL": item.headers.get("git-protocol", ""),
        }
        result = subprocess.run(
            ["git", "http-backend"],
            input=item.body,
            env=env,
            capture_output=True,
            timeout=20,
            check=True,
        )
        header_bytes, separator, body = result.stdout.partition(b"\r\n\r\n")
        if not separator:
            header_bytes, separator, body = result.stdout.partition(b"\n\n")
        assert separator, (
            f"invalid CGI response: {result.stdout!r}; stderr={result.stderr!r}"
        )
        headers = []
        status = 200
        for line in header_bytes.splitlines():
            name, value = line.decode("latin-1").split(":", 1)
            if name.lower() == "status":
                status = int(value.strip().split()[0])
            else:
                headers.append((name, value.strip()))
        return UpstreamReply(status=status, body=body, headers=tuple(headers))

    with fake_upstream(reply) as upstream:
        yield upstream


@pytest.mark.parametrize("suffix", ["", ".git"])
def test_real_git_v2_clone_ls_remote_fetch_and_denied_push(
    tmp_path: Path, suffix: str
) -> None:
    repository = seed_git_repository(tmp_path)
    with (
        git_http_upstream(repository) as upstream,
        running_broker(upstream.base_url) as broker,
    ):
        url = f"{broker.base_url}/git/acme/widgets{suffix}"
        advertised = run_git(
            tmp_path, "ls-remote", url, "refs/heads/main", env=repository.env
        )
        assert advertised.stdout.strip() == f"{repository.revision}\trefs/heads/main"
        checkout = tmp_path / "consumer"
        run_git(tmp_path, "clone", url, str(checkout), env=repository.env)
        assert (checkout / "payload.txt").read_text() == "initial payload\n"
        assert (
            run_git(checkout, "rev-parse", "HEAD", env=repository.env).stdout.strip()
            == repository.revision
        )
        (repository.seed / "payload.txt").write_text("updated upstream payload\n")
        run_git(repository.seed, "add", "payload.txt", env=repository.env)
        run_git(repository.seed, "commit", "-m", "upstream update", env=repository.env)
        run_git(repository.seed, "push", "origin", "main", env=repository.env)
        latest = run_git(
            repository.seed, "rev-parse", "HEAD", env=repository.env
        ).stdout.strip()
        run_git(checkout, "fetch", "origin", "main", env=repository.env)
        assert (
            run_git(
                checkout, "rev-parse", "FETCH_HEAD", env=repository.env
            ).stdout.strip()
            == latest
        )
        assert (
            run_git(
                checkout, "show", "FETCH_HEAD:payload.txt", env=repository.env
            ).stdout
            == "updated upstream payload\n"
        )
        run_git(checkout, "merge", "--ff-only", "origin/main", env=repository.env)
        (checkout / "payload.txt").write_text("sandbox must not publish\n")
        run_git(checkout, "add", "payload.txt", env=repository.env)
        run_git(checkout, "commit", "-m", "agent write attempt", env=repository.env)
        before_push = len(upstream.requests)
        push = run_git(
            checkout,
            "push",
            "origin",
            "HEAD:refs/heads/main",
            env=repository.env,
            check=False,
        )
        assert push.returncode != 0 and "403" in push.stderr
        assert len(upstream.requests) == before_push, (
            "push discovery must stop at the broker"
        )
        assert (
            run_git(
                repository.bare, "rev-parse", "main", env=repository.env
            ).stdout.strip()
            == latest
        )
        assert (
            request(
                broker.base_url,
                "POST",
                f"/git/acme/widgets{suffix}/git-receive-pack",
                body=b"0000",
            )[0]
            == 403
        )
        assert all("git-receive-pack" not in item.target for item in upstream.requests)
        discovery = [
            item
            for item in upstream.requests
            if "/info/refs?service=git-upload-pack" in item.target
        ]
        rpc = [
            item
            for item in upstream.requests
            if item.target.endswith("/git-upload-pack")
        ]
        assert discovery and rpc
        assert all(
            item.headers.get("git-protocol") == "version=2" for item in discovery + rpc
        )
        assert any(b"command=ls-refs" in item.body for item in rpc)
        assert any(b"command=fetch" in item.body for item in rpc)
        assert all(
            item.headers["authorization"] == f"Basic {BASIC_CREDENTIAL}"
            for item in upstream.requests
        )
        assert_secret_free(push.stdout, push.stderr, advertised.stdout)


@pytest.mark.parametrize("prefix", ["/%61pi", "/a%70i"])
def test_encoded_api_prefix_cannot_change_upstream_origin(prefix: str) -> None:
    decision = decide("GET", f"{prefix}/repos/acme/widgets", "", REPOS)
    if decision.allowed:
        upstream = urlsplit(decision.upstream_url)
        assert upstream.scheme == "https"
        assert upstream.hostname == "api.github.com" and upstream.port is None
        assert upstream.username is None and upstream.password is None
        assert upstream.path == "/repos/acme/widgets"
    else:
        assert decision.upstream_url is None


def test_unknown_method_is_denied_with_task_attribution() -> None:
    with fake_upstream() as upstream, running_broker(upstream.base_url) as broker:
        status, headers, body = request(
            broker.base_url,
            "BREW",
            "/api/repos/acme/widgets",
            headers={"X-Ompire-Task": "unknown-method-task"},
        )
        assert status == 403 and not upstream.requests
        audit = broker.audit.records(1)[0]
        assert audit["method"] == "BREW" and audit["task"] == "unknown-method-task"
        assert audit["decision"] == "deny" and audit["upstream_status"] is None
        assert_secret_free(headers, body, audit)


def test_folded_allowlisted_response_headers_cannot_inject_new_headers() -> None:
    folded = (
        ("Content-Type", f"text/plain\r\n X-Injected: {TOKEN}"),
        ("Cache-Control", f"private\r\n Set-Cookie: secret={TOKEN}"),
        ("X-GitHub-Request-Id", f"request\r\n Authorization: Basic {BASIC_CREDENTIAL}"),
    )
    with (
        fake_upstream(
            lambda _: UpstreamReply(headers=folded, body=b"safe body")
        ) as upstream,
        running_broker(upstream.base_url) as broker,
    ):
        status, headers, body = request(
            broker.base_url, "GET", "/api/repos/acme/widgets"
        )
        assert status == 200 and body == b"safe body"
        assert not ({"x-injected", "set-cookie", "authorization"} & headers.keys())
        assert all(
            "\r" not in value and "\n" not in value for value in headers.values()
        )
        assert_secret_free(headers, body, broker.audit.records(1))
