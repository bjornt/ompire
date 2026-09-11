"""The accepted-task preparation coordinator.

Application orchestration, not resource mechanics: this module decides *when*
accepted work uses the isolation boundary's resources, and owns everything
that is product state rather than workspace state — reading the task and its
accepted inputs, projecting them onto a `WorkspaceSpec`, translating live
resource progress into task events, coordinating the retained-attachment
handoff between clone and container, recording lifecycle observations through
the work owner, and starting the task's pinned workflow only after every
required workspace operation has succeeded.

The pipeline resolves nothing: acceptance already reviewed and pinned every
value this needs, in the same transaction that created the row (ADR-0026).
There is no second, later reading of a project, profile, or request override
that could disagree with what the operator approved, and no low-level
provisioning function can start or select a workflow. Preparation runs as an
asyncio background job after the acceptance transaction has committed; its
progress stays ephemeral — broadcast, never persisted — while the registry
records only the outcome (and, for the workshop phase, the launched
container's lock id).

Runs as: fetch → clone → branch → (attachment launches only) inputs →
workshop. The `inputs` phase is ordered deliberately after `branch` and before
`workshop`: the clone exists and is on its own branch, and no container,
session, or workflow step has started. A failure there fails the task with
nothing having executed under partial inputs — which is the whole guarantee
that phase exists to provide. A task with no pinned attachments keeps exactly
the four-step pipeline it had, including its progress shape.

ADR-0026, ADR-0028, ADR-0035, ADR-0039.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import Engine

from ompire_daemon.config import Config
from ompire_daemon.events import EventHub
from ompire_daemon.handoff import (
    HandoffError,
    MaterializationError,
    install_attachments,
    payload_key,
)
from ompire_daemon.isolation import (
    ExcludeUpdateError,
    WorkspaceOperationError,
    WorkspaceSpec,
    ensure_git_excludes,
    launch_workshop,
    prepare_clone,
    verify_pinned_source,
)
from ompire_daemon.oversight.tasks import task_payload
from ompire_daemon.registry.results import (
    DamagedManifestError,
    ResultNotAttachableError,
    ResultNotFoundError,
    read_all_files_on,
    verify_attachable_on,
    verify_payload_on,
)
from ompire_daemon.taskdefinition import (
    TaskDefinitionUnavailableError,
    resolve_task_definition,
)
from ompire_daemon.work.inputs import TaskExecutionInputs
from ompire_daemon.work.tasks import (
    Task,
    TaskConfigurationRequiredError,
    get_task,
    mark_failed,
    mark_spawn_completed,
    mark_workshop_launched,
    require_task_inputs,
)

logger = logging.getLogger(__name__)


def _workspace_spec(config: Config, task: Task, inputs: TaskExecutionInputs) -> WorkspaceSpec:
    """Project the task's accepted state onto the resource boundary's values.

    Everything the resource operations need, and nothing they do not: the
    owner token is the task id, and every workspace choice comes from the
    accepted inputs or the executable configuration — never re-read from
    mutable project or profile defaults.
    """
    return WorkspaceSpec(
        owner_id=task.id,
        clone_path=task.clone_path,
        task_root=config.task_dir_root,
        checkout_path=inputs.checkout_path,
        fetch_remote=inputs.fetch_remote,
        base_branch=inputs.workspace.base_branch,
        branch=task.branch,
        source_commit=inputs.source_commit,
        additions_source=inputs.workspace.workshop_additions,
        protected_destinations=inputs.protected_destinations,
        data_dir=config.data_dir,
        launcher_argv=config.my_workshop_command,
        git_timeout=config.spawn_step_timeout,
        workshop_timeout=config.workshop_step_timeout,
    )


def read_attachment_bytes(
    engine: Engine, inputs: TaskExecutionInputs
) -> dict[str, bytes]:
    """Re-verify every pinned revision and return its retained bytes.

    Identity is re-checked at consumption, not merely trusted from acceptance:
    the reservation that created this task proved the bytes were intact then,
    and this proves they still are now, immediately before an agent could see
    them. A revision that was damaged, purged, or replaced in between fails the
    spawn rather than installing whatever is currently stored under its id.
    """
    payloads: dict[str, bytes] = {}
    with engine.connect() as conn:
        for attachment in inputs.result_attachments:
            result = verify_attachable_on(
                conn,
                attachment.result_id,
                expected_manifest_id=attachment.manifest_id,
            )
            verify_payload_on(conn, result)
            stored = read_all_files_on(conn, attachment.result_id)
            for entry in attachment.files:
                data = stored.get(entry.path)
                if data is None:
                    raise ResultNotAttachableError(
                        attachment.result_id,
                        "damaged",
                        f"no longer retains {entry.path!r}",
                    )
                payloads[payload_key(attachment.result_id, entry.path)] = data
    return payloads


async def _install_inputs(
    engine: Engine, config: Config, task: Task, inputs: TaskExecutionInputs
) -> None:
    """Install the reviewed handoff bytes, before anything can run.

    Bytes come from the retained store and are verified against the manifest
    the launch was accepted with. The workspace is never read as an
    alternative source, and a failure is never repaired from it. Every
    failure is translated into the one classified preparation error, so the
    coordinator's handling covers it like any other phase.
    """
    if inputs.source_commit:
        await verify_pinned_source(
            task.clone_path,
            base_branch=inputs.workspace.base_branch,
            source_commit=inputs.source_commit,
            timeout=config.spawn_step_timeout,
        )
    try:
        payloads = await asyncio.to_thread(read_attachment_bytes, engine, inputs)
        await asyncio.to_thread(
            install_attachments,
            task.clone_path,
            list(inputs.result_attachments),
            payloads,
        )
    except (MaterializationError, HandoffError) as exc:
        raise WorkspaceOperationError("inputs", exc.detail) from exc
    except (ResultNotAttachableError, ResultNotFoundError, DamagedManifestError) as exc:
        raise WorkspaceOperationError("inputs", str(exc)) from exc
    # Re-applied now that the destinations exist: excluding them keeps the
    # handoff out of ordinary status and staging. It is a convenience, and
    # the delivery boundary never treats it as proof (ADR-0035).
    try:
        ensure_git_excludes(
            task.clone_path, "inputs", protected=inputs.protected_destinations
        )
    except ExcludeUpdateError as exc:
        raise WorkspaceOperationError("inputs", exc.detail) from exc


async def run_spawn_pipeline(
    engine: Engine,
    events: EventHub,
    config: Config,
    task_id: int,
    runner: Any,
) -> None:
    """Build the task's workspace from the inputs it was accepted under, then
    start its pinned workflow.

    The coordinator turns resource progress into the task's live events at the
    operation boundary, records lifecycle observations through the work owner,
    and hands the completed workspace to the workflow engine — session spawn
    and prompt delivery are workflow execution, not preparation.
    """
    task = get_task(engine, task_id)
    try:
        inputs = require_task_inputs(task)
    except TaskConfigurationRequiredError as exc:
        # Unreachable through acceptance, which writes the inputs in the same
        # transaction as the row. Kept as a hard stop rather than an assert:
        # if it ever happens, no git command must run against guessed values.
        failed = mark_failed(engine, task_id, str(exc))
        events.publish("task_updated", task_payload(failed, engine=engine))
        return

    spec = _workspace_spec(config, task, inputs)

    def report_clone_progress(progress) -> None:
        events.publish(
            "spawn_step",
            {"task_id": task_id, "step": progress.step, "status": progress.status},
        )

    def report_additions(source: str, note: str) -> None:
        # Its own event, not a second `spawn_step`: which additions source
        # applied — and whether the selected one was simply absent — is a
        # disclosure about the workspace, not a pipeline step outcome.
        events.publish(
            "workshop_additions",
            {"task_id": task_id, "source": source, "detail": note},
        )

    try:
        workspace = await prepare_clone(spec, report_clone_progress)
        # Only an attachment launch gets the extra phase. A task with no
        # pinned inputs keeps exactly the pipeline it had.
        if inputs.has_attachments:
            events.publish(
                "spawn_step", {"task_id": task_id, "step": "inputs", "status": "started"}
            )
            await _install_inputs(engine, config, task, inputs)
            events.publish(
                "spawn_step", {"task_id": task_id, "step": "inputs", "status": "ok"}
            )
        events.publish(
            "spawn_step",
            {"task_id": task_id, "step": "workshop", "status": "started"},
        )
        lock_id = await launch_workshop(spec, workspace, report_additions)
        mark_workshop_launched(engine, task_id, lock_id)
    except WorkspaceOperationError as exc:
        events.publish(
            "spawn_step",
            {
                "task_id": task_id,
                "step": exc.step,
                "status": "failed",
                "stderr": exc.detail,
            },
        )
        failed = mark_failed(engine, task_id, str(exc))
        events.publish("task_updated", task_payload(failed, engine=engine))
        return
    events.publish(
        "spawn_step", {"task_id": task_id, "step": "workshop", "status": "ok"}
    )

    # Workspace ready: record spawn completion (its startup-reconciliation
    # meaning is unchanged), then hand the task to the workflow engine —
    # session spawn and prompt delivery are workflow execution now.
    completed = mark_spawn_completed(engine, task_id)
    events.publish("task_updated", task_payload(completed, engine=engine))
    try:
        # The task's own pinned revision, not the catalog's current definition
        # of the same name (ADR-0028). Acceptance retained it in the same
        # transaction as the row, so a failure here is a damaged store, not a
        # race with an edit.
        revision = resolve_task_definition(engine, completed)
        runner.start_run(completed, revision)
    except TaskDefinitionUnavailableError as exc:
        failed = mark_failed(engine, task_id, exc.detail)
        events.publish("task_updated", task_payload(failed, engine=engine))
