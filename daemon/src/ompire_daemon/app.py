"""FastAPI app wiring: config, migrations, auth, routers, static serving.

Architecture: ADR-0002 (docs/adr/0002-run-as-local-daemon-with-stateless-web-ui.md)
Trusted control-plane language: ADR-0003
(docs/adr/0003-implement-trusted-control-plane-in-python.md)
Local persistence: ADR-0005
(docs/adr/0005-persist-local-state-with-sqlite-core-and-alembic.md)
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

from ompire_daemon import launchconfig, workshopadditions
from ompire_daemon.advisories import AdvisorySampler
from ompire_daemon.agent import AgentSupervisor
from ompire_daemon.api.rest import router as api_router
from ompire_daemon.api.ws import router as ws_router
from ompire_daemon.auth import load_or_create_token
from ompire_daemon.config import DEFAULT_CONFIG_PATH, Config
from ompire_daemon.datadir import carry_forward_snap_state
from ompire_daemon.db import db_path_for, ensure_db_dir, make_engine
from ompire_daemon.delivery import WorkspaceGuard
from ompire_daemon.events import EventHub
from ompire_daemon.gh import GitHubProbe
from ompire_daemon.gpg import GpgProbe
from ompire_daemon.migrate import upgrade_head
from ompire_daemon.notifications import AttentionNotifier
from ompire_daemon.projectsetup import ProjectSetupManager
from ompire_daemon.prwatch import PrWatcher
from ompire_daemon.recovery import classify_startup_tasks, run_recovery
from ompire_daemon.registry.settings import SettingsStore
from ompire_daemon.registry.tasks import list_tasks
from ompire_daemon.review import ReviewManager, restore_reviews
from ompire_daemon.sessions import SessionTracker
from ompire_daemon.ship import ShipManager
from ompire_daemon.static import DEFAULT_FRONTEND_DIST, mount_frontend
from ompire_daemon.workflows import WorkflowRunner, install_packaged_workflows

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
    gh: GitHubProbe = app.state.gh
    project_setup: ProjectSetupManager = app.state.project_setup
    # Synchronous REST routes publish from FastAPI's threadpool; the hub needs
    # this loop to hand fan-out back to it (see events.py).
    events: EventHub = app.state.events
    events.bind_loop(asyncio.get_running_loop())
    await notifier.probe()
    notifier.start()
    advisories.start()
    reviews.start()
    prwatch.start()
    # Prime the shared GPG lock condition before the first snapshot.
    await gpg.probe()
    # GitHub observation is bounded and fail-closed, but it must not prevent
    # the daemon from serving unrelated work when the forge is unavailable.
    await gh.probe()
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
        await project_setup.shutdown()
        await agents.shutdown()
        await reviews.shutdown()


async def _prepare_startup(
    engine: Engine,
    config: Config,
    events: EventHub,
    sessions: SessionTracker,
    project_setup: ProjectSetupManager,
    ships: ShipManager,
    guard: WorkspaceGuard,
) -> list[Any]:
    """Retain the packaged workflow definitions, finish the upgrade, resolve
    interrupted project clones, reviews and deliveries, restore any parked
    clone or Workshop staging, then classify startup tasks.

    Order matters here. The packaged definitions are retained *first*: a task
    accepted in this process will pin one of them, and recovery resolves every
    existing task through its own retained revision, so neither can run before
    the rows exist (ADR-0028). Launch-configuration initialization comes next:
    a project or task the upgrade left blocked has to be blocked before the
    classifier hands it to recovery, or the daemon would try to resume a run
    whose model policy nobody has confirmed.
    """
    # Every packaged definition, retained under its content identity and
    # selected by its built-in library entry (ADR-0031). A packaged definition
    # that does not parse or validate raises out of here and stops the daemon:
    # shipping an unexecutable built-in is a build error, and starting anyway
    # would leave launches silently unavailable. Custom entries and their
    # drafts are untouched, and a broken custom draft is never parsed here —
    # which is why one cannot keep the daemon from starting.
    for conflict in install_packaged_workflows(engine):
        logger.error("workflow library: %s", conflict.detail)
    # Finish what migration 0013 could not: seed ordinary defaults for
    # projects that never had a template, and record any retired `judge_model`
    # as evidence to acknowledge (ADR-0026). Idempotent across restarts.
    launchconfig.initialize(engine, config)
    # A crash mid-launch can leave a clone carrying staged Workshop additions
    # that are not the ones its repository owns. Undo that before any agent
    # can start in it.
    workshopadditions.recover_pending(config.data_dir)
    # A project left `cloning` by a stopped daemon is resolved from the
    # filesystem before any client can see the project list, so a card can
    # never sit pending forever (ADR-0022).
    await project_setup.reconcile_pending()
    # Durable review history is corrected before anything else reads it, so
    # the first snapshot never carries an open review whose llmvet process
    # died with the daemon (review capability; ADR-0016).
    restore_reviews(engine)
    # Interrupted deliveries are reconciled before anything else can write to
    # their tasks (ADR-0032). This performs no privileged writes: it reads what
    # each attempt intended, observes whether that specific result exists, and
    # blocks the task when it cannot tell. It must run before the parked-clone
    # restore below, so a completed signed result is never reset away before
    # recovery has looked at it, and before task classification hands anything
    # to session recovery.
    for blocked_task_id in await ships.restore():
        logger.warning(
            "task %d has an unresolved delivery effect and is blocked until an "
            "operator resolves it",
            blocked_task_id,
        )
    # Clones parked by an older daemon's in-clone review or signing. A ref that
    # survives because Ompire could not honour it is evidence, not noise: the
    # affected task is blocked and says so, rather than being handed to
    # recovery as though its workspace were sound (ADR-0032).
    for task in list_tasks(engine):
        if task.state == "archived":
            continue
        outcomes: dict[str, str] = {}
        for label, restore in (
            ("review", ReviewManager.restore_parked_clone),
            ("delivery", ShipManager.restore_parked_clone),
        ):
            try:
                outcomes[label] = await restore(
                    task.clone_path, config.spawn_step_timeout
                )
            except Exception as exc:  # noqa: BLE001 — one clone must not break startup
                logger.warning(
                    "failed to check/restore the legacy %s ref for task %d: %s",
                    label,
                    task.id,
                    exc,
                )
                outcomes[label] = "unsafe"
        unsafe = [label for label, result in outcomes.items() if result == "unsafe"]
        if unsafe:
            reason = (
                f"a legacy {' and '.join(unsafe)} recovery ref survives in this "
                "task's clone because it could not be restored safely; its "
                "workspace is not trustworthy until that is resolved by hand"
            )
            guard.block(task.id, reason)
            logger.warning("task %d is blocked: %s", task.id, reason)
        elif "restored" in outcomes.values():
            logger.info("restored task %d clone from a legacy parked ref", task.id)
    return await classify_startup_tasks(engine, events, sessions)


async def _continue_task(app: FastAPI, task: Any) -> None:
    """Resume one confirmed-but-interrupted task, using the same per-task
    routine startup recovery uses (and therefore the same run guard, so a
    Continue that races an already-running run is a no-op rather than a
    second run)."""
    from ompire_daemon.recovery import recover_task

    await recover_task(
        app.state.engine,
        app.state.events,
        app.state.config,
        app.state.agents,
        app.state.sessions,
        app.state.workflow_runner,
        task,
    )


def create_app(
    config: Config,
    *,
    frontend_dist: Path = DEFAULT_FRONTEND_DIST,
    config_path: Path | None = None,
) -> FastAPI:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.data_dir.chmod(0o755)
    # Before any database work: a snap that used to store state per revision
    # leaves it behind on upgrade (ADR-0024). Anything carried in is an
    # ordinary existing database from here on, including to `upgrade_head`.
    carry_forward_snap_state(config.data_dir)
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
        app.state.events,
        config.session_idle_debounce,
        effective_settings["stall_threshold"],
    )
    app.state.agents = AgentSupervisor(config, app.state.events, app.state.sessions)
    app.state.workflow_runner = WorkflowRunner(
        app.state.engine,
        config,
        app.state.events,
        app.state.agents,
        app.state.sessions,
    )
    # One task-scoped exclusion shared by review, drafting, delivery, agent
    # turns, workflow steps and cleanup (ADR-0032). It is created before the
    # managers that admit against it.
    app.state.workspace_guard = WorkspaceGuard()
    app.state.workflow_runner.set_guard(app.state.workspace_guard)
    app.state.reviews = ReviewManager(
        config,
        app.state.engine,
        app.state.events,
        app.state.sessions,
        app.state.agents,
        app.state.workspace_guard,
    )
    app.state.gpg = GpgProbe(config, app.state.events, app.state.settings_store)
    app.state.gh = GitHubProbe(config, app.state.events)
    app.state.project_setup = ProjectSetupManager(
        config, app.state.engine, app.state.events
    )
    app.state.ships = ShipManager(
        config,
        app.state.engine,
        app.state.events,
        app.state.sessions,
        app.state.agents,
        app.state.gpg,
        app.state.gh,
        app.state.workspace_guard,
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
    app.state.prwatch = PrWatcher(
        config, app.state.engine, app.state.events, app.state.gh
    )
    app.state.advisories.register(app.state.sessions)
    # Bound to the app so the REST route does not have to reach into recovery
    # internals; the guard on eligibility lives in the route.
    app.state.continue_task = lambda task: _continue_task(app, task)

    # Before any snapshot is served: close out reviews interrupted by the
    # restart and restore any clone left parked by a mid-review crash (review
    # capability, design D-3), then classify every live task per the startup
    # reconciliation matrix (crash-recovery capability, design D-4). No event
    # loop is running yet at this point in `create_app`, hence `run`.
    app.state.recoverable_tasks = asyncio.run(
        _prepare_startup(
            app.state.engine,
            app.state.config,
            app.state.events,
            app.state.sessions,
            app.state.project_setup,
            app.state.ships,
            app.state.workspace_guard,
        )
    )

    app.include_router(api_router)
    app.include_router(ws_router)
    mount_frontend(app, frontend_dist)

    return app
