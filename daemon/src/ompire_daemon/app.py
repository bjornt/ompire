"""FastAPI app wiring: config, migrations, auth, routers, static serving."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from ompire_daemon.api.rest import router as api_router
from ompire_daemon.api.ws import router as ws_router
from ompire_daemon.auth import load_or_create_token
from ompire_daemon.config import Config
from ompire_daemon.db import db_path_for, make_engine
from ompire_daemon.events import EventHub
from ompire_daemon.migrate import upgrade_head
from ompire_daemon.registry.tasks import reconcile_interrupted_spawns
from ompire_daemon.static import DEFAULT_FRONTEND_DIST, mount_frontend


def create_app(config: Config, *, frontend_dist: Path = DEFAULT_FRONTEND_DIST) -> FastAPI:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_path_for(config.data_dir)
    upgrade_head(db_path)

    app = FastAPI(title="ompire-daemon")
    app.state.config = config
    app.state.engine = make_engine(db_path)
    app.state.auth_token = load_or_create_token(config.data_dir)
    app.state.events = EventHub()
    app.state.spawn_jobs = set()

    # Before any snapshot is served: spawns interrupted by a daemon death are dead.
    reconcile_interrupted_spawns(app.state.engine)

    app.include_router(api_router)
    app.include_router(ws_router)
    mount_frontend(app, frontend_dist)

    return app
