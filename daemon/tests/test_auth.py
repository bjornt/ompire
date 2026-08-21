"""Tests for the auth dependency/check in isolation, against a minimal
throwaway app. The real REST/WS routers (tasks 4.2, 5.2) wire in these same
`require_bearer_token` / `check_ws_token` functions.
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ompire_daemon.auth import check_ws_token, require_bearer_token

EXPECTED_TOKEN = "expected-token-value"


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.state.auth_token = EXPECTED_TOKEN

    @app.get("/api/protected", dependencies=[Depends(require_bearer_token)])
    def protected() -> dict[str, bool]:
        return {"ok": True}

    @app.websocket("/ws-protected")
    async def ws_protected(websocket: WebSocket) -> None:
        if not check_ws_token(websocket):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        await websocket.send_text("ok")

    return TestClient(app)


def test_missing_token_returns_401(client: TestClient) -> None:
    response = client.get("/api/protected")
    assert response.status_code == 401


def test_wrong_token_returns_401(client: TestClient) -> None:
    response = client.get("/api/protected", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401


def test_malformed_scheme_returns_401(client: TestClient) -> None:
    response = client.get("/api/protected", headers={"Authorization": EXPECTED_TOKEN})
    assert response.status_code == 401


def test_correct_token_returns_200(client: TestClient) -> None:
    response = client.get("/api/protected", headers={"Authorization": f"Bearer {EXPECTED_TOKEN}"})
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_ws_missing_token_refused(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws-protected"):
        pass


def test_ws_wrong_token_refused(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws-protected?token=wrong"):
        pass


def test_ws_correct_token_accepted(client: TestClient) -> None:
    with client.websocket_connect(f"/ws-protected?token={EXPECTED_TOKEN}") as ws:
        assert ws.receive_text() == "ok"
