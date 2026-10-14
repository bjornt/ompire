"""Standalone trusted-side forge capability broker (ADR-0041).

Run directly with Python 3.12+ or with ``python -m ompire_daemon.forge_broker``.
"""

from __future__ import annotations

import argparse
import base64
import http.client
import json
import logging
import os
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NoReturn
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit, urlunsplit

TOKEN_ENV = "OMPIRE_FORGE_TOKEN"
CHUNK_SIZE = 65536
REQUEST_HEADERS = frozenset({"accept", "content-type", "user-agent", "git-protocol"})
RESPONSE_HEADERS = frozenset(
    {"content-type", "cache-control", "expires", "pragma", "x-github-request-id"}
)
_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+\Z")


@dataclass(frozen=True)
class Decision:
    allowed: bool
    rule: str
    upstream_url: str | None = None


def _repository(value: str) -> str:
    normalized = value.removesuffix(".git")
    if not _REPO.fullmatch(normalized) or normalized.split("/")[1] in {".", ".."}:
        raise ValueError("--repo must name an owner/repository")
    return normalized.casefold()


def decide(method: str, path: str, query: str, repos: frozenset[str]) -> Decision:
    """Apply policy before translating or contacting an upstream (ADR-0041)."""
    decoded = unquote(path)
    if (
        not path.startswith("/")
        or path.startswith("//")
        or any(c in decoded for c in "\\%?#")
        or any(ord(c) < 32 or ord(c) == 127 for c in decoded + query)
        or any(part in {".", "..", ""} for part in decoded.split("/")[1:-1])
        or decoded.split("/")[-1] in {".", ".."}
        or decoded.count("/") != path.count("/")
    ):
        return Decision(False, "invalid-path")
    if decoded == "/healthz" and not query:
        return Decision(method in {"GET", "HEAD"}, "health")
    parts = decoded.split("/")
    allowed_repos = frozenset(repo.casefold() for repo in repos)
    if len(parts) >= 5 and parts[1:3] == ["api", "repos"]:
        if method not in {"GET", "HEAD"}:
            return Decision(False, "api-write")
        repo = "/".join(parts[3:5]).casefold()
        if repo not in allowed_repos:
            return Decision(False, "repository-denied")
        api_path = "/" + "/".join(path.split("/")[2:])
        return Decision(
            True,
            "api-read",
            "https://api.github.com" + api_path + ("?" + query if query else ""),
        )
    if len(parts) >= 4 and parts[1] == "git":
        repo = f"{parts[2]}/{parts[3].removesuffix('.git')}".casefold()
        if repo not in allowed_repos:
            return Decision(False, "repository-denied")
        tail = "/".join(parts[4:])
        if tail == "info/refs" and method in {"GET", "HEAD"}:
            allowed = parse_qsl(query, keep_blank_values=True) == [
                ("service", "git-upload-pack")
            ]
        else:
            allowed = tail == "git-upload-pack" and method == "POST" and not query
        if not allowed:
            return Decision(False, "git-service-denied")
        # GitHub's smart HTTP endpoint uses the .git form, regardless of the client URL.
        upstream = (
            f"https://github.com/{parts[2]}/{parts[3].removesuffix('.git')}.git/{tail}"
        )
        return Decision(
            True, "git-upload-pack", upstream + ("?" + query if query else "")
        )
    return Decision(False, "path-denied")


class _Redactor:
    def __init__(self, token: str) -> None:
        values = {token.encode(), base64.b64encode(f"x-access-token:{token}".encode())}
        self.pattern = re.compile(
            b"|".join(
                re.escape(value) for value in sorted(values, key=len, reverse=True)
            )
        )
        self.overlap = max(map(len, values)) - 1

    def scrub_bytes(self, value: bytes) -> bytes:
        return self.pattern.sub(b"[REDACTED]", value)

    def text(self, value: str) -> str:
        return self.scrub_bytes(value.encode("utf-8", "replace")).decode(
            "utf-8", "replace"
        )

    def stream(self, chunks: Iterator[bytes]) -> Iterator[bytes]:
        pending = b""
        for chunk in chunks:
            pending += chunk
            end = max(0, len(pending) - self.overlap)
            # A match beginning before the safe boundary must be emitted whole.
            for match in self.pattern.finditer(pending):
                if match.start() >= end:
                    break
                end = max(end, match.end())
            if end:
                yield self.scrub_bytes(pending[:end])
                pending = pending[end:]
        if pending:
            yield self.scrub_bytes(pending)


def _open_upstream(url: str) -> http.client.HTTPConnection:
    parsed = urlsplit(url)
    connection = (
        http.client.HTTPSConnection
        if parsed.scheme == "https"
        else http.client.HTTPConnection
    )
    return connection(parsed.hostname or "", parsed.port, timeout=30)


class ForgeBroker(ThreadingHTTPServer):
    """One stateless localhost listener; each request owns its upstream connection."""

    daemon_threads = True

    def __init__(
        self,
        token: str,
        repos: frozenset[str],
        *,
        port: int = 4100,
        bind: str = "127.0.0.1",
        upstream_base: str | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        if (
            not token
            or not token.isascii()
            or any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in token)
        ):
            raise ValueError("token must be non-empty ASCII without whitespace")
        self.redactor = _Redactor(token)
        if bind not in {"127.0.0.1", "localhost"}:
            raise ValueError("--bind must be localhost (127.0.0.1)")
        if not 0 <= port <= 65535:
            raise ValueError("--port must be between 0 and 65535")
        if not repos:
            raise ValueError("at least one --repo is required")
        self.repos = frozenset(_repository(repo) for repo in repos)
        if upstream_base is not None:
            parsed = urlsplit(upstream_base)
            if (
                parsed.scheme != "http"
                or parsed.hostname not in {"127.0.0.1", "localhost"}
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("--upstream-base must be a loopback HTTP origin")
            # Validate port before opening the listener.
            _ = parsed.port
        self.upstream_base = upstream_base.rstrip("/") if upstream_base else None
        self.api_auth = f"Bearer {token}"
        self.git_auth = (
            "Basic " + base64.b64encode(f"x-access-token:{token}".encode()).decode()
        )
        self.logger = (
            logger if logger is not None else logging.getLogger("forge-broker")
        )
        super().__init__(("127.0.0.1", port), _Handler)

    def audit(
        self,
        task: str,
        method: str,
        path: str,
        decision: Decision,
        upstream_status: int | None,
        status: int,
    ) -> None:
        line = {
            "timestamp": datetime.now(UTC).isoformat(),
            "task": self.redactor.text(task),
            "method": self.redactor.text(method),
            "path": self.redactor.text(path),
            "decision": "allow" if decision.allowed else "deny",
            "rule": decision.rule,
            "upstream_status": upstream_status,
            "status": status,
        }
        # Scrub before JSON escaping too: non-ASCII/control input cannot hide a secret.
        self.logger.info(self.redactor.text(json.dumps(line, ensure_ascii=True)))

    def handle_error(self, request: object, client_address: object) -> None:
        # Never let socketserver print an exception/traceback containing credentials.
        self.logger.error(self.redactor.text("forge broker connection failed"))


class _InvalidBody(ValueError):
    pass


class _Handler(BaseHTTPRequestHandler):
    server: ForgeBroker

    def log_message(self, format: str, *args: object) -> None:
        pass

    def send_error(
        self, code: int, message: str | None = None, explain: str | None = None
    ) -> None:
        status = 403 if code == 501 else code
        headers = getattr(self, "headers", None)
        task = headers.get("X-Ompire-Task") if headers is not None else None
        try:
            self._reply(status, b"request refused\n")
        finally:
            self.server.audit(
                task or "unlabeled",
                getattr(self, "command", "") or "",
                getattr(self, "path", "") or "",
                Decision(False, "invalid-request"),
                None,
                status,
            )

    def _reply(self, status: int, body: bytes) -> None:
        self.send_response_only(status)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        if getattr(self, "command", "") != "HEAD":
            self.wfile.write(self.server.redactor.scrub_bytes(body))
        self.close_connection = True

    def _framing(self) -> tuple[int | None, bool]:
        lengths = self.headers.get_all("Content-Length", [])
        encodings = self.headers.get_all("Transfer-Encoding", [])
        if encodings:
            if lengths or len(encodings) != 1 or encodings[0].lower() != "chunked":
                raise _InvalidBody
            return None, True
        if lengths:
            if len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0]):
                raise _InvalidBody
            return int(lengths[0]), False
        return 0, False

    def _body(self, length: int | None, chunked: bool) -> Iterator[bytes]:
        if not chunked:
            remaining = length or 0
            while remaining:
                chunk = self.rfile.read(min(remaining, CHUNK_SIZE))
                if not chunk:
                    raise _InvalidBody
                remaining -= len(chunk)
                yield chunk
            return
        while True:
            line = self.rfile.readline(8193)
            size_text = line.split(b";", 1)[0].strip()
            if (
                not line.endswith(b"\r\n")
                or len(line) > 8192
                or not re.fullmatch(rb"[0-9a-fA-F]+", size_text)
            ):
                raise _InvalidBody
            size = int(size_text, 16)
            if size == 0:
                while True:
                    trailer = self.rfile.readline(8193)
                    if trailer == b"\r\n":
                        return
                    if not trailer.endswith(b"\r\n") or len(trailer) > 8192:
                        raise _InvalidBody
            while size:
                chunk = self.rfile.read(min(size, CHUNK_SIZE))
                if not chunk:
                    raise _InvalidBody
                size -= len(chunk)
                yield chunk
            if self.rfile.read(2) != b"\r\n":
                raise _InvalidBody

    def _redirect(self, url: str, location: str) -> str | None:
        if any(ord(c) < 32 or ord(c) == 127 for c in location) or "\\" in location:
            return None
        target = urlsplit(urljoin(url, location))
        origin = urlsplit(url)
        if (
            (target.scheme, target.hostname, target.port)
            != (origin.scheme, origin.hostname, origin.port)
            or target.username is not None
            or target.password is not None
            or target.fragment
        ):
            return None
        if self.server.upstream_base:
            path = target.path
        elif origin.hostname == "api.github.com":
            path = "/api" + target.path
        else:
            path = "/git" + target.path
        translated = path + ("?" + target.query if target.query else "")
        # Recheck the actual relayed (scrubbed) destination, never follow upstream.
        translated = self.server.redactor.text(translated)
        parsed = urlsplit(translated)
        if not decide(
            self.command, parsed.path, parsed.query, self.server.repos
        ).allowed:
            return None
        return translated

    def _handle(self) -> None:
        self.close_connection = True
        self.connection.settimeout(30)
        upstream_status: int | None = None
        status = 403
        decision = Decision(False, "invalid-path")
        task = self.headers.get("X-Ompire-Task") or "unlabeled"
        connection: http.client.HTTPConnection | None = None
        sent = False
        try:
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc or parsed.fragment:
                self._reply(403, b"forbidden\n")
                return
            decision = decide(
                self.command, parsed.path, parsed.query, self.server.repos
            )
            if not decision.allowed:
                self._reply(403, b"forbidden\n")
                return
            if decision.rule == "health":
                status = 200
                self._reply(status, b"ok\n")
                return
            length, chunked = self._framing()
            headers = {
                name: value
                for name, value in self.headers.items()
                if name.lower() in REQUEST_HEADERS
            }
            headers["Authorization"] = (
                self.server.git_auth
                if decision.rule == "git-upload-pack"
                else self.server.api_auth
            )
            if not chunked:
                headers["Content-Length"] = str(length)
            url = decision.upstream_url or ""
            if self.server.upstream_base:
                url = self.server.upstream_base + self.path
            connection = _open_upstream(url)
            upstream = urlsplit(url)
            connection.request(
                self.command,
                urlunsplit(("", "", upstream.path, upstream.query, "")),
                body=self._body(length, chunked),
                headers=headers,
                encode_chunked=chunked,
            )
            response = connection.getresponse()
            upstream_status = response.status
            location = None
            if 300 <= response.status < 400:
                try:
                    location = (
                        self._redirect(url, response.getheader("Location", ""))
                        if response.getheader("Location")
                        else None
                    )
                except ValueError:
                    location = None
                if location is None:
                    decision = Decision(False, "redirect-denied")
                    self._reply(403, b"forbidden\n")
                    return
            status = response.status
            self.send_response_only(status)
            for name, value in response.getheaders():
                if name.lower() in RESPONSE_HEADERS or name.lower().startswith(
                    "x-ratelimit-"
                ):
                    # Folded upstream headers must not introduce additional headers.
                    clean_name = (
                        self.server.redactor.text(name)
                        .replace("\r", "")
                        .replace("\n", "")
                    )
                    clean_value = (
                        self.server.redactor.text(value)
                        .replace("\r", "")
                        .replace("\n", "")
                    )
                    self.send_header(clean_name, clean_value)
            if location is not None:
                self.send_header("Location", location)
            self.end_headers()
            sent = True
            if self.command != "HEAD":

                def chunks() -> Iterator[bytes]:
                    while data := response.read1(CHUNK_SIZE):
                        yield data

                for chunk in self.server.redactor.stream(chunks()):
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except _InvalidBody:
            decision = Decision(False, "invalid-body")
            status = 403
            if not sent:
                self._reply(status, b"forbidden\n")
        except (OSError, ValueError, http.client.HTTPException):
            if not sent:
                status = 502
                self._reply(status, b"upstream unavailable\n")
        finally:
            if connection is not None:
                connection.close()
            self.server.audit(
                task, self.command, self.path, decision, upstream_status, status
            )

    do_GET = _handle
    do_HEAD = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle
    do_OPTIONS = _handle
    do_TRACE = _handle
    do_CONNECT = _handle


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # argparse errors can quote arbitrary argv; never echo their values.
        self.exit(2, "forge broker: invalid command-line arguments (see --help)\n")


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument(
        "--token-file", type=Path, help=f"PAT file (exclusive with {TOKEN_ENV})"
    )
    parser.add_argument(
        "--repo",
        action="append",
        default=[],
        help="allowlisted owner/repository (repeatable)",
    )
    parser.add_argument("--port", type=int, default=4100)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--upstream-base", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    env_token = os.environ.get(TOKEN_ENV)
    if (args.token_file is not None) == (env_token is not None):
        print(
            f"forge broker: exactly one of --token-file or {TOKEN_ENV} is required",
            file=sys.stderr,
        )
        return 2
    try:
        token = (
            args.token_file.read_text().strip()
            if args.token_file is not None
            else (env_token or "")
        )
    except (OSError, UnicodeError):
        print("forge broker: cannot read --token-file", file=sys.stderr)
        return 2
    if (
        not token
        or not token.isascii()
        or any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in token)
    ):
        print(
            "forge broker: token must be non-empty ASCII without whitespace",
            file=sys.stderr,
        )
        return 2
    redactor = _Redactor(token)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        server = ForgeBroker(
            token,
            frozenset(args.repo),
            port=args.port,
            bind=args.bind,
            upstream_base=args.upstream_base,
        )
    except ValueError as exc:
        print(redactor.text(f"forge broker: {exc}"), file=sys.stderr)
        return 2
    except OSError:
        print(
            redactor.text("forge broker: cannot bind localhost listener"),
            file=sys.stderr,
        )
        return 2
    print(
        redactor.text(
            f"forge broker ready http://127.0.0.1:{server.server_address[1]}"
        ),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
