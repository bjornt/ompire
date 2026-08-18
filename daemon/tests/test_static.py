from pathlib import Path

import pytest
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


@pytest.fixture
def spa_client(tmp_path: Path) -> TestClient:
    """A daemon serving a built frontend with one real asset, for SPA
    fallback coverage."""
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<h1>ompire frontend</h1>")
    assets = dist / "assets"
    assets.mkdir()
    (assets / "app.js").write_text("console.log('app');")

    config = Config(
        data_dir=tmp_path / "data",
        task_dir_root=tmp_path / "tasks",
        checkout_root=tmp_path / "proj",
    )
    app = create_app(config, frontend_dist=dist)
    return TestClient(app)


def test_existing_static_asset_wins_over_fallback(spa_client: TestClient) -> None:
    response = spa_client.get("/assets/app.js")
    assert response.status_code == 200
    assert "console.log" in response.text


@pytest.mark.parametrize("path", ["/projects", "/settings"])
def test_direct_client_route_receives_spa_entry_point(spa_client: TestClient, path: str) -> None:
    response = spa_client.get(path)
    assert response.status_code == 200
    assert "ompire frontend" in response.text
    assert "text/html" in response.headers["content-type"]


@pytest.mark.parametrize("path", ["/tasks/42", "/ship/42"])
def test_nested_client_route_receives_spa_entry_point(spa_client: TestClient, path: str) -> None:
    response = spa_client.get(path)
    assert response.status_code == 200
    assert "ompire frontend" in response.text


def test_similar_non_api_prefix_remains_eligible(spa_client: TestClient) -> None:
    response = spa_client.get("/apiary")
    assert response.status_code == 200
    assert "ompire frontend" in response.text


def test_unknown_api_path_stays_json_404(spa_client: TestClient) -> None:
    response = spa_client.get("/api/missing")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_registered_api_route_still_takes_precedence(spa_client: TestClient) -> None:
    response = spa_client.get("/api/projects")
    assert response.status_code == 401


def test_non_get_request_does_not_receive_spa(spa_client: TestClient) -> None:
    response = spa_client.post("/projects")
    assert response.status_code == 405
    assert "ompire frontend" not in response.text


def test_deep_link_without_frontend_build_stays_missing(client: TestClient) -> None:
    response = client.get("/projects")
    assert response.status_code == 404
