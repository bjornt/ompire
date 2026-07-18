"""REST endpoints under /api/. Commands only — events go out over the WebSocket."""

from __future__ import annotations

from dataclasses import asdict

from sqlalchemy import Engine
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, field_validator

from ompire_daemon.auth import require_bearer_token
from ompire_daemon.config import Config
from ompire_daemon.events import EventHub
from ompire_daemon.registry.projects import (
    DuplicateProjectError,
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

router = APIRouter(prefix="/api", dependencies=[Depends(require_bearer_token)])


class ProjectCreate(BaseModel):
    name: str
    title: str
    upstream_url: str
    fork_url: str | None = None
    checkout_path: str | None = None

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


class ProjectOut(BaseModel):
    name: str
    title: str
    upstream_url: str
    fork_url: str | None
    checkout_path: str

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
            default_checkout_root=config.checkout_root,
        )
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
        )
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
