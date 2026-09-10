"""Work router: global model profiles (ADR-0025).

CRUD over the profile registry with committed-change events. The command
layer owns validation; this router owns the wire shapes and status codes.
Configuration only — nothing here reaches spawn, agent argv, or a running
session.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import Engine

from ompire_daemon.api.deps import _engine, _events
from ompire_daemon.api.work_models import (
    ModelProfileCreate,
    ModelProfileOut,
    ModelProfileUpdate,
)
from ompire_daemon.application import work
from ompire_daemon.events import EventHub
from ompire_daemon.model_config import (
    InvalidRoleBindingError,
    InvalidRoleSetError,
)
from ompire_daemon.work.profiles import (
    DuplicateModelProfileError,
    InvalidModelProfileNameError,
    ModelProfile,
    ModelProfileNotFoundError,
    ModelProfileReferencedError,
    get_model_profile,
    list_model_profiles,
)

router = APIRouter()


def _model_profile_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ModelProfileNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, (DuplicateModelProfileError, ModelProfileReferencedError)):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    # Invalid names ride FastAPI's request validation via the field validator;
    # role-set and binding refusals are registry-level 422s that name the role
    # and field the operator has to fix.
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))


def _profile_roles_payload(body: ModelProfileCreate | ModelProfileUpdate) -> dict[str, Any]:
    return {role: binding.model_dump() for role, binding in body.roles.items()}


@router.get("/model-profiles", response_model=list[ModelProfileOut])
def list_model_profiles_route(
    engine: Engine = Depends(_engine),
) -> list[ModelProfile]:
    return list_model_profiles(engine)


@router.post(
    "/model-profiles",
    response_model=ModelProfileOut,
    status_code=status.HTTP_201_CREATED,
)
def create_model_profile_route(
    body: ModelProfileCreate,
    engine: Engine = Depends(_engine),
    events: EventHub = Depends(_events),
) -> ModelProfile:
    try:
        return work.create_profile(
            engine, events, name=body.name, roles=_profile_roles_payload(body)
        )
    except (
        DuplicateModelProfileError,
        InvalidModelProfileNameError,
        InvalidRoleBindingError,
        InvalidRoleSetError,
    ) as exc:
        raise _model_profile_error(exc) from exc


@router.get("/model-profiles/{name}", response_model=ModelProfileOut)
def get_model_profile_route(
    name: str, engine: Engine = Depends(_engine)
) -> ModelProfile:
    try:
        return get_model_profile(engine, name)
    except ModelProfileNotFoundError as exc:
        raise _model_profile_error(exc) from exc


@router.put("/model-profiles/{name}", response_model=ModelProfileOut)
def update_model_profile_route(
    name: str,
    body: ModelProfileUpdate,
    engine: Engine = Depends(_engine),
    events: EventHub = Depends(_events),
) -> ModelProfile:
    try:
        return work.update_profile(
            engine, events, name, roles=_profile_roles_payload(body)
        )
    except (
        ModelProfileNotFoundError,
        InvalidRoleBindingError,
        InvalidRoleSetError,
    ) as exc:
        raise _model_profile_error(exc) from exc


@router.delete("/model-profiles/{name}")
def delete_model_profile_route(
    name: str, engine: Engine = Depends(_engine), events: EventHub = Depends(_events)
) -> dict[str, str]:
    try:
        work.delete_profile(engine, events, name)
    except (ModelProfileNotFoundError, ModelProfileReferencedError) as exc:
        raise _model_profile_error(exc) from exc
    return {"deleted": name}
