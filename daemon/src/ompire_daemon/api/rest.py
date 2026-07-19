"""REST endpoints under /api/. Commands only — events go out over the WebSocket."""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import asdict
from pathlib import Path

from sqlalchemy import Engine
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, field_validator

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
    job = asyncio.create_task(run_spawn_pipeline(engine, events, config, task.id, project))
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
    events.publish("task_updated", asdict(archived))
    return archived


@router.delete("/tasks/{task_id}")
def purge_task_route(
    task_id: int, engine: Engine = Depends(_engine), events: EventHub = Depends(_events)
) -> dict[str, int]:
    try:
        purge_task(engine, task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except TaskNotArchivedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    events.publish("task_deleted", {"id": task_id})
    return {"deleted": task_id}
