"""Startup recovery (crash-recovery capability, design D-4; workflow-engine
design D-6): classify every live task on daemon startup, resume the
recoverable tasks' recorded sessions, then re-drive interrupted workflow
runs from persisted state.

Two phases, split because they have very different latency budgets:

- `classify_startup_tasks` — the reconciliation matrix. DB-derivable fail
  verdicts come from `registry.tasks.reconcile_startup`; the remaining
  candidates (spawn-completed) get one `workshop_status` probe each to split
  `fail-missing-container` from recoverable. Fast, and must finish before the
  first WebSocket snapshot is served, so callers run it synchronously at
  startup (before `uvicorn` starts accepting requests). Sessions with a
  recorded omp identity are seeded `starting` here so the first snapshot
  already paints them as recovering.
- `run_recovery` — the actual resumes plus workflow re-drives. Each resume is
  a real container-side `omp` startup (tens of seconds), so this runs as a
  background task kicked off from the lifespan startup, bounded by a
  concurrency limit so one wedged container can't starve the others.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy import Engine

from ompire_daemon.agent import AgentSupervisor
from ompire_daemon.config import Config
from ompire_daemon.events import EventHub
from ompire_daemon.execution_inputs import ModelPolicy
from ompire_daemon.registry.sessions import list_resumable_sessions
from ompire_daemon.registry.tasks import (
    Task,
    mark_failed,
    reconcile_startup,
    task_payload,
)
from ompire_daemon.sessions import SessionTracker
from ompire_daemon.workflows import WorkflowRunner
from ompire_daemon.workshop import workshop_status

logger = logging.getLogger(__name__)


async def classify_startup_tasks(
    engine: Engine, events: EventHub, tracker: SessionTracker
) -> list[Task]:
    """Run the full startup reconciliation matrix and seed recovering
    sessions (design D-4/6.1/6.2). Every task this call fails is broadcast as
    `task_updated`; every task it hands back as recoverable has its recorded
    sessions painted `starting` in the tracker. Must be awaited to completion
    before the first snapshot is served.
    """
    failed, candidates = reconcile_startup(engine)
    for task in failed:
        events.publish("task_updated", task_payload(task))

    recoverable: list[Task] = []
    for task in candidates:
        status = await workshop_status(task.clone_path)
        if status == "present":
            recoverable.append(task)
            for session in list_resumable_sessions(engine, task.id):
                tracker.recovering(task.id, session.name)
        else:
            failed_task = mark_failed(
                engine, task.id, f"workshop container gone (status: {status!r}); cannot resume"
            )
            events.publish("task_updated", task_payload(failed_task))
    return recoverable


async def _resume_session(
    engine: Engine,
    events: EventHub,
    supervisor: AgentSupervisor,
    tracker: SessionTracker,
    task: Task,
    session_name: str,
    omp_session_id: str,
    policy: ModelPolicy,
) -> bool:
    """Resume one recorded session under the task's *accepted* policy.

    A resumed omp restores its own model settings from the session file, so
    the supervisor re-asserts the accepted active pair over the acknowledged
    native controls and verifies the result before anything is prompted. A
    profile edited or deleted since acceptance changes nothing here — the
    policy comes off the task."""
    try:
        await supervisor.start(
            task.id,
            session_name,
            task.clone_path,
            policy=policy,
            resume=omp_session_id,
        )
    except Exception as exc:  # noqa: BLE001 — any resume failure lands the session `failed`
        reason = f"resume failed: {exc}"
        logger.warning(
            "recovery failed for task %d session %s: %s", task.id, session_name, exc
        )
        tracker.recovery_failed(task.id, session_name, reason)
        return False
    tracker.session_recovered(task.id, session_name)
    return True


async def recover_task(
    engine: Engine,
    events: EventHub,
    config: Config,
    supervisor: AgentSupervisor,
    tracker: SessionTracker,
    runner: WorkflowRunner,
    task: Task,
) -> None:
    """Recover one task on its own, unbounded by the startup fan-out.

    This is what the operator's explicit Continue calls after confirming a
    legacy task's configuration, so a resumed-after-confirmation task follows
    exactly the same path as a resumed-after-restart one."""
    await _recover_one(
        engine,
        events,
        config,
        supervisor,
        tracker,
        runner,
        asyncio.Semaphore(1),
        task,
    )


async def _recover_one(
    engine: Engine,
    events: EventHub,
    config: Config,
    supervisor: AgentSupervisor,
    tracker: SessionTracker,
    runner: WorkflowRunner,
    semaphore: asyncio.Semaphore,
    task: Task,
) -> None:
    if task.execution_inputs is None:
        # A task from before pinned inputs (ADR-0026). Resuming would need a
        # model policy nobody recorded, and today's project/profile settings
        # are not evidence of what it ran under. Leave it exactly as it is:
        # the run keeps its position, its sessions and workspace are
        # untouched, and task detail offers the operator a confirmation.
        logger.info(
            "task %d has no confirmed launch configuration; skipping recovery",
            task.id,
        )
        return
    inputs = task.execution_inputs
    policy = ModelPolicy.from_roles(inputs.roles)

    # 1. Resume every recorded session (bounded concurrency across tasks).
    sessions = list_resumable_sessions(engine, task.id)

    async def bound(name: str, omp_session_id: str) -> bool:
        async with semaphore:
            return await _resume_session(
                engine, events, supervisor, tracker, task, name, omp_session_id, policy
            )

    results = await asyncio.gather(
        *(bound(s.name, s.omp_session_id) for s in sessions if s.omp_session_id is not None)
    )
    all_resumed = all(results)

    if task.workflow_status not in ("running", "waiting"):
        # No run to re-drive (legacy-migrated `complete`, or `failed`): the
        # pre-workflow behavior stands — a session that would not resume
        # fails the task.
        if not all_resumed and sessions:
            failed_task = mark_failed(engine, task.id, "session resume failed")
            events.publish("task_updated", task_payload(failed_task))
        return

    # 2. Re-drive the interrupted run from persisted state (design D-6). A
    # session that failed to resume is lazily re-spawned fresh by the engine
    # on first use (its old context is lost; the run's step records persist).
    runner.recover_run(task)


async def run_recovery(
    engine: Engine,
    events: EventHub,
    config: Config,
    supervisor: AgentSupervisor,
    tracker: SessionTracker,
    runner: WorkflowRunner,
    recoverable: list[Task],
) -> None:
    """Resume every recoverable task's sessions and re-drive interrupted
    workflow runs, in the background (design D-4/7, workflow-engine D-6):
    bounded fan-out (`config.recovery_concurrency`), each resume already
    bounded by the ask-timeout preflight and ready-handshake timeouts
    `AgentSupervisor` composes internally — never blocks the daemon from
    serving. Intended to be kicked off as a background task from the lifespan
    startup, after `classify_startup_tasks` has already run synchronously.
    """
    if not recoverable:
        return
    semaphore = asyncio.Semaphore(config.recovery_concurrency)
    await asyncio.gather(
        *(
            _recover_one(
                engine, events, config, supervisor, tracker, runner, semaphore, task
            )
            for task in recoverable
        )
    )
