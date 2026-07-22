"""FastAPI app wiring: config, migrations, auth, routers, static serving."""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI

from ompire_daemon.advisories import AdvisorySampler
from ompire_daemon.agent import AgentSupervisor
from ompire_daemon.api.rest import router as api_router
from ompire_daemon.api.ws import router as ws_router
from ompire_daemon.auth import load_or_create_token
from ompire_daemon.config import Config
from ompire_daemon.db import db_path_for, make_engine
from ompire_daemon.events import EventHub
from ompire_daemon.migrate import upgrade_head
from ompire_daemon.notifications import AttentionNotifier
from ompire_daemon.recovery import classify_startup_tasks, run_recovery
from ompire_daemon.sessions import SessionTracker
from ompire_daemon.static import DEFAULT_FRONTEND_DIST, mount_frontend


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    notifier: AttentionNotifier = app.state.notifications
    advisories: AdvisorySampler = app.state.advisories
    agents: AgentSupervisor = app.state.agents
    await notifier.probe()
    notifier.start()
    advisories.start()
    # Slow (real container-side omp startups): runs in the background so it
    # never blocks serving (crash-recovery capability, design D-4/7.3). The
    # fast, must-finish-before-the-first-snapshot classification already ran
    # synchronously in `create_app`.
    recovery_job = asyncio.create_task(
        run_recovery(
            app.state.engine,
            app.state.events,
            app.state.config,
            agents,
            app.state.sessions,
            app.state.recoverable_tasks,
        )
    )
    try:
        yield
    finally:
        recovery_job.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await recovery_job
        await notifier.stop()
        await advisories.stop()
        await agents.shutdown()


def create_app(config: Config, *, frontend_dist: Path = DEFAULT_FRONTEND_DIST) -> FastAPI:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_path_for(config.data_dir)
    upgrade_head(db_path)

    app = FastAPI(title="ompire-daemon", lifespan=_lifespan)
    app.state.config = config
    app.state.engine = make_engine(db_path)
    app.state.auth_token = load_or_create_token(config.data_dir)
    app.state.events = EventHub()
    app.state.spawn_jobs = set()
    app.state.sessions = SessionTracker(
        app.state.events, config.session_idle_debounce, config.stall_threshold
    )
    app.state.agents = AgentSupervisor(config, app.state.events, app.state.sessions)
    app.state.notifications = AttentionNotifier(
        app.state.events,
        bind=config.bind,
        port=config.port,
        renotify_interval=config.renotify_interval,
        enabled=config.notifications_enabled,
    )
    app.state.advisories = AdvisorySampler(
        app.state.events,
        stats_throttle_interval=config.stats_throttle_interval,
        context_advisory_threshold=config.context_advisory_threshold,
    )
    app.state.advisories.register(app.state.sessions)

    # Before any snapshot is served: classify every live task per the startup
    # reconciliation matrix (crash-recovery capability, design D-4) — spawns
    # interrupted by a daemon death, tasks with no recorded session, and
    # tasks whose container is gone are all failed here; the rest are seeded
    # `starting` and handed to the lifespan's background recovery job. No
    # event loop is running yet at this point in `create_app`, hence `run`.
    app.state.recoverable_tasks = asyncio.run(
        classify_startup_tasks(app.state.engine, app.state.events, app.state.sessions)
    )

    app.include_router(api_router)
    app.include_router(ws_router)
    mount_frontend(app, frontend_dist)

    return app
