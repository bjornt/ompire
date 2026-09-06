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
from ompire_daemon.execution_inputs import (
    MissingConsumerBindingError,
    ModelPolicy,
)
from ompire_daemon.registry.sessions import (
    APPLIED_ORIGIN_MIGRATED,
    AppliedPolicy,
    build_applied_policy,
    get_session,
    list_resumable_sessions,
    record_applied_policy,
)
from ompire_daemon.registry.tasks import (
    Task,
    mark_failed,
    reconcile_startup,
    task_payload,
)
from ompire_daemon.registry.workflows import list_step_records
from ompire_daemon.sessions import SessionTracker
from ompire_daemon.taskdefinition import (
    TaskDefinitionUnavailableError,
    resolve_task_definition,
)
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
        events.publish("task_updated", task_payload(task, engine=engine))

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
            events.publish("task_updated", task_payload(failed_task, engine=engine))
    return recoverable


async def _resume_session(
    engine: Engine,
    events: EventHub,
    supervisor: AgentSupervisor,
    tracker: SessionTracker,
    task: Task,
    session_name: str,
    omp_session_id: str,
    applied: AppliedPolicy,
) -> bool:
    """Resume one recorded session under the policy *that session* last ran.

    Not the task's first step's policy, and not today's profiles: two steps
    sharing a session can pin different bindings, so only the session's own
    applied record says what its conversation was configured with. A resumed
    omp restores its model settings from the session file, so the supervisor
    still re-asserts the pair over the acknowledged native controls and
    verifies the result before anything is prompted. A profile edited or
    deleted since acceptance changes nothing here.

    The record is re-committed after a verified resume so its origin becomes
    `verified` — a migrated continuation policy stops being a guess once a
    process has actually been put on it.
    """

    def commit() -> None:
        record_applied_policy(
            engine,
            task.id,
            session_name,
            build_applied_policy(
                applied.policy,
                profile_name=applied.profile_name,
                role=applied.role,
                consumer_kind=applied.consumer_kind,
                consumer_name=applied.consumer_name,
            ),
        )

    try:
        await supervisor.start(
            task.id,
            session_name,
            task.clone_path,
            policy=applied.policy,
            resume=omp_session_id,
            commit=commit,
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


def _continuation_policy(
    engine: Engine, task: Task, session_name: str
) -> AppliedPolicy | None:
    """What this session continues under, or None if nothing records it.

    The applied record is the answer whenever there is one. Failing that, a
    step that was interrupted *before* its prompt went out has an accepted
    binding that the run is about to apply anyway, so resuming on it is the
    same decision the workflow is about to make rather than an inference.
    Anything else is genuinely unknown, and a session is left unresumed
    rather than restored under a policy nobody chose.
    """
    session = get_session(engine, task.id, session_name)
    if session is not None and session.applied_policy is not None:
        return session.applied_policy
    inputs = task.execution_inputs
    if inputs is None:
        return None
    records = list_step_records(engine, task.id)
    pending = next(
        (
            record
            for record in reversed(records)
            if record.status == "running"
            and record.kind == "agent"
            and record.session == session_name
            and record.prompted_at is None
        ),
        None,
    )
    if pending is None:
        return None
    try:
        binding = inputs.binding_for_step(pending.step)
    except MissingConsumerBindingError:
        return None
    return build_applied_policy(
        ModelPolicy.from_binding(binding),
        profile_name=binding.profile_name,
        role=binding.role,
        consumer_kind="step",
        consumer_name=pending.step,
        origin=APPLIED_ORIGIN_MIGRATED,
    )


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
    # 0. Resolve the pinned definition *before* anything else (ADR-0028).
    #    A revision that is absent, damaged, or written for a newer format
    #    cannot tell this run where it is, so nothing is resumed, no prompt is
    #    sent, and no privileged work is published. The task keeps its
    #    position, its sessions and workspace, and says why in task detail.
    try:
        revision = resolve_task_definition(engine, task)
    except TaskDefinitionUnavailableError as exc:
        logger.info(
            "task %d cannot resolve its pinned workflow definition (%s); "
            "skipping recovery: %s",
            task.id,
            exc.reason,
            exc.detail,
        )
        return
    # 1. Resume every recorded session the pinned definition still declares,
    #    each under its own last applied policy (bounded concurrency across
    #    tasks). A session the definition does not declare — the retired
    #    engine `judge` above all — is deliberately left alone: nothing will
    #    prompt it again, so starting a process for it would spend a container
    #    slot on a conversation with no next turn. Its transcript and applied
    #    policy stay on record.
    declared = set(revision.definition.sessions)
    sessions = [
        session
        for session in list_resumable_sessions(engine, task.id)
        if session.name in declared
    ]

    async def bound(name: str, omp_session_id: str) -> bool:
        applied = _continuation_policy(engine, task, name)
        if applied is None:
            # No record of what this session ran under. Resuming it would
            # mean choosing a model policy for a conversation already in
            # progress, which is exactly the guess per-consumer pinning
            # exists to avoid. The workspace, transcript and step history
            # are untouched; the engine spawns a fresh session if the run
            # needs one, and says so.
            logger.info(
                "task %d session %s has no recorded model policy; leaving it "
                "unresumed rather than choosing one",
                task.id,
                name,
            )
            tracker.recovery_failed(
                task.id,
                name,
                "no recorded model policy for this session; it cannot be "
                "resumed without choosing one",
            )
            return False
        async with semaphore:
            return await _resume_session(
                engine, events, supervisor, tracker, task, name, omp_session_id, applied
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
            events.publish("task_updated", task_payload(failed_task, engine=engine))
        return

    # 2. Re-drive the interrupted run from persisted state (design D-6). A
    # session that failed to resume is lazily re-spawned fresh by the engine
    # on first use (its old context is lost; the run's step records persist).
    runner.recover_run(task, revision)


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
