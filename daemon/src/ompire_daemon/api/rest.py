"""REST endpoints under /api/. Commands only — events go out over the WebSocket."""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import asdict
from pathlib import Path

from sqlalchemy import Engine
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, field_validator

from ompire_daemon.advisories import AdvisorySampler
from ompire_daemon.agent import AgentHandle, AgentSupervisor, NoLiveAgentError
from ompire_daemon.rpc import AgentGoneError, RequestFailedError
from ompire_daemon.auth import require_bearer_token
from ompire_daemon.config import Config
from ompire_daemon.events import EventHub
from ompire_daemon.registry.projects import (
    DuplicateProjectError,
    InvalidBranchPatternError,
    Project,
    ProjectHasReferencingTasksError,
    ProjectNotFoundError,
    create_project,
    delete_project,
    get_project,
    list_projects,
    update_project,
    validate_slug,
)
from ompire_daemon.registry.tasks import (
    ClonePathOutsideRootError,
    DuplicateTaskError,
    Task,
    TaskNotArchivedError,
    TaskNotFoundError,
    clone_path_for,
    create_task,
    get_task,
    list_tasks,
    mark_archived,
    purge_task,
    validate_task_slug,
)
from ompire_daemon.sessions import SessionTracker
from ompire_daemon.spawn import run_spawn_pipeline
from ompire_daemon.workshop import WorkshopRemoveError, remove_workshop, workshop_status

router = APIRouter(prefix="/api", dependencies=[Depends(require_bearer_token)])


class ProjectCreate(BaseModel):
    name: str
    title: str
    upstream_url: str
    fork_url: str | None = None
    checkout_path: str | None = None
    base_branch: str = "main"
    branch_pattern: str | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        validate_slug(value)
        return value


class ProjectUpdate(BaseModel):
    title: str
    upstream_url: str
    fork_url: str | None = None
    checkout_path: str
    base_branch: str
    branch_pattern: str


class ProjectOut(BaseModel):
    name: str
    title: str
    upstream_url: str
    fork_url: str | None
    checkout_path: str
    base_branch: str
    branch_pattern: str

    model_config = {"from_attributes": True}


def _engine(request: Request) -> Engine:
    return request.app.state.engine


def _config(request: Request) -> Config:
    return request.app.state.config


def _events(request: Request) -> EventHub:
    return request.app.state.events


def _sessions(request: Request) -> SessionTracker:
    return request.app.state.sessions


def _advisories(request: Request) -> AdvisorySampler:
    return request.app.state.advisories


@router.get("/projects", response_model=list[ProjectOut])
def list_projects_route(engine: Engine = Depends(_engine)) -> list[Project]:
    return list_projects(engine)


@router.post("/projects", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project_route(
    body: ProjectCreate,
    engine: Engine = Depends(_engine),
    config: Config = Depends(_config),
    events: EventHub = Depends(_events),
) -> Project:
    try:
        project = create_project(
            engine,
            name=body.name,
            title=body.title,
            upstream_url=body.upstream_url,
            fork_url=body.fork_url,
            checkout_path=body.checkout_path,
            base_branch=body.base_branch,
            branch_pattern=body.branch_pattern,
            default_branch_pattern=config.default_branch_pattern,
            default_checkout_root=config.checkout_root,
        )
    except InvalidBranchPatternError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except DuplicateProjectError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    events.publish("project_created", asdict(project))
    return project


@router.get("/projects/{name}", response_model=ProjectOut)
def get_project_route(name: str, engine: Engine = Depends(_engine)) -> Project:
    try:
        return get_project(engine, name)
    except ProjectNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.put("/projects/{name}", response_model=ProjectOut)
def update_project_route(
    name: str,
    body: ProjectUpdate,
    engine: Engine = Depends(_engine),
    events: EventHub = Depends(_events),
) -> Project:
    try:
        project = update_project(
            engine,
            name,
            title=body.title,
            upstream_url=body.upstream_url,
            fork_url=body.fork_url,
            checkout_path=body.checkout_path,
            base_branch=body.base_branch,
            branch_pattern=body.branch_pattern,
        )
    except InvalidBranchPatternError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except ProjectNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    events.publish("project_updated", asdict(project))
    return project


@router.delete("/projects/{name}")
def delete_project_route(
    name: str, engine: Engine = Depends(_engine), events: EventHub = Depends(_events)
) -> dict[str, str]:
    try:
        delete_project(engine, name)
    except ProjectNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ProjectHasReferencingTasksError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    events.publish("project_deleted", {"name": name})
    return {"deleted": name}


class TaskCreate(BaseModel):
    project_name: str
    slug: str
    prompt: str

    @field_validator("slug")
    @classmethod
    def _validate_slug(cls, value: str) -> str:
        validate_task_slug(value)
        return value


class TaskOut(BaseModel):
    id: int
    project_name: str
    slug: str
    branch: str
    clone_path: str
    state: str
    prompt: str
    error: str | None
    workshop_id: str | None
    spawn_completed_at: str | None
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


@router.get("/tasks", response_model=list[TaskOut])
def list_tasks_route(engine: Engine = Depends(_engine)) -> list[Task]:
    return list_tasks(engine)


class TaskDetailOut(TaskOut):
    # Derived on demand from the workshop CLI, never persisted (design D-3).
    workshop_status: str | None


@router.get("/tasks/{task_id}", response_model=TaskDetailOut)
async def get_task_route(task_id: int, engine: Engine = Depends(_engine)) -> TaskDetailOut:
    try:
        task = get_task(engine, task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    derived_status = await workshop_status(task.clone_path) if task.workshop_id else None
    return TaskDetailOut(**asdict(task), workshop_status=derived_status)


@router.post("/tasks", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
async def spawn_task_route(
    body: TaskCreate,
    request: Request,
    engine: Engine = Depends(_engine),
    config: Config = Depends(_config),
    events: EventHub = Depends(_events),
) -> Task:
    try:
        project = get_project(engine, body.project_name)
    except ProjectNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    try:
        clone_path = clone_path_for(config.task_dir_root, project.name, body.slug)
    except ClonePathOutsideRootError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc

    branch = project.branch_pattern.replace("<slug>", body.slug)
    try:
        task = create_task(
            engine,
            project_name=project.name,
            slug=body.slug,
            branch=branch,
            clone_path=str(clone_path),
            prompt=body.prompt,
        )
    except DuplicateTaskError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    events.publish("task_created", asdict(task))
    job = asyncio.create_task(
        run_spawn_pipeline(
            engine,
            events,
            config,
            task.id,
            project,
            request.app.state.agents,
            request.app.state.sessions,
        )
    )
    # Keep a reference so the job isn't garbage-collected mid-pipeline.
    jobs: set[asyncio.Task] = request.app.state.spawn_jobs
    jobs.add(job)
    job.add_done_callback(jobs.discard)
    return task


@router.post("/tasks/{task_id}/cleanup", response_model=TaskOut)
async def cleanup_task_route(
    task_id: int,
    engine: Engine = Depends(_engine),
    config: Config = Depends(_config),
    events: EventHub = Depends(_events),
    sessions: SessionTracker = Depends(_sessions),
    advisories: AdvisorySampler = Depends(_advisories),
) -> Task:
    try:
        task = get_task(engine, task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    clone_path = Path(task.clone_path).resolve()
    task_root = config.task_dir_root.expanduser().resolve()
    if task_root not in clone_path.parents:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"refusing to delete {clone_path}: outside task root {task_root}",
        )

    # Tear down the container before deleting the clone under it (design D-4);
    # an already-gone workshop is fine, any other failure aborts un-archived.
    if task.workshop_id is not None:
        try:
            await remove_workshop(str(clone_path), config.workshop_step_timeout)
        except WorkshopRemoveError as exc:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"workshop remove failed; clone retained:\n{exc.stderr}",
            ) from exc

    # Idempotent: a missing directory is already cleaned up.
    await asyncio.to_thread(shutil.rmtree, clone_path, ignore_errors=True)

    archived = mark_archived(engine, task_id)
    sessions.discard(task_id)
    advisories.clear_task(task_id)
    events.publish("task_updated", asdict(archived))
    return archived


# --- Agent control surface --------------------------------------------------
# Start and prompt are the spawn pipeline's job now (design D-5); stop stays
# as the manual kill switch, feeding the tracker's operator-stop reason.


def _supervisor(request: Request) -> AgentSupervisor:
    return request.app.state.agents


def _require_task(engine: Engine, task_id: int) -> Task:
    try:
        return get_task(engine, task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post("/tasks/{task_id}/agent/stop")
async def stop_agent_route(
    task_id: int,
    engine: Engine = Depends(_engine),
    supervisor: AgentSupervisor = Depends(_supervisor),
    sessions: SessionTracker = Depends(_sessions),
) -> dict[str, object]:
    _require_task(engine, task_id)
    # Flag before the kill so the exit lands as "stopped by operator", not a
    # crash (design D-2); cleared again if there was nothing to stop.
    sessions.expect_operator_stop(task_id)
    try:
        await supervisor.stop(task_id)
    except NoLiveAgentError as exc:
        sessions.clear_operator_stop(task_id)
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {"task_id": task_id, "agent": "stopped"}


# --- Agent interaction: thin proxies over the live AgentHandle (design D-1) ---
# Composer actions and status reads for a task's live agent. `interrupt` maps
# to the RPC `abort_and_prompt`; the message field name mirrors `prompt`
# (`message`) — verified against real omp in this change's verification task.


class AgentMessage(BaseModel):
    message: str


def _require_live_agent(supervisor: AgentSupervisor, task_id: int) -> AgentHandle:
    handle = supervisor.get(task_id)
    if handle is None:
        raise HTTPException(status.HTTP_409_CONFLICT, str(NoLiveAgentError(task_id)))
    return handle


async def _agent_request(
    handle: AgentHandle, request_type: str, **fields: object
) -> dict[str, object]:
    """Issue an RPC request and turn agent-side failures into clean errors:
    a `success: false` response is a 502 (the agent rejected it), and a child
    that exits mid-request is a 409 (no live agent anymore)."""
    try:
        return await handle.request(request_type, **fields)
    except RequestFailedError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, f"agent rejected {request_type}: {exc}"
        ) from exc
    except AgentGoneError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"agent exited during {request_type}: {exc}"
        ) from exc


@router.post("/tasks/{task_id}/agent/steer")
async def steer_agent_route(
    task_id: int,
    body: AgentMessage,
    engine: Engine = Depends(_engine),
    supervisor: AgentSupervisor = Depends(_supervisor),
) -> dict[str, object]:
    _require_task(engine, task_id)
    handle = _require_live_agent(supervisor, task_id)
    return await _agent_request(handle, "steer", message=body.message)


@router.post("/tasks/{task_id}/agent/follow-up")
async def follow_up_agent_route(
    task_id: int,
    body: AgentMessage,
    engine: Engine = Depends(_engine),
    supervisor: AgentSupervisor = Depends(_supervisor),
) -> dict[str, object]:
    _require_task(engine, task_id)
    handle = _require_live_agent(supervisor, task_id)
    return await _agent_request(handle, "follow_up", message=body.message)


@router.post("/tasks/{task_id}/agent/interrupt")
async def interrupt_agent_route(
    task_id: int,
    body: AgentMessage,
    engine: Engine = Depends(_engine),
    supervisor: AgentSupervisor = Depends(_supervisor),
    sessions: SessionTracker = Depends(_sessions),
) -> dict[str, object]:
    _require_task(engine, task_id)
    handle = _require_live_agent(supervisor, task_id)
    # Any pending question is moot once the turn is aborted (design D-6); the
    # abort's own agent_start/agent_end then drives state normally.
    sessions.clear_pending(task_id)
    return await _agent_request(handle, "abort_and_prompt", message=body.message)


# --- Ask/approval answers (ask-approvals capability) ------------------------
# Replies to a pending `extension_ui_request` over the agent's stdin (design
# D-5). Reply shape confirmed against the omp source during dogfooding
# 2026-07-20 (see the `omp-rpc-field-assumptions` memory note): both `ask` and
# the approval gate use the same `method: "select"` dialog, answered with a
# single `{"value": <string>}` — approval's value must be literally "Approve"
# or "Deny" (an exact string match in the omp tool wrapper); there is no
# separate multi-value or free-text reply shape — arbitrary text is accepted
# as `value` with no membership check against the offered options.


class AgentAnswer(BaseModel):
    question_id: str
    selections: list[str] | None = None
    text: str | None = None
    approved: bool | None = None


def _answer_reply_payload(body: AgentAnswer) -> dict[str, object]:
    if body.approved is not None:
        return {"value": "Approve" if body.approved else "Deny"}
    if body.text is not None:
        return {"value": body.text}
    if body.selections:
        # The wire protocol carries one `value`; multi-select isn't exposed
        # over rpc-ui mode (see `_build_ask_pending`), so only the first
        # selection is sent.
        return {"value": body.selections[0]}
    return {"cancelled": True}


@router.post("/tasks/{task_id}/agent/answer")
async def answer_agent_route(
    task_id: int,
    body: AgentAnswer,
    engine: Engine = Depends(_engine),
    supervisor: AgentSupervisor = Depends(_supervisor),
    sessions: SessionTracker = Depends(_sessions),
) -> dict[str, object]:
    _require_task(engine, task_id)
    handle = _require_live_agent(supervisor, task_id)
    pending = sessions.pending(task_id)
    if pending is None or pending.id != body.question_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"task {task_id} has no pending question {body.question_id!r}",
        )
    await handle.respond_ui_request(body.question_id, _answer_reply_payload(body))
    # Optimistic clear (design D-5): omp sends no distinct "answer accepted"
    # frame, so the card disappears on send and the fan-out corrects any
    # surprise (e.g. a re-posted question arrives as a fresh question_posted).
    sessions.answer_pending(task_id)
    return {"task_id": task_id, "question_id": body.question_id, "answered": True}


@router.get("/tasks/{task_id}/agent/state")
async def agent_state_route(
    task_id: int,
    engine: Engine = Depends(_engine),
    supervisor: AgentSupervisor = Depends(_supervisor),
) -> dict[str, object]:
    _require_task(engine, task_id)
    handle = _require_live_agent(supervisor, task_id)
    # Pass the agent's `data` through untouched (isStreaming, queuedMessageCount,
    # todos, context usage, model); the daemon never reinterprets its meaning.
    response = await _agent_request(handle, "get_state")
    return response.get("data") or {}


@router.get("/tasks/{task_id}/agent/stats")
async def agent_stats_route(
    task_id: int,
    engine: Engine = Depends(_engine),
    supervisor: AgentSupervisor = Depends(_supervisor),
) -> dict[str, object]:
    _require_task(engine, task_id)
    handle = _require_live_agent(supervisor, task_id)
    response = await _agent_request(handle, "get_session_stats")
    return response.get("data") or {}


@router.delete("/tasks/{task_id}")
def purge_task_route(
    task_id: int,
    engine: Engine = Depends(_engine),
    events: EventHub = Depends(_events),
    sessions: SessionTracker = Depends(_sessions),
    advisories: AdvisorySampler = Depends(_advisories),
) -> dict[str, int]:
    try:
        purge_task(engine, task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except TaskNotArchivedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    sessions.discard(task_id)
    advisories.clear_task(task_id)
    events.publish("task_deleted", {"id": task_id})
    return {"deleted": task_id}
