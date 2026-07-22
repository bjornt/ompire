"""Startup recovery (crash-recovery capability, design D-4): classify every
live task on daemon startup, then resume the recoverable ones.

Two phases, split because they have very different latency budgets:

- `classify_startup_tasks` — the reconciliation matrix. DB-derivable fail
  verdicts come from `registry.tasks.reconcile_startup`; the remaining
  candidates (spawn-completed, session id recorded) get one `workshop_status`
  probe each to split `fail-missing-container` from recoverable. Fast, and
  must finish before the first WebSocket snapshot is served, so callers run
  it synchronously at startup (before `uvicorn` starts accepting requests).
- `run_recovery` — the actual resumes. Each is a real container-side `omp`
  startup (tens of seconds), so this runs as a background task kicked off
  from the lifespan startup, bounded by a concurrency limit so one wedged
  container can't starve the others.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict

from sqlalchemy import Engine

from ompire_daemon.agent import AgentSupervisor
from ompire_daemon.config import Config
from ompire_daemon.events import EventHub
from ompire_daemon.registry.tasks import Task, mark_failed, reconcile_startup
from ompire_daemon.sessions import SessionTracker
from ompire_daemon.workshop import workshop_status

logger = logging.getLogger(__name__)


async def classify_startup_tasks(
    engine: Engine, events: EventHub, tracker: SessionTracker
) -> list[Task]:
    """Run the full startup reconciliation matrix and seed recovering
    sessions (design D-4/6.1/6.2). Every task this call fails is broadcast as
    `task_updated`; every task it hands back as recoverable is already
    painted `starting` in the tracker. Must be awaited to completion before
    the first snapshot is served.
    """
    failed, candidates = reconcile_startup(engine)
    for task in failed:
        events.publish("task_updated", asdict(task))

    recoverable: list[Task] = []
    for task in candidates:
        status = await workshop_status(task.clone_path)
        if status == "present":
            recoverable.append(task)
            tracker.recovering(task.id)
        else:
            failed_task = mark_failed(
                engine, task.id, f"workshop container gone (status: {status!r}); cannot resume"
            )
            events.publish("task_updated", asdict(failed_task))
    return recoverable


async def _recover_one(
    engine: Engine,
    events: EventHub,
    supervisor: AgentSupervisor,
    tracker: SessionTracker,
    task: Task,
) -> None:
    assert task.session_id is not None  # guaranteed by classify_startup_tasks
    try:
        await supervisor.start(task.id, task.clone_path, resume=task.session_id)
    except Exception as exc:  # noqa: BLE001 — any resume failure lands `failed` (design 7.2)
        reason = f"resume failed: {exc}"
        logger.warning("recovery failed for task %d: %s", task.id, exc)
        tracker.recovery_failed(task.id, reason)
        failed_task = mark_failed(engine, task.id, reason)
        events.publish("task_updated", asdict(failed_task))
        return
    tracker.session_recovered(task.id)


async def run_recovery(
    engine: Engine,
    events: EventHub,
    config: Config,
    supervisor: AgentSupervisor,
    tracker: SessionTracker,
    recoverable: list[Task],
) -> None:
    """Resume every recoverable task in the background (design D-4/7): bounded
    fan-out (`config.recovery_concurrency`), each attempt already bounded by
    the ask-timeout preflight and ready-handshake timeouts `AgentSupervisor`
    composes internally — never blocks the daemon from serving. Intended to
    be kicked off as a background task from the lifespan startup, after
    `classify_startup_tasks` has already run synchronously.
    """
    if not recoverable:
        return
    semaphore = asyncio.Semaphore(config.recovery_concurrency)

    async def bound(task: Task) -> None:
        async with semaphore:
            await _recover_one(engine, events, supervisor, tracker, task)

    await asyncio.gather(*(bound(task) for task in recoverable))
