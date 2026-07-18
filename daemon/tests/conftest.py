from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ompire_daemon.app import create_app
from ompire_daemon.config import Config


@pytest.fixture
def daemon_config(tmp_path: Path) -> Config:
    return Config(
        data_dir=tmp_path / "data",
        task_dir_root=tmp_path / "tasks",
        checkout_root=tmp_path / "proj",
    )


@pytest.fixture
def app(daemon_config: Config):
    return create_app(daemon_config)


@pytest.fixture
def auth_token(app) -> str:
    return app.state.auth_token


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture
def auth_headers(auth_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth_token}"}
