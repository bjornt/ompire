"""Bearer-token auth: first-run token generation, REST dependency, WS check.

Architecture: ADR-0002 (docs/adr/0002-run-as-local-daemon-with-stateless-web-ui.md)
"""

from __future__ import annotations

import contextlib
import os
import secrets
from pathlib import Path

from fastapi import HTTPException, Request, WebSocket, status

TOKEN_FILENAME = "token"


def token_path_for(data_dir: Path) -> Path:
    return data_dir / TOKEN_FILENAME


def write_token_file(path: Path, token: str, *, exclusive: bool = False) -> None:
    """Atomically write `token` to `path` with owner-only (0600) mode.

    Creates a sibling temp file in the same directory, then uses `os.replace`
    so the visible path is never in a partially-written state. The temp file
    is created with mode 0600; `os.replace` preserves the destination's mode
    on some systems, so the temp is set restrictively explicitly.

    `exclusive=True` adds `O_EXCL` (fail if the final path already exists),
    suitable for first-run creation. Rotation uses the default non-exclusive
    behaviour.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if exclusive:
        flags |= os.O_EXCL
    fd = os.open(tmp_path, flags, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(token)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except Exception:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def load_or_create_token(data_dir: Path) -> str:
    """Return the daemon's auth token, generating it on first run.

    The token file is created atomically with mode 0600 so it is never
    briefly world-readable between creation and permission-tightening.
    """
    path = token_path_for(data_dir)
    if path.exists():
        return path.read_text().strip()

    token = secrets.token_urlsafe(32)
    try:
        write_token_file(path, token, exclusive=True)
    except FileExistsError:
        # Lost a first-run race to another process; use what it wrote.
        return path.read_text().strip()
    return token


def require_bearer_token(request: Request) -> None:
    """FastAPI dependency: enforce `Authorization: Bearer <token>` on REST routes."""
    expected = request.app.state.auth_token
    scheme, _, presented = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not presented or not secrets.compare_digest(presented, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid or missing bearer token")


def check_ws_token(websocket: WebSocket) -> bool:
    """Return whether the WebSocket upgrade request carries a valid `token` query param."""
    expected = websocket.app.state.auth_token
    presented = websocket.query_params.get("token")
    return presented is not None and secrets.compare_digest(presented, expected)
