"""Work router: task launch, reads, configuration, and explicit Continue.

The launch routes are adapters over `LaunchService`: they convert the wire
body to the typed `LaunchRequest` (preserving the omission/null/empty
distinctions), call the command, and map domain errors to the published
status codes. The acceptance transaction, observation, and scheduling live
in the service, never here.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import Engine

from ompire_daemon.api.deps import _engine, _events, _launch_service
from ompire_daemon.api.errors import (
    attachment_observation_error,
    launch_error,
    preview_changed,
)
from ompire_daemon.api.work_models import (
    TaskAccept,
    TaskContinuationConfirmIn,
    TaskContinuationIn,
    TaskCreate,
    TaskDetailOut,
    TaskOut,
)
from ompire_daemon.application import tasks as task_commands
from ompire_daemon.application.launch import (
    LaunchMentionsRejectedError,
    LaunchService,
)
from ompire_daemon.events import EventHub
from ompire_daemon.handoff import HandoffError
from ompire_daemon.oversight.tasks import resolution_payload, task_payload
from ompire_daemon.work.files import ProjectFilesError
from ompire_daemon.work.inputs import WORKSPACE_FIELDS
from ompire_daemon.work.launch import (
    AttachmentSelection,
    ConsumerOverride,
    LaunchInputError,
    LaunchRequest,
    PreviewChangedError,
    ProjectNotLaunchableError,
)
from ompire_daemon.work.projects import (
    InvalidWorkshopAdditionsError,
    ProjectNotFoundError,
)
from ompire_daemon.work.reconciliation import (
    ReconciliationConflictError,
    TaskContinuation,
    confirm_task_configuration,
    preview_task_configuration,
    task_configuration,
)
from ompire_daemon.work.tasks import (
    ClonePathOutsideRootError,
    DuplicateTaskError,
    TaskConfigurationRequiredError,
    TaskInputsAlreadyPinnedError,
    TaskNotFoundError,
)

router = APIRouter()


def _launch_request(body: TaskCreate) -> LaunchRequest:
    """Normalize the wire body into the typed selection the service resolves.

    Omitted-versus-null distinctions are made here because only the wire can
    express them; everything the same rules check about the *selection* is
    enforced by the service for direct callers too.
    """
    overrides: dict[str, str] = {}
    supplied = body.workspace_overrides
    if supplied is not None:
        for field in WORKSPACE_FIELDS:
            if field not in supplied.model_fields_set:
                continue
            value = getattr(supplied, field)
            if value is None:
                if field == "preamble":
                    # A null preamble says the same thing as an empty one.
                    overrides[field] = ""
                    continue
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_CONTENT,
                    f"workspace_overrides.{field}: null is not a reset; omit the "
                    "field to inherit the project default",
                )
            overrides[field] = value
    return LaunchRequest(
        project_name=body.project_name,
        workflow_name=body.workflow_name,
        slug=body.slug,
        prompt=body.prompt,
        model_profile=body.model_profile,
        profile_explicit=(
            "model_profile" in body.model_fields_set and body.model_profile is not None
        ),
        workspace_overrides=overrides,
        result_attachments=tuple(
            AttachmentSelection(
                producer_task_id=entry.producer_task_id,
                result_id=entry.result_id,
                expected_manifest_id=entry.expected_manifest_id,
            )
            for entry in body.result_attachments
        ),
        acknowledge_result_base_difference=body.acknowledge_result_base_difference,
        step_overrides=_consumer_overrides(body.step_overrides, "step_overrides"),
        # Not normalized: an empty override for a consumer that no longer
        # exists is still a caller believing it configures something, and it
        # is refused with the field rather than dropped as a no-op.
        auxiliary_overrides={
            name: ConsumerOverride(
                model_profile=entry.model_profile, role=entry.role
            )
            for name, entry in body.auxiliary_overrides.items()
        },
    )


def _consumer_overrides(
    supplied: dict[str, Any], field: str
) -> dict[str, ConsumerOverride]:
    """Normalize the row override maps before anything resolves or
    fingerprints them.

    Null and omitted both mean inherit, and an entry that overrides nothing
    is dropped entirely: `{"fix": {}}` and an absent `fix` are the same
    launch, and letting them fingerprint differently would invalidate a
    reviewed preview over a difference the operator cannot see. An empty
    profile name is refused rather than read as a reset — a reset is
    expressed by omitting the field.
    """
    normalized: dict[str, ConsumerOverride] = {}
    for name, entry in supplied.items():
        if entry.model_profile is not None and not entry.model_profile.strip():
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"{field}.{name}.model_profile: a model profile name must not be "
                "empty; omit the field to inherit",
            )
        override = ConsumerOverride(
            model_profile=entry.model_profile, role=entry.role
        )
        if not override.is_empty():
            normalized[name] = override
    return normalized


@router.post("/tasks/preview")
async def preview_task_route(
    body: TaskCreate,
    service: LaunchService = Depends(_launch_service),
) -> dict[str, Any]:
    """Resolve the operator's selections without creating anything.

    Same rules, same service, and same output as acceptance — that identity is
    what makes reviewing a preview worth anything.
    """
    try:
        resolved = await service.preview(_launch_request(body))
    except (LaunchInputError, ProjectNotLaunchableError) as exc:
        raise launch_error(exc) from exc
    except HandoffError as exc:
        raise attachment_observation_error(exc) from exc
    return resolution_payload(resolved)


@router.get("/tasks", response_model=list[TaskOut])
def list_tasks_route(engine: Engine = Depends(_engine)) -> list[dict[str, Any]]:
    return task_commands.task_list_payloads(engine)


@router.get("/tasks/{task_id}", response_model=TaskDetailOut)
async def get_task_route(
    task_id: int, engine: Engine = Depends(_engine)
) -> TaskDetailOut:
    try:
        detail = await task_commands.task_detail(engine, task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return TaskDetailOut(
        **task_payload(detail.task, engine=engine),
        workshop_status=detail.workshop_status,
        workflow_steps=detail.workflow_steps,
    )


@router.post("/tasks", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
async def spawn_task_route(
    body: TaskAccept,
    service: LaunchService = Depends(_launch_service),
    engine: Engine = Depends(_engine),
) -> dict[str, Any]:
    """Accept one reviewed launch.

    The ordering that matters — observation and validation outside the write
    reservation, the task and its references inside it, effects only after
    the commit — is owned by the service this delegates to.
    """
    try:
        task = await service.accept(
            _launch_request(body), preview_token=body.preview_token
        )
    except (LaunchInputError, ProjectNotLaunchableError) as exc:
        raise launch_error(exc) from exc
    except HandoffError as exc:
        raise attachment_observation_error(exc) from exc
    except PreviewChangedError as exc:
        raise preview_changed(exc) from exc
    except LaunchMentionsRejectedError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except ProjectFilesError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except ClonePathOutsideRootError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except DuplicateTaskError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return task_payload(task, engine=engine)


# --- Upgrade reconciliation: a legacy task's continuation configuration -------
# Never resolved by guessing (ADR-0026, ADR-0028).


def _continuation(body: TaskContinuationIn) -> TaskContinuation:
    return TaskContinuation(
        model_profile=body.model_profile,
        base_branch=body.base_branch,
        workshop_additions=body.workshop_additions,
        preamble=body.preamble,
    )


@router.get("/tasks/{task_id}/configuration")
def get_task_configuration_route(
    task_id: int, engine: Engine = Depends(_engine)
) -> dict[str, Any]:
    try:
        return task_configuration(engine, task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post("/tasks/{task_id}/configuration/preview")
def preview_task_configuration_route(
    task_id: int,
    body: TaskContinuationIn,
    engine: Engine = Depends(_engine),
) -> dict[str, Any]:
    try:
        return preview_task_configuration(engine, task_id, _continuation(body))
    except TaskNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ProjectNotFoundError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except ReconciliationConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except (LaunchInputError, InvalidWorkshopAdditionsError) as exc:
        raise launch_error(exc) from exc


@router.post("/tasks/{task_id}/configuration/confirm", response_model=TaskOut)
def confirm_task_configuration_route(
    task_id: int,
    body: TaskContinuationConfirmIn,
    engine: Engine = Depends(_engine),
    events: EventHub = Depends(_events),
) -> dict[str, Any]:
    try:
        task = confirm_task_configuration(
            engine,
            task_id,
            _continuation(body),
            preview_token=body.preview_token,
            acknowledge_unknown=body.acknowledge_unknown,
            acknowledge_workflow=body.acknowledge_workflow,
        )
    except TaskNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ProjectNotFoundError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except (ReconciliationConflictError, TaskInputsAlreadyPinnedError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except (LaunchInputError, InvalidWorkshopAdditionsError) as exc:
        raise launch_error(exc) from exc
    payload = task_payload(task, engine=engine)
    events.publish("task_updated", payload)
    return payload


@router.post("/tasks/{task_id}/continue", response_model=TaskOut)
async def continue_task_route(
    task_id: int,
    request: Request,
    engine: Engine = Depends(_engine),
) -> dict[str, Any]:
    """Resume a task whose run was left in place while it was unconfigured.

    Deliberately explicit and deliberately narrow: only a run that was already
    `running` or `waiting` is eligible. A failed or completed task is not
    silently restarted, and review and ship remain their own actions.
    """
    try:
        return await task_commands.continue_task(
            engine, task_id, request.app.state.continue_task
        )
    except TaskNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except TaskConfigurationRequiredError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except task_commands.ContinueIneligibleError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

