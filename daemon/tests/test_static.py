from pathlib import Path

from fastapi.testclient import TestClient

from ompire_daemon.app import create_app
from ompire_daemon.config import Config


def test_placeholder_page_when_no_frontend_build(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "ompire daemon is running" in response.text


def test_serves_built_frontend_when_present(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<h1>ompire frontend</h1>")

    config = Config(
        data_dir=tmp_path / "data",
        task_dir_root=tmp_path / "tasks",
        checkout_root=tmp_path / "proj",
    )
    app = create_app(config, frontend_dist=dist)
    client = TestClient(app)

    response = client.get("/")
    assert response.status_code == 200
    assert "ompire frontend" in response.text


def test_api_routes_take_precedence_over_static(client: TestClient) -> None:
    response = client.get("/api/projects")
    assert response.status_code == 401
