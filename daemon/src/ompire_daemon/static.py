"""Static file serving: built frontend when present, placeholder status page
otherwise. Must be mounted after API routers so /api/* takes precedence.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DEFAULT_FRONTEND_DIST = _REPO_ROOT / "frontend" / "dist"

_PLACEHOLDER_HTML = """<!doctype html>
<html>
<head><meta charset="utf-8"><title>ompire</title></head>
<body>
<h1>ompire daemon is running</h1>
<p>No frontend build found at frontend/dist/. This placeholder will be
replaced once the frontend ships.</p>
</body>
</html>
"""


class _SPAStaticFiles(StaticFiles):
    """Serves built files and directory indexes normally. On an unmatched
    GET whose path is outside the `/api` namespace, falls back to
    `index.html` so React Router can resolve client-side routes on a hard
    reload or direct navigation (design D-1..D-3). Non-GET requests and the
    `/api` namespace keep Starlette/FastAPI's normal 404/405 responses.
    """

    async def get_response(self, path: str, scope: dict[str, Any]) -> Response:
        try:
            return await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404 or scope["method"] != "GET":
                raise
            request_path = scope["path"]
            if request_path == "/api" or request_path.startswith("/api/"):
                raise
            return await super().get_response("index.html", scope)


def mount_frontend(app: FastAPI, frontend_dist: Path = DEFAULT_FRONTEND_DIST) -> None:
    if frontend_dist.is_dir():
        app.mount("/", _SPAStaticFiles(directory=frontend_dist, html=True), name="frontend")
        return

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def placeholder() -> str:
        return _PLACEHOLDER_HTML
