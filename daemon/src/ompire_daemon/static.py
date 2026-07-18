"""Static file serving: built frontend when present, placeholder status page
otherwise. Must be mounted after API routers so /api/* takes precedence.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

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


def mount_frontend(app: FastAPI, frontend_dist: Path = DEFAULT_FRONTEND_DIST) -> None:
    if frontend_dist.is_dir():
        app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
        return

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def placeholder() -> str:
        return _PLACEHOLDER_HTML
