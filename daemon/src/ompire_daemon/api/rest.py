"""REST endpoints under /api/. Commands only — events go out over the WebSocket.

Architecture: ADR-0004 (docs/adr/0004-use-rest-and-websocket-snapshot-deltas.md)
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import shutil
from dataclasses import asdict
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, status
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import Engine

from ompire_daemon import auth
from ompire_daemon.advisories import AdvisorySampler
from ompire_daemon.agent import AgentHandle, AgentSupervisor, NoLiveAgentError
from ompire_daemon.auth import require_bearer_token
from ompire_daemon.config import Config
from ompire_daemon.datadir import audit_log_path_for
from ompire_daemon.events import EventHub
from ompire_daemon.gh import GitHubProbe
from ompire_daemon.gpg import (
    FINGERPRINT_RE,
    STATE_READY,
    GpgProbe,
    gpg_signing_refusal,
)
from ompire_daemon.model_config import InvalidThinkingLevelError, validate_thinking
from ompire_daemon.notifications import AttentionNotifier
from ompire_daemon.projectcheckout import (
    InvalidRemoteNameError,
    InvalidRepoUrlError,
    inspect_checkout,
    inspection_message,
    validate_remote_name,
    validate_repo_url,
)
from ompire_daemon.projectfiles import (
    DEFAULT_LIMIT as FILE_SEARCH_DEFAULT_LIMIT,
)
from ompire_daemon.projectfiles import (
    MAX_LIMIT as FILE_SEARCH_MAX_LIMIT,
)
from ompire_daemon.projectfiles import (
    ProjectFilesError,
    search_project_files,
    validate_mentions,
)
from ompire_daemon.projectsetup import (
    CLONE_FETCH_REMOTE,
    DestinationExistsError,
    ProjectSetupManager,
    clone_target,
)
from ompire_daemon.registry.model_profiles import (
    DuplicateModelProfileError,
    InvalidModelProfileNameError,
    InvalidRoleBindingError,
    InvalidRoleSetError,
    ModelProfile,
    ModelProfileNotFoundError,
    ModelProfileReferencedError,
    UnknownModelProfileReferenceError,
    create_model_profile,
    delete_model_profile,
    get_model_profile,
    list_model_profiles,
    update_model_profile,
    validate_profile_name,
)
from ompire_daemon.registry.projects import (
    DEFAULT_FETCH_REMOTE,
    UNSUPPLIED,
    DuplicateProjectError,
    Project,
    ProjectHasReferencingTasksError,
    ProjectNotFoundError,
    ProjectNotReadyError,
    ProjectSetupBusyError,
    create_project,
    delete_project,
    get_project,
    list_projects,
    update_project,
    validate_slug,
)
from ompire_daemon.registry.settings import (
    SettingsStore,
    SettingsValidationError,
    effective_checkout_root,
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
from ompire_daemon.registry.templates import (
    DuplicateTemplateError,
    InvalidBranchPatternError,
    InvalidWorkshopAdditionsError,
    Template,
    TemplateHasReferencingTasksError,
    TemplateNotFoundError,
    UnknownTemplateProjectError,
    UnknownWorkflowError,
    create_template,
    delete_template,
    get_template,
    list_templates,
    update_template,
)
from ompire_daemon.review import ReviewAlreadyOpenError, ReviewError, ReviewManager
from ompire_daemon.rpc import AgentGoneError, RequestFailedError
from ompire_daemon.sessions import SessionTracker
from ompire_daemon.ship import GitHubPreflightError, ShipError, ShipManager
from ompire_daemon.spawn import run_spawn_pipeline
from ompire_daemon.workflows import (
    JUDGE_SESSION,
    UnknownWorkflowNameError,
    Workflow,
    WorkflowNotWaitingError,
    WorkflowRunner,
    get_workflow,
)
from ompire_daemon.workshop import WorkshopRemoveError, remove_workshop, workshop_status

# REST authentication boundary: ADR-0002
# (docs/adr/0002-run-as-local-daemon-with-stateless-web-ui.md)
router = APIRouter(prefix="/api", dependencies=[Depends(require_bearer_token)])


class ProjectCreate(BaseModel):
    """`checkout_mode` defaults to `adopt`, which is what every registration
    before ADR-0022 meant: the operator supplies (or derives) a checkout that
    already exists. `clone` derives the destination from the effective
    checkout root and refuses a `checkout_path` of its own."""

    name: str
    title: str
    upstream_url: str
    fork_url: str | None = None
    checkout_path: str | None = None
    checkout_mode: str = "adopt"
    fetch_remote: str = DEFAULT_FETCH_REMOTE
    # Optional global model profile (ADR-0025). Omitted or null means no
    # default; nothing is inferred from templates, credentials, or the name.
    default_model_profile: str | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        validate_slug(value)
        return value

    @field_validator("checkout_mode")
    @classmethod
    def _validate_mode(cls, value: str) -> str:
        if value not in ("adopt", "clone"):
            raise ValueError("checkout_mode must be 'adopt' or 'clone'")
        return value


class ProjectUpdate(BaseModel):
    title: str
    upstream_url: str
    fork_url: str | None = None
    checkout_path: str
    fetch_remote: str = DEFAULT_FETCH_REMOTE
    new_name: str | None = None
    # Three-valued on update: absent from the body preserves the stored
    # reference, explicit null clears it, a name selects that profile. The
    # route reads `model_fields_set` to tell the first two apart, so a caller
    # written before profiles existed cannot clear one by not mentioning it.
    default_model_profile: str | None = None

    @field_validator("new_name")
    @classmethod
    def _validate_new_name(cls, value: str | None) -> str | None:
        if value is not None:
            validate_slug(value)
        return value


class CheckoutInspectIn(BaseModel):
    checkout_path: str
    fetch_remote: str = DEFAULT_FETCH_REMOTE


class RemoteOut(BaseModel):
    name: str
    url: str


class CheckoutInspectOut(BaseModel):
    """What a read-only look at a candidate checkout found. Remote names and
    URLs only — never file contents, and nothing is written to the path."""

    ok: bool
    reason: str
    detail: str
    remotes: list[RemoteOut]
    suggested_upstream: str | None = None
    suggested_fork: str | None = None


class ProjectOut(BaseModel):
    name: str
    title: str
    upstream_url: str
    fork_url: str | None
    checkout_path: str
    checkout_mode: str
    fetch_remote: str
    setup_state: str
    setup_error: str | None
    default_model_profile: str | None

    model_config = {"from_attributes": True}


def _engine(request: Request) -> Engine:
    return request.app.state.engine


def _config(request: Request) -> Config:
    return request.app.state.config


def _settings(request: Request) -> SettingsStore:
    return request.app.state.settings_store


def _events(request: Request) -> EventHub:
    return request.app.state.events


def _sessions(request: Request) -> SessionTracker:
    return request.app.state.sessions


def _advisories(request: Request) -> AdvisorySampler:
    return request.app.state.advisories


def _notifications(request: Request) -> AttentionNotifier:
    return request.app.state.notifications


def _reviews(request: Request) -> ReviewManager:
    return request.app.state.reviews


def _ships(request: Request) -> ShipManager:
    return request.app.state.ships


def _gpg(request: Request) -> GpgProbe:
    return request.app.state.gpg


def _assert_selectable_signing_key(value: Any, gpg: GpgProbe) -> None:
    """Bound a stored signing selection to the host keyring (ADR-0021).

    The settings store validates the fingerprint's *form*; only the probe
    knows which keys actually exist, so membership is checked here — before
    anything is persisted.
    """
    if not isinstance(value, str) or not FINGERPRINT_RE.match(value):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "gpg_signing_key: must be a 40-character OpenPGP fingerprint",
        )
    wanted = value.upper()
    known = {candidate.fingerprint for candidate in gpg.candidates()}
    if wanted not in known:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"gpg_signing_key: {wanted} is not a usable signing key in the "
            "daemon's keyring",
        )


def _gh(request: Request) -> GitHubProbe:
    return request.app.state.gh


def _project_setup(request: Request) -> ProjectSetupManager:
    return request.app.state.project_setup


def _require_ready_project(engine: Engine, name: str) -> Project:
    """Resolve a project that a task may actually use.

    A project whose checkout is still being created, or whose creation failed,
    has no usable clone source; letting a task start against it only defers
    the failure to the spawn pipeline's first git command (ADR-0022).
    """
    try:
        project = get_project(engine, name)
    except ProjectNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    _refuse_unready(project)
    return project


def _refuse_unready(project: Project) -> None:
    if project.setup_state != "ready":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            str(ProjectNotReadyError(project.name, project.setup_state)),
        )


def _refuse_unready_named(engine: Engine, name: str) -> None:
    """Readiness guard for template routes.

    An *unknown* project is deliberately left to the template registry, whose
    `UnknownTemplateProjectError` is already a 422; only an existing but
    unusable project is refused here.
    """
    try:
        project = get_project(engine, name)
    except ProjectNotFoundError:
        return
    _refuse_unready(project)


def _apply_settings_live(
    settings: dict[str, Any],
    events: EventHub,
    notifications: AttentionNotifier,
    advisories: AdvisorySampler,
    sessions: SessionTracker,
) -> None:
    """Push a new effective settings map to every live consumer and broadcast
    the change to WebSocket clients."""
    notifications.apply_settings(settings)
    advisories.set_threshold(settings["context_advisory_threshold"])
    sessions.set_stall_threshold(settings["stall_threshold"])
    events.publish("settings_changed", {"settings": settings})


async def _close_all_ws(connections: set[WebSocket]) -> None:
    """Close every tracked WebSocket with policy-violation code 1008."""
    for ws in list(connections):
        try:
            await ws.close(code=1008, reason="token rotated")
        except Exception:  # noqa: BLE001 — best-effort close
            pass


logger = logging.getLogger(__name__)


@router.get("/projects", response_model=list[ProjectOut])
def list_projects_route(engine: Engine = Depends(_engine)) -> list[Project]:
    return list_projects(engine)


def _validated_urls(body: ProjectCreate | ProjectUpdate) -> tuple[str, str | None]:
    """Upstream and fork, refused before they can become `git` argv."""
    try:
        upstream = validate_repo_url("upstream_url", body.upstream_url)
        fork = (
            validate_repo_url("fork_url", body.fork_url)
            if body.fork_url and body.fork_url.strip()
            else None
        )
    except InvalidRepoUrlError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)
        ) from exc
    return upstream, fork


def _validated_remote(name: str) -> str:
    try:
        return validate_remote_name(name)
    except InvalidRemoteNameError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)
        ) from exc


async def _require_usable_checkout(
    checkout_path: str, fetch_remote: str, timeout: int
) -> None:
    """Refuse an adopted checkout Ompire cannot clone a task workspace from.

    Read-only: this only looks (ADR-0022).
    """
    inspection = await inspect_checkout(
        checkout_path, fetch_remote=fetch_remote, timeout=timeout
    )
    if not inspection.ok:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            inspection_message(inspection, fetch_remote),
        )


@router.post("/projects/checkout-inspect", response_model=CheckoutInspectOut)
async def inspect_checkout_route(
    body: CheckoutInspectIn,
    config: Config = Depends(_config),
) -> CheckoutInspectOut:
    """Look at a candidate checkout so the create form can prefill and explain.

    Answers for an unregistered path, reads only remote names and URLs, and
    writes nothing. A refusal is a successful response describing why, not an
    error — the operator is still typing.
    """
    fetch_remote = _validated_remote(body.fetch_remote)
    inspection = await inspect_checkout(
        body.checkout_path,
        fetch_remote=fetch_remote,
        timeout=config.spawn_step_timeout,
    )
    return CheckoutInspectOut(
        ok=inspection.ok,
        reason=inspection.reason,
        detail=inspection_message(inspection, fetch_remote),
        remotes=[RemoteOut(name=r.name, url=r.url) for r in inspection.remotes],
        suggested_upstream=inspection.suggested_upstream,
        suggested_fork=inspection.suggested_fork,
    )


@router.post(
    "/projects", response_model=ProjectOut, status_code=status.HTTP_201_CREATED
)
async def create_project_route(
    body: ProjectCreate,
    engine: Engine = Depends(_engine),
    config: Config = Depends(_config),
    events: EventHub = Depends(_events),
    settings: SettingsStore = Depends(_settings),
    setup: ProjectSetupManager = Depends(_project_setup),
) -> Project:
    """Register a project, adopting an existing checkout or creating one.

    Adoption is answered here: validation is a handful of local git reads, so
    the operator gets ready-or-why in the response. Clone mode returns a
    `cloning` project immediately and continues in the background.
    """
    upstream_url, fork_url = _validated_urls(body)
    if body.checkout_mode == "clone":
        if body.checkout_path:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "checkout_path cannot be supplied in clone mode; the "
                "destination is derived from the effective checkout root",
            )
        # Derived, never supplied — that is what bounds the one place Ompire
        # creates a repository outside its task root (ADR-0022/0023).
        target = clone_target(
            effective_checkout_root(settings.effective()), body.name
        )
        if target.destination.exists():
            raise HTTPException(
                status.HTTP_409_CONFLICT, str(DestinationExistsError(target.destination))
            )
        checkout_path: str | None = str(target.destination)
        fetch_remote = CLONE_FETCH_REMOTE
        checkout_mode, setup_state = "cloned", "cloning"
    else:
        fetch_remote = _validated_remote(body.fetch_remote)
        checkout_path = body.checkout_path or str(
            effective_checkout_root(settings.effective()) / body.name
        )
        await _require_usable_checkout(
            checkout_path, fetch_remote, config.spawn_step_timeout
        )
        checkout_mode, setup_state = "adopted", "ready"

    try:
        # Both modes converge here, so an unknown profile is refused before any
        # row exists — and, in clone mode, before a clone job is scheduled.
        project = create_project(
            engine,
            name=body.name,
            title=body.title,
            upstream_url=upstream_url,
            fork_url=fork_url,
            checkout_path=checkout_path,
            default_checkout_root=config.checkout_root,
            checkout_mode=checkout_mode,
            fetch_remote=fetch_remote,
            setup_state=setup_state,
            default_model_profile=body.default_model_profile,
        )
    except DuplicateProjectError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except UnknownModelProfileReferenceError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)
        ) from exc
    events.publish("project_created", asdict(project))
    if project.setup_state == "cloning":
        setup.start(project)
    return project


@router.post(
    "/projects/{name}/setup/retry",
    response_model=ProjectOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def retry_project_setup_route(
    name: str,
    setup: ProjectSetupManager = Depends(_project_setup),
) -> Project:
    # Must be async: `retry` schedules the clone job on the running loop, and
    # a sync route would run in FastAPI's threadpool where there is none.
    try:
        return setup.retry(name)
    except ProjectNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.get("/projects/{name}", response_model=ProjectOut)
def get_project_route(name: str, engine: Engine = Depends(_engine)) -> Project:
    try:
        return get_project(engine, name)
    except ProjectNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


class ProjectFilesOut(BaseModel):
    """Repository-relative path names only — never contents, sizes, or
    absolute paths (add-spawn-file-mentions)."""

    paths: list[str]
    truncated: bool


@router.get("/projects/{name}/files", response_model=ProjectFilesOut)
async def search_project_files_route(
    name: str,
    q: str = "",
    limit: int = FILE_SEARCH_DEFAULT_LIMIT,
    engine: Engine = Depends(_engine),
    config: Config = Depends(_config),
) -> ProjectFilesOut:
    """List the project's repository files for the Spawn view's `@` mentions.

    A client-supplied `limit` cannot exceed the server's hard maximum. An
    unusable checkout is a 409, never an empty success — "your checkout is
    gone" and "no matches" must not read the same.
    """
    try:
        project = get_project(engine, name)
    except ProjectNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    try:
        result = await search_project_files(
            project.checkout_path,
            query=q,
            limit=min(max(limit, 1), FILE_SEARCH_MAX_LIMIT),
            timeout=config.spawn_step_timeout,
        )
    except ProjectFilesError as exc:
        # Missing checkout, not-a-repository, git failure, timeout: all state
        # the operator has to fix, each carrying its own message.
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return ProjectFilesOut(paths=result.paths, truncated=result.truncated)


@router.put("/projects/{name}", response_model=ProjectOut)
async def update_project_route(
    name: str,
    body: ProjectUpdate,
    engine: Engine = Depends(_engine),
    config: Config = Depends(_config),
    events: EventHub = Depends(_events),
) -> Project:
    upstream_url, fork_url = _validated_urls(body)
    fetch_remote = _validated_remote(body.fetch_remote)
    try:
        current = get_project(engine, name)
    except ProjectNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    if current.setup_state == "cloning":
        raise HTTPException(
            status.HTTP_409_CONFLICT, str(ProjectSetupBusyError(name))
        )
    # The checkout mode is fixed at registration: a cloned project's checkout
    # is Ompire's own derived path, and repointing it would silently orphan
    # what was created (ADR-0022).
    if current.checkout_mode == "cloned" and body.checkout_path != current.checkout_path:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"project {name!r} uses a checkout Ompire created; its path cannot "
            "be changed",
        )
    checkout_changed = (
        body.checkout_path != current.checkout_path
        or fetch_remote != current.fetch_remote
    )
    if current.setup_state == "ready" and checkout_changed:
        await _require_usable_checkout(
            body.checkout_path, fetch_remote, config.spawn_step_timeout
        )
    try:
        project = update_project(
            engine,
            name,
            title=body.title,
            upstream_url=upstream_url,
            fork_url=fork_url,
            checkout_path=body.checkout_path,
            fetch_remote=fetch_remote,
            new_name=body.new_name,
            # Absent from the body means "leave it alone"; the registry
            # resolves that against the stored row inside its write
            # transaction, not against the `current` read above.
            default_model_profile=(
                body.default_model_profile
                if "default_model_profile" in body.model_fields_set
                else UNSUPPLIED
            ),
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except DuplicateProjectError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ProjectHasReferencingTasksError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except UnknownModelProfileReferenceError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)
        ) from exc
    renamed = body.new_name is not None and body.new_name != name
    if renamed:
        # Keyed-by-name consumers can't match a renamed payload via
        # `project_updated`; the rename event carries the old key.
        events.publish(
            "project_renamed", {"old_name": name, "project": asdict(project)}
        )
    else:
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
    except ProjectSetupBusyError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ProjectHasReferencingTasksError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    events.publish("project_deleted", {"name": name})
    return {"deleted": name}


# --- Model profiles ---------------------------------------------------------
# ADR-0025: global named model-role bindings. Configuration only in this
# change — nothing here reaches spawn, agent argv, or a running session.


class RoleBindingIn(BaseModel):
    """One role's pair. Both fields are required and neither may be null: a
    binding that cannot say which model and how much reasoning is not one."""

    model_config = ConfigDict(extra="forbid")

    model: str
    thinking: str


class ModelProfileCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    roles: dict[str, RoleBindingIn]

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        validate_profile_name(value)
        return value


class ModelProfileUpdate(BaseModel):
    """The name is the stable identifier, so an update replaces only the
    bindings — and replaces all four of them together."""

    model_config = ConfigDict(extra="forbid")

    roles: dict[str, RoleBindingIn]


class RoleBindingOut(BaseModel):
    model: str
    thinking: str

    model_config = {"from_attributes": True}


class ModelProfileOut(BaseModel):
    name: str
    roles: dict[str, RoleBindingOut]
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


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


def _profile_payload(profile: ModelProfile) -> dict[str, Any]:
    """Event/response shape: the same nested role map the REST body uses."""
    return asdict(profile)


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
        profile = create_model_profile(
            engine, name=body.name, roles=_profile_roles_payload(body)
        )
    except (
        DuplicateModelProfileError,
        InvalidModelProfileNameError,
        InvalidRoleBindingError,
        InvalidRoleSetError,
    ) as exc:
        raise _model_profile_error(exc) from exc
    events.publish("model_profile_created", _profile_payload(profile))
    return profile


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
        profile = update_model_profile(
            engine, name, roles=_profile_roles_payload(body)
        )
    except (
        ModelProfileNotFoundError,
        InvalidRoleBindingError,
        InvalidRoleSetError,
    ) as exc:
        raise _model_profile_error(exc) from exc
    events.publish("model_profile_updated", _profile_payload(profile))
    return profile


@router.delete("/model-profiles/{name}")
def delete_model_profile_route(
    name: str, engine: Engine = Depends(_engine), events: EventHub = Depends(_events)
) -> dict[str, str]:
    try:
        delete_model_profile(engine, name)
    except (ModelProfileNotFoundError, ModelProfileReferencedError) as exc:
        raise _model_profile_error(exc) from exc
    events.publish("model_profile_deleted", {"name": name})
    return {"deleted": name}


# --- Templates --------------------------------------------------------------
# SPEC Decision 6: spawn configuration lives on templates; projects carry
# only identity/checkout/remotes (Decision 9).


class TemplateCreate(BaseModel):
    name: str
    project_name: str
    base_branch: str = "main"
    branch_pattern: str | None = None
    workflow: str = "single-step"
    workshop_additions: str = "project"
    model: str | None = None
    thinking: str | None = None
    preamble: str = ""

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        validate_slug(value)
        return value


class TemplateUpdate(BaseModel):
    project_name: str
    base_branch: str
    branch_pattern: str
    workflow: str
    workshop_additions: str
    model: str | None = None
    thinking: str | None = None
    preamble: str = ""


class TemplateOut(BaseModel):
    name: str
    project_name: str
    base_branch: str
    branch_pattern: str
    workflow: str
    workshop_additions: str
    model: str | None
    thinking: str | None
    preamble: str
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


def _template_error(exc: Exception) -> HTTPException:
    if isinstance(exc, TemplateNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, (DuplicateTemplateError, TemplateHasReferencingTasksError)):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    # InvalidSlugError rides FastAPI's request validation (field_validator);
    # the rest are registry-level 422s.
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))


@router.get("/templates", response_model=list[TemplateOut])
def list_templates_route(engine: Engine = Depends(_engine)) -> list[Template]:
    return list_templates(engine)


@router.post(
    "/templates", response_model=TemplateOut, status_code=status.HTTP_201_CREATED
)
def create_template_route(
    body: TemplateCreate,
    engine: Engine = Depends(_engine),
    config: Config = Depends(_config),
    events: EventHub = Depends(_events),
) -> Template:
    _refuse_unready_named(engine, body.project_name)
    try:
        template = create_template(
            engine,
            name=body.name,
            project_name=body.project_name,
            base_branch=body.base_branch,
            branch_pattern=body.branch_pattern or config.default_branch_pattern,
            workflow=body.workflow,
            workshop_additions=body.workshop_additions,
            model=body.model,
            thinking=body.thinking,
            preamble=body.preamble,
        )
    except (
        InvalidBranchPatternError,
        InvalidThinkingLevelError,
        InvalidWorkshopAdditionsError,
        UnknownTemplateProjectError,
        UnknownWorkflowError,
        DuplicateTemplateError,
    ) as exc:
        raise _template_error(exc) from exc
    events.publish("template_created", asdict(template))
    return template


@router.get("/templates/{name}", response_model=TemplateOut)
def get_template_route(name: str, engine: Engine = Depends(_engine)) -> Template:
    try:
        return get_template(engine, name)
    except TemplateNotFoundError as exc:
        raise _template_error(exc) from exc


@router.put("/templates/{name}", response_model=TemplateOut)
def update_template_route(
    name: str,
    body: TemplateUpdate,
    engine: Engine = Depends(_engine),
    events: EventHub = Depends(_events),
) -> Template:
    _refuse_unready_named(engine, body.project_name)
    try:
        template = update_template(
            engine,
            name,
            project_name=body.project_name,
            base_branch=body.base_branch,
            branch_pattern=body.branch_pattern,
            workflow=body.workflow,
            workshop_additions=body.workshop_additions,
            model=body.model,
            thinking=body.thinking,
            preamble=body.preamble,
        )
    except (
        TemplateNotFoundError,
        InvalidBranchPatternError,
        InvalidThinkingLevelError,
        InvalidWorkshopAdditionsError,
        UnknownTemplateProjectError,
        UnknownWorkflowError,
    ) as exc:
        raise _template_error(exc) from exc
    events.publish("template_updated", asdict(template))
    return template


@router.delete("/templates/{name}")
def delete_template_route(
    name: str, engine: Engine = Depends(_engine), events: EventHub = Depends(_events)
) -> dict[str, str]:
    try:
        delete_template(engine, name)
    except (TemplateNotFoundError, TemplateHasReferencingTasksError) as exc:
        raise _template_error(exc) from exc
    events.publish("template_deleted", {"name": name})
    return {"deleted": name}


class TaskCreate(BaseModel):
    template_name: str
    slug: str
    prompt: str
    model: str | None = None
    thinking: str | None = None

    @field_validator("slug")
    @classmethod
    def _validate_slug(cls, value: str) -> str:
        validate_task_slug(value)
        return value

    @field_validator("thinking")
    @classmethod
    def _validate_thinking(cls, value: str | None) -> str | None:
        if value is not None:
            validate_thinking(value)
        return value


class TaskOut(BaseModel):
    id: int
    project_name: str
    template_name: str | None
    slug: str
    branch: str
    clone_path: str
    state: str
    prompt: str
    error: str | None
    workshop_id: str | None
    workflow_name: str
    workflow_status: str | None
    workflow_step: str | None
    pr_url: str | None
    pr_state: str | None
    pr_merged_at: str | None
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
async def get_task_route(
    task_id: int, engine: Engine = Depends(_engine)
) -> TaskDetailOut:
    try:
        task = get_task(engine, task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    derived_status = (
        await workshop_status(task.clone_path) if task.workshop_id else None
    )
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
        template = get_template(engine, body.template_name)
    except TemplateNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    project = _require_ready_project(engine, template.project_name)

    # Mentions are validated before anything is created: Omp drops one it
    # cannot resolve without a word (findings-omp-file-mentions.md), so a
    # mention that will not survive into the clone must be refused here, not
    # discovered after the workspace is built.
    try:
        rejections = await validate_mentions(
            body.prompt,
            checkout_path=project.checkout_path,
            base_branch=template.base_branch,
            timeout=config.spawn_step_timeout,
        )
    except ProjectFilesError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    if rejections:
        detail = "; ".join(rejection.message() for rejection in rejections)
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"prompt file mention rejected — {detail}",
        )

    try:
        clone_path = clone_path_for(config.task_dir_root, project.name, body.slug)
    except ClonePathOutsideRootError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    branch = template.branch_pattern.replace("<slug>", body.slug)
    try:
        task = create_task(
            engine,
            project_name=project.name,
            template_name=template.name,
            slug=body.slug,
            branch=branch,
            clone_path=str(clone_path),
            prompt=body.prompt,
            workflow_name=template.workflow,
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
            request.app.state.workflow_runner,
            model_override=body.model,
            thinking_override=body.thinking,
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
    reviews: ReviewManager = Depends(_reviews),
    ships: ShipManager = Depends(_ships),
    notifications: AttentionNotifier = Depends(_notifications),
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

    await reviews.cancel_and_drop(task_id)
    await ships.cancel_and_drop(task_id)
    archived = mark_archived(engine, task_id)
    sessions.discard(task_id)
    advisories.clear_task(task_id)
    notifications.clear_task(task_id)
    events.publish("task_updated", asdict(archived))
    return archived


# --- Agent control surface --------------------------------------------------
# Start and prompt are the workflow engine's job now (workflow-engine design
# D-4); stop stays as the manual kill switch, feeding the tracker's
# operator-stop reason. Everything session-scoped is addressed
# `/tasks/{id}/sessions/{name}/agent/*` (design D-1).


def _supervisor(request: Request) -> AgentSupervisor:
    return request.app.state.agents


def _require_task(engine: Engine, task_id: int) -> Task:
    try:
        return get_task(engine, task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


def _workflow_for(task: Task) -> Workflow:
    """The task's registered workflow definition; 409 if its name is no
    longer registered (e.g. a workflow removed while tasks reference it)."""
    try:
        return get_workflow(task.workflow_name)
    except UnknownWorkflowNameError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


def _require_declared_session(task: Task, session: str) -> None:
    """404 on a session the task's workflow does not declare (design D-1).
    The engine-reserved judge session (bugfix-workflow design D-4) is
    admitted: it is spawned by the engine itself, never addressable in
    advance, and its transcript must stay inspectable."""
    workflow = _workflow_for(task)
    if session != JUDGE_SESSION and session not in workflow.sessions:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"task {task.id} workflow {workflow.name!r} declares no session {session!r}",
        )


def _primary_session(task: Task) -> str:
    """The workflow-declared primary session (design D-8): the target of
    task-scoped operations that mean "the agent" (review, ship)."""
    return _workflow_for(task).primary


@router.post("/tasks/{task_id}/sessions/{session}/agent/stop")
async def stop_agent_route(
    task_id: int,
    session: str,
    engine: Engine = Depends(_engine),
    supervisor: AgentSupervisor = Depends(_supervisor),
    sessions: SessionTracker = Depends(_sessions),
) -> dict[str, object]:
    task = _require_task(engine, task_id)
    _require_declared_session(task, session)
    # Flag before the kill so the exit lands as "stopped by operator", not a
    # crash (design D-2); cleared again if there was nothing to stop.
    sessions.expect_operator_stop(task_id, session)
    try:
        await supervisor.stop(task_id, session)
    except NoLiveAgentError as exc:
        sessions.clear_operator_stop(task_id, session)
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {"task_id": task_id, "session": session, "agent": "stopped"}


# --- Agent interaction: thin proxies over the live AgentHandle (design D-1) ---
# Composer actions and status reads for a session's live agent. `interrupt`
# maps to the RPC `abort_and_prompt`; the message field name mirrors `prompt`
# (`message`) — verified against real omp in this change's verification task.


class AgentMessage(BaseModel):
    message: str


def _require_live_agent(
    supervisor: AgentSupervisor, task_id: int, session: str
) -> AgentHandle:
    handle = supervisor.get(task_id, session)
    if handle is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, str(NoLiveAgentError(task_id, session))
        )
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


@router.post("/tasks/{task_id}/sessions/{session}/agent/steer")
async def steer_agent_route(
    task_id: int,
    session: str,
    body: AgentMessage,
    engine: Engine = Depends(_engine),
    supervisor: AgentSupervisor = Depends(_supervisor),
) -> dict[str, object]:
    task = _require_task(engine, task_id)
    _require_declared_session(task, session)
    handle = _require_live_agent(supervisor, task_id, session)
    return await _agent_request(handle, "steer", message=body.message)


@router.post("/tasks/{task_id}/sessions/{session}/agent/follow-up")
async def follow_up_agent_route(
    task_id: int,
    session: str,
    body: AgentMessage,
    engine: Engine = Depends(_engine),
    supervisor: AgentSupervisor = Depends(_supervisor),
) -> dict[str, object]:
    task = _require_task(engine, task_id)
    _require_declared_session(task, session)
    handle = _require_live_agent(supervisor, task_id, session)
    return await _agent_request(handle, "follow_up", message=body.message)


@router.post("/tasks/{task_id}/sessions/{session}/agent/interrupt")
async def interrupt_agent_route(
    task_id: int,
    session: str,
    body: AgentMessage,
    engine: Engine = Depends(_engine),
    supervisor: AgentSupervisor = Depends(_supervisor),
    sessions: SessionTracker = Depends(_sessions),
) -> dict[str, object]:
    task = _require_task(engine, task_id)
    _require_declared_session(task, session)
    handle = _require_live_agent(supervisor, task_id, session)
    # Any pending question is moot once the turn is aborted (design D-6); the
    # abort's own agent_start/agent_end then drives state normally.
    sessions.clear_pending(task_id, session)
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


@router.post("/tasks/{task_id}/sessions/{session}/agent/answer")
async def answer_agent_route(
    task_id: int,
    session: str,
    body: AgentAnswer,
    engine: Engine = Depends(_engine),
    supervisor: AgentSupervisor = Depends(_supervisor),
    sessions: SessionTracker = Depends(_sessions),
) -> dict[str, object]:
    task = _require_task(engine, task_id)
    _require_declared_session(task, session)
    handle = _require_live_agent(supervisor, task_id, session)
    pending = sessions.pending(task_id, session)
    if pending is None or pending.id != body.question_id:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"task {task_id} session {session!r} has no pending question {body.question_id!r}",
        )
    await handle.respond_ui_request(body.question_id, _answer_reply_payload(body))
    # Optimistic clear (design D-5): omp sends no distinct "answer accepted"
    # frame, so the card disappears on send and the fan-out corrects any
    # surprise (e.g. a re-posted question arrives as a fresh question_posted).
    sessions.answer_pending(task_id, session)
    return {
        "task_id": task_id,
        "session": session,
        "question_id": body.question_id,
        "answered": True,
    }


@router.get("/tasks/{task_id}/sessions/{session}/agent/state")
async def agent_state_route(
    task_id: int,
    session: str,
    engine: Engine = Depends(_engine),
    supervisor: AgentSupervisor = Depends(_supervisor),
) -> dict[str, object]:
    task = _require_task(engine, task_id)
    _require_declared_session(task, session)
    handle = _require_live_agent(supervisor, task_id, session)
    # Pass the agent's `data` through untouched (isStreaming, queuedMessageCount,
    # todos, context usage, model); the daemon never reinterprets its meaning.
    response = await _agent_request(handle, "get_state")
    data = response.get("data")
    return data if isinstance(data, dict) else {}


@router.get("/tasks/{task_id}/sessions/{session}/agent/stats")
async def agent_stats_route(
    task_id: int,
    session: str,
    engine: Engine = Depends(_engine),
    supervisor: AgentSupervisor = Depends(_supervisor),
) -> dict[str, object]:
    task = _require_task(engine, task_id)
    _require_declared_session(task, session)
    handle = _require_live_agent(supervisor, task_id, session)
    response = await _agent_request(handle, "get_session_stats")
    data = response.get("data")
    return data if isinstance(data, dict) else {}


# --- Workflow gates (workflow-engine capability) ----------------------------


class WorkflowResumeBody(BaseModel):
    note: str | None = None


@router.post("/tasks/{task_id}/workflow/resume")
async def resume_workflow_route(
    task_id: int,
    body: WorkflowResumeBody,
    request: Request,
    engine: Engine = Depends(_engine),
) -> dict[str, object]:
    task = _require_task(engine, task_id)
    runner: WorkflowRunner = request.app.state.workflow_runner
    try:
        runner.resume_gate(task.id, note=body.note)
    except WorkflowNotWaitingError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {"task_id": task.id, "workflow": "resumed", "step": task.workflow_step}


@router.post("/tasks/{task_id}/review")
async def start_review_route(
    task_id: int,
    engine: Engine = Depends(_engine),
    supervisor: AgentSupervisor = Depends(_supervisor),
    sessions: SessionTracker = Depends(_sessions),
    reviews: ReviewManager = Depends(_reviews),
) -> dict[str, Any]:
    try:
        task = get_task(engine, task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    # Review gates on the workflow's primary session (workflow-engine D-8).
    primary = _primary_session(task)
    session_info = sessions.get(task_id, primary)
    if session_info is None or session_info.status != "idle":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"task {task_id} session {primary!r} is not idle",
        )
    if supervisor.get(task_id, primary) is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"task {task_id} session {primary!r} has no live agent",
        )

    try:
        state = await reviews.start_review(task)
    except ReviewAlreadyOpenError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ReviewError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"review could not be started: {exc}",
        ) from exc
    return {
        "task_id": task_id,
        "status": state.status,
        "url": state.url,
        "port": state.port,
        "iterations": [
            {
                "outcome": it.outcome,
                "comment_count": it.comment_count,
                "stderr": it.stderr,
                "recorded_at": it.recorded_at,
            }
            for it in state.iterations
        ],
    }


@router.post("/tasks/{task_id}/review/cancel")
async def cancel_review_route(
    task_id: int,
    engine: Engine = Depends(_engine),
    reviews: ReviewManager = Depends(_reviews),
) -> dict[str, Any]:
    try:
        get_task(engine, task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    try:
        state = await reviews.cancel_review(task_id)
    except ReviewError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {
        "task_id": task_id,
        "status": state.status,
        "url": state.url,
        "port": state.port,
        "iterations": [
            {
                "outcome": it.outcome,
                "comment_count": it.comment_count,
                "stderr": it.stderr,
                "recorded_at": it.recorded_at,
            }
            for it in state.iterations
        ],
    }


class ShipDraftBody(BaseModel):
    replace: bool = False


class ShipCommitBody(BaseModel):
    message: str
    pr_title: str
    pr_body: str
    mode: str = "squash"


_IN_FLIGHT_SHIP_STATUSES = {"drafting", "committing", "pushing"}


@router.post("/tasks/{task_id}/ship/draft")
async def draft_ship_route(
    task_id: int,
    body: ShipDraftBody | None = None,
    engine: Engine = Depends(_engine),
    ships: ShipManager = Depends(_ships),
) -> dict[str, Any]:
    try:
        task = get_task(engine, task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    try:
        state = await ships.draft(
            task, replace=body.replace if body is not None else False
        )
    except ShipError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return asdict(state)


@router.post("/tasks/{task_id}/ship/commit")
async def commit_ship_route(
    task_id: int,
    body: ShipCommitBody,
    request: Request,
    engine: Engine = Depends(_engine),
    gpg: GpgProbe = Depends(_gpg),
    ships: ShipManager = Depends(_ships),
) -> dict[str, Any]:
    try:
        task = get_task(engine, task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc

    if body.mode not in ("squash", "retain"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"ship mode {body.mode!r} is not supported; only 'squash' or 'retain' are available",
        )

    existing = ships.get(task_id)
    if existing is not None and existing.status in _IN_FLIGHT_SHIP_STATUSES:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"task {task_id} already has a ship in flight",
        )
    try:
        await ships.preflight(task)
    except GitHubPreflightError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={"message": str(exc), "gh": asdict(exc.status)},
        ) from exc

    gpg_status = await gpg.probe()
    if gpg_status.state != STATE_READY:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "message": gpg_signing_refusal(gpg_status),
                "gpg": asdict(gpg_status),
            },
        )

    if body.mode == "retain":
        try:
            await ships.check_retain_preconditions(task)
        except ShipError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    ships.seed_commit(task.id, mode=body.mode)

    job = asyncio.create_task(
        ships.commit_and_ship(
            task,
            body.message,
            body.pr_title,
            body.pr_body,
            mode=body.mode,
        )
    )
    jobs: set[asyncio.Task] = request.app.state.spawn_jobs
    jobs.add(job)
    job.add_done_callback(jobs.discard)

    state = ships.get(task_id)
    assert state is not None
    return asdict(state)


@router.get("/gpg")
async def get_gpg_route(gpg: GpgProbe = Depends(_gpg)) -> dict[str, Any]:
    return asdict(gpg.current())


@router.post("/gpg/recheck")
async def recheck_gpg_route(gpg: GpgProbe = Depends(_gpg)) -> dict[str, Any]:
    return asdict(await gpg.probe())


class GitHubRecheckBody(BaseModel):
    task_id: int | None = None


@router.get("/gh")
async def get_gh_route(gh: GitHubProbe = Depends(_gh)) -> dict[str, Any]:
    """Return the last safe GitHub observation without starting a probe."""

    return asdict(gh.current())


@router.post("/gh/recheck")
async def recheck_gh_route(
    body: GitHubRecheckBody | None = None,
    engine: Engine = Depends(_engine),
    gh: GitHubProbe = Depends(_gh),
) -> dict[str, Any]:
    """Refresh global identity, optionally together with one task's trusted target."""

    if body is None or body.task_id is None:
        return asdict(await gh.probe())
    task = _require_task(engine, body.task_id)
    project = get_project(engine, task.project_name)
    status, _target = await gh.probe_target(project.upstream_url)
    return asdict(status)


@router.delete("/tasks/{task_id}")
def purge_task_route(
    task_id: int,
    engine: Engine = Depends(_engine),
    events: EventHub = Depends(_events),
    sessions: SessionTracker = Depends(_sessions),
    advisories: AdvisorySampler = Depends(_advisories),
    reviews: ReviewManager = Depends(_reviews),
    ships: ShipManager = Depends(_ships),
    notifications: AttentionNotifier = Depends(_notifications),
) -> dict[str, int]:
    try:
        purge_task(engine, task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except TaskNotArchivedError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    reviews.drop_review(task_id)
    ships.drop_ship(task_id)
    sessions.discard(task_id)
    advisories.clear_task(task_id)
    notifications.clear_task(task_id)
    events.publish("task_deleted", {"id": task_id})
    return {"deleted": task_id}


# --- Settings / daemon info / token (daemon-settings capability) ----------


class SettingsOut(BaseModel):
    settings: dict[str, Any]
    provenance: dict[str, str]


@router.get("/settings", response_model=SettingsOut)
def get_settings_route(
    settings_store: SettingsStore = Depends(_settings),
) -> SettingsOut:
    result = settings_store.get()
    return SettingsOut(settings=result.settings, provenance=result.provenance)


@router.put("/settings", response_model=SettingsOut)
async def update_settings_route(
    body: dict[str, Any],
    settings_store: SettingsStore = Depends(_settings),
    events: EventHub = Depends(_events),
    notifications: AttentionNotifier = Depends(_notifications),
    advisories: AdvisorySampler = Depends(_advisories),
    sessions: SessionTracker = Depends(_sessions),
    gpg: GpgProbe = Depends(_gpg),
) -> SettingsOut:
    if "gpg_signing_key" in body:
        _assert_selectable_signing_key(body["gpg_signing_key"], gpg)
    try:
        result = settings_store.update(body)
    except SettingsValidationError as exc:
        # Every validator already names its key; prefixing unconditionally
        # produced "checkout_root: checkout_root must be…", which the
        # Settings panel now shows to the operator verbatim.
        detail = (
            exc.message
            if exc.message.startswith(f"{exc.key}:") or exc.message.startswith(exc.key)
            else f"{exc.key}: {exc.message}"
        )
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail) from exc
    _apply_settings_live(result.settings, events, notifications, advisories, sessions)
    if "gpg_signing_key" in body:
        await gpg.probe()
    return SettingsOut(settings=result.settings, provenance=result.provenance)


@router.delete("/settings/{key}", response_model=SettingsOut)
async def delete_settings_route(
    key: str,
    settings_store: SettingsStore = Depends(_settings),
    events: EventHub = Depends(_events),
    notifications: AttentionNotifier = Depends(_notifications),
    advisories: AdvisorySampler = Depends(_advisories),
    sessions: SessionTracker = Depends(_sessions),
    gpg: GpgProbe = Depends(_gpg),
) -> SettingsOut:
    deleted = settings_store.delete(key)
    if not deleted:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"setting {key!r} is unknown or has no override to delete",
        )
    result = settings_store.get()
    _apply_settings_live(result.settings, events, notifications, advisories, sessions)
    if key == "gpg_signing_key":
        await gpg.probe()
    return SettingsOut(settings=result.settings, provenance=result.provenance)


class DaemonInfoOut(BaseModel):
    bind: str
    port: int
    version: str
    config_path: str
    data_dir: str
    audit_log_path: str | None


@router.get("/daemon/info", response_model=DaemonInfoOut)
def daemon_info_route(
    request: Request,
    config: Config = Depends(_config),
) -> DaemonInfoOut:
    data_dir = config.data_dir
    audit_path = audit_log_path_for(data_dir)
    audit_log_path = str(audit_path) if audit_path.is_file() else None
    return DaemonInfoOut(
        bind=config.bind,
        port=config.port,
        version=package_version("ompire-daemon"),
        config_path=str(request.app.state.config_path),
        data_dir=str(data_dir),
        audit_log_path=audit_log_path,
    )


@router.get("/settings/token", response_model=dict[str, str])
def get_token_route(request: Request) -> dict[str, str]:
    return {"token": request.app.state.auth_token}


@router.post("/settings/token/rotate", response_model=dict[str, str])
async def rotate_token_route(request: Request) -> dict[str, str]:
    data_dir = request.app.state.config.data_dir
    new_token = secrets.token_urlsafe(32)
    auth.write_token_file(auth.token_path_for(data_dir), new_token)
    request.app.state.auth_token = new_token
    await _close_all_ws(request.app.state.ws_connections)
    logger.info("auth token rotated")
    return {"token": new_token}
