"""FastAPI app wiring: config, migrations, auth, routers, static serving.

Architecture: ADR-0002 (docs/adr/0002-run-as-local-daemon-with-stateless-web-ui.md)
Trusted control-plane language: ADR-0003
(docs/adr/0003-implement-trusted-control-plane-in-python.md)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from sqlalchemy import Engine

from ompire_daemon.advisories import AdvisorySampler
from ompire_daemon.agent import AgentSupervisor
from ompire_daemon.api.rest import router as api_router
from ompire_daemon.api.ws import router as ws_router
from ompire_daemon.auth import load_or_create_token
from ompire_daemon.config import DEFAULT_CONFIG_PATH, Config
from ompire_daemon.db import db_path_for, ensure_db_dir, make_engine
from ompire_daemon.events import EventHub
from ompire_daemon.gpg import GpgProbe
from ompire_daemon.migrate import upgrade_head
from ompire_daemon.notifications import AttentionNotifier
from ompire_daemon.prwatch import PrWatcher
from ompire_daemon.recovery import classify_startup_tasks, run_recovery
from ompire_daemon.registry.settings import SettingsStore
from ompire_daemon.registry.tasks import list_tasks
from ompire_daemon.review import ReviewManager
from ompire_daemon.sessions import SessionTracker
from ompire_daemon.ship import ShipManager
from ompire_daemon.static import DEFAULT_FRONTEND_DIST, mount_frontend
from ompire_daemon.workflows import WorkflowRunner

logger = logging.getLogger(__name__)


def _chmod_db_private(db_path: Path) -> None:
    """Restrict the database directory and files to the owner only."""
    try:
        db_path.parent.chmod(0o700)
        db_path.chmod(0o600)
        for extra in db_path.parent.glob(f"{db_path.name}-*"):
            extra.chmod(0o600)
    except OSError as exc:
        logger.warning("failed to tighten permissions on data store: %s", exc)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    notifier: AttentionNotifier = app.state.notifications
    advisories: AdvisorySampler = app.state.advisories
    agents: AgentSupervisor = app.state.agents
    reviews: ReviewManager = app.state.reviews
    prwatch: PrWatcher = app.state.prwatch
    gpg: GpgProbe = app.state.gpg
    await notifier.probe()
    notifier.start()
    advisories.start()
    reviews.start()
    prwatch.start()
    # Prime the shared GPG lock condition before the first snapshot.
    await gpg.probe()
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
            app.state.workflow_runner,
            app.state.recoverable_tasks,
        )
    )
    try:
        yield
    finally:
        recovery_job.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await recovery_job
        background_jobs = list(app.state.spawn_jobs)
        for job in background_jobs:
            job.cancel()
        if background_jobs:
            await asyncio.gather(*background_jobs, return_exceptions=True)
        await notifier.stop()
        await advisories.stop()
        await prwatch.stop()
        # Cancel in-memory workflow runs before terminating agents: a run
        # cancelled mid-step leaves its record `running` for the next
        # startup's recovery (workflow-engine design D-6).
        await app.state.workflow_runner.shutdown()
        await agents.shutdown()
        await reviews.shutdown()


async def _prepare_startup(
    engine: Engine,
    config: Config,
    events: EventHub,
    sessions: SessionTracker,
) -> list[Any]:
    """Restore any clone parked mid-review, then classify startup tasks."""
    for task in list_tasks(engine):
        if task.state == "archived":
            continue
        try:
            restored_review = await ReviewManager.restore_parked_clone(
                task.clone_path, config.spawn_step_timeout
            )
        except Exception as exc:  # noqa: BLE001 — a single clone must not break startup
            logger.warning(
                "failed to check/restore review ref for task %d: %s", task.id, exc
            )
            restored_review = False
        try:
            restored_ship = await ShipManager.restore_parked_clone(
                task.clone_path, config.spawn_step_timeout
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "failed to check/restore ship ref for task %d: %s", task.id, exc
            )
            restored_ship = False
        if restored_review or restored_ship:
            logger.info("restored task %d clone from parked ref", task.id)
    return await classify_startup_tasks(engine, events, sessions)


def create_app(
    config: Config,
    *,
    frontend_dist: Path = DEFAULT_FRONTEND_DIST,
    config_path: Path | None = None,
) -> FastAPI:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.data_dir.chmod(0o755)
    db_path = db_path_for(config.data_dir)
    ensure_db_dir(db_path)
    upgrade_head(db_path)

    app = FastAPI(title="ompire-daemon", lifespan=_lifespan)
    app.state.config = config
    app.state.config_path = config_path or DEFAULT_CONFIG_PATH
    app.state.engine = make_engine(db_path)
    _chmod_db_private(db_path)
    app.state.auth_token = load_or_create_token(config.data_dir)
    app.state.events = EventHub()
    app.state.spawn_jobs = set()
    app.state.ws_connections = set()
    app.state.settings_store = SettingsStore(app.state.engine, config)
    effective_settings = app.state.settings_store.effective()
    app.state.sessions = SessionTracker(
        app.state.events, config.session_idle_debounce, effective_settings["stall_threshold"]
    )
    app.state.agents = AgentSupervisor(config, app.state.events, app.state.sessions)
    app.state.workflow_runner = WorkflowRunner(
        app.state.engine,
        config,
        app.state.events,
        app.state.agents,
        app.state.sessions,
    )
    app.state.reviews = ReviewManager(
        config, app.state.engine, app.state.events, app.state.sessions, app.state.agents
    )
    app.state.gpg = GpgProbe(config, app.state.events)
    app.state.ships = ShipManager(
        config,
        app.state.engine,
        app.state.events,
        app.state.sessions,
        app.state.agents,
        app.state.gpg,
    )
    app.state.notifications = AttentionNotifier(
        app.state.events,
        bind=config.bind,
        port=config.port,
        renotify_interval=effective_settings["renotify_interval"],
        enabled=config.notifications_enabled,
    )
    app.state.notifications.apply_settings(effective_settings)
    app.state.advisories = AdvisorySampler(
        app.state.events,
        stats_throttle_interval=config.stats_throttle_interval,
        context_advisory_threshold=effective_settings["context_advisory_threshold"],
    )
    app.state.prwatch = PrWatcher(config, app.state.engine, app.state.events)
    app.state.advisories.register(app.state.sessions)

    # Before any snapshot is served: restore any clone left parked by a
    # mid-review crash (review capability, design D-3), then classify every
    # live task per the startup reconciliation matrix (crash-recovery
    # capability, design D-4). No event loop is running yet at this point in
    # `create_app`, hence `run`.
    app.state.recoverable_tasks = asyncio.run(
        _prepare_startup(
            app.state.engine,
            app.state.config,
            app.state.events,
            app.state.sessions,
        )
    )

    app.include_router(api_router)
    app.include_router(ws_router)
    mount_frontend(app, frontend_dist)

    return app
