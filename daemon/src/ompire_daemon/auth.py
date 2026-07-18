"""Bearer-token auth: first-run token generation, REST dependency, WS check."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from fastapi import HTTPException, Request, WebSocket, status

TOKEN_FILENAME = "token"


def token_path_for(data_dir: Path) -> Path:
    return data_dir / TOKEN_FILENAME


def load_or_create_token(data_dir: Path) -> str:
    """Return the daemon's auth token, generating it on first run.

    The token file is created atomically with mode 0600 so it is never
    briefly world-readable between creation and permission-tightening.
    """
    path = token_path_for(data_dir)
    if path.exists():
        return path.read_text().strip()

    data_dir.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        # Lost a first-run race to another process; use what it wrote.
        return path.read_text().strip()
    with os.fdopen(fd, "w") as f:
        f.write(token)
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
