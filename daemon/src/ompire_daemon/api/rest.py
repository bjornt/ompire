"""REST endpoints under /api/. Commands only — events go out over the WebSocket.

Architecture: ADR-0004 (docs/adr/0004-use-rest-and-websocket-snapshot-deltas.md)
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import shutil
from collections.abc import Mapping
from dataclasses import asdict
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import Engine

from ompire_daemon import auth, launchconfig
from ompire_daemon.advisories import AdvisorySampler
from ompire_daemon.agent import AgentHandle, AgentSupervisor, NoLiveAgentError
from ompire_daemon.auth import require_bearer_token
from ompire_daemon.config import Config
from ompire_daemon.datadir import audit_log_path_for
from ompire_daemon.events import EventHub
from ompire_daemon.execution_inputs import (
    WORKSPACE_FIELDS,
)
from ompire_daemon.gh import GitHubProbe
from ompire_daemon.gpg import (
    FINGERPRINT_RE,
    STATE_READY,
    GpgProbe,
    gpg_signing_refusal,
)
from ompire_daemon.launch import (
    ConsumerOverride,
    LaunchInputError,
    LaunchRequest,
    PreviewChangedError,
    ProjectNotLaunchableError,
    resolution_payload,
    resolve_launch,
)
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
    reserved_write,
    update_model_profile,
    validate_profile_name,
)
from ompire_daemon.registry.projects import (
    DEFAULT_BASE_BRANCH,
    DEFAULT_FETCH_REMOTE,
    DEFAULT_WORKSHOP_ADDITIONS,
    UNSUPPLIED,
    DuplicateProjectError,
    InvalidBranchPatternError,
    InvalidWorkshopAdditionsError,
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
    TaskConfigurationRequiredError,
    TaskInputsAlreadyPinnedError,
    TaskNotArchivedError,
    TaskNotFoundError,
    clone_path_for,
    create_task,
    get_task,
    list_tasks,
    mark_archived,
    purge_task,
    require_task_inputs,
    task_payload,
    validate_task_slug,
)
from ompire_daemon.registry.workflow_definitions import (
    WorkflowRevisionUnavailableError,
    get_revision,
)
from ompire_daemon.registry.workflow_library import (
    ORIGIN_BUILTIN,
    ArchivedWorkflowError,
    BuiltinWorkflowReadOnlyError,
    DuplicateWorkflowNameError,
    InvalidWorkflowNameError,
    LibraryDetail,
    LibraryEntry,
    UnknownWorkflowNameError,
    WorkflowDraftTooLargeError,
    WorkflowEntryNotFoundError,
    WorkflowNameMismatchError,
    WorkflowVersionConflictError,
    check_draft_size,
    create_entry,
    get_detail,
    launchable_descriptors,
    list_entries,
    save_draft,
    save_revision,
    set_archived,
)
from ompire_daemon.registry.workflows import (
    GATE_SNAPSHOT_VERSION,
    WorkflowGateChoiceError,
    WorkflowWaitConflictError,
    latest_step_record,
    list_step_records,
)
from ompire_daemon.review import ReviewAlreadyOpenError, ReviewError, ReviewManager
from ompire_daemon.rpc import AgentGoneError, RequestFailedError
from ompire_daemon.sessions import SessionTracker
from ompire_daemon.ship import GitHubPreflightError, ShipError, ShipManager
from ompire_daemon.spawn import run_spawn_pipeline
from ompire_daemon.taskdefinition import (
    TaskDefinitionUnavailableError,
    resolve_task_definition,
)
from ompire_daemon.workflow_definitions import (
    UnsupportedWorkflowFormatError,
    WorkflowDefinition,
    WorkflowDocumentError,
    WorkflowRevision,
    definition_from_document,
    describe,
    emit_draft_yaml,
    export_yaml,
    load_canonical_document,
    load_definition,
    make_revision,
    parse_yaml_document,
)
from ompire_daemon.workflows import (
    WorkflowNotWaitingError,
    WorkflowRunner,
    packaged_yaml,
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
    # default; nothing is inferred from credentials or the project name.
    default_model_profile: str | None = None
    # Workspace and prompt defaults a launch inherits (ADR-0026). The branch
    # pattern defaults to the daemon's `default_branch_pattern` *setting* at
    # registration time — a seed, not a value anything reads later.
    base_branch: str = DEFAULT_BASE_BRANCH
    branch_pattern: str | None = None
    workshop_additions: str = DEFAULT_WORKSHOP_ADDITIONS
    preamble: str = ""

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
    # Same omission rule for the workspace defaults, but they are never null:
    # an empty preamble is the value "no preamble".
    base_branch: str | None = None
    branch_pattern: str | None = None
    workshop_additions: str | None = None
    preamble: str | None = None

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
    base_branch: str
    branch_pattern: str
    workshop_additions: str
    preamble: str
    # `reconciled` or `needs-reconciliation` — whether the carried-over
    # template configuration still needs a decision (ADR-0026). Independent of
    # `setup_state`, and reported alongside it so the UI can say which of the
    # two is blocking a launch.
    launch_config_state: str

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


def _supplied_workspace_defaults(body: ProjectUpdate) -> dict[str, Any]:
    """Only the workspace defaults the caller actually mentioned. Everything
    else stays `UNSUPPLIED`, so a client written before these fields existed
    cannot blank them by omission — and an explicit null is refused rather
    than read as a reset, because none of them has a meaningful empty value
    except `preamble`, whose empty string is a real choice."""
    supplied: dict[str, Any] = {}
    for field in WORKSPACE_FIELDS:
        if field not in body.model_fields_set:
            continue
        value = getattr(body, field)
        if value is None:
            if field == "preamble":
                supplied[field] = ""
                continue
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                f"{field}: null is not a reset; omit the field to leave it unchanged",
            )
        supplied[field] = value
    return supplied


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
            base_branch=body.base_branch,
            branch_pattern=body.branch_pattern or config.default_branch_pattern,
            workshop_additions=body.workshop_additions,
            preamble=body.preamble,
        )
    except DuplicateProjectError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except (
        UnknownModelProfileReferenceError,
        InvalidBranchPatternError,
        InvalidWorkshopAdditionsError,
    ) as exc:
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
            **_supplied_workspace_defaults(body),
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except DuplicateProjectError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ProjectHasReferencingTasksError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except (
        UnknownModelProfileReferenceError,
        InvalidBranchPatternError,
        InvalidWorkshopAdditionsError,
    ) as exc:
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


# --- The workflow library, its revisions, and authoring ------------------------
# An operator owns which procedures exist (ADR-0031). `GET /workflows` stays the
# *launchable* descriptor collection and `GET /workflows/revisions/{revision}`
# stays immutable inspection; everything under `/workflow-library` is the
# mutable part: entries, inert drafts, validation, executable saves, and
# archive/restore.
#
# Three operations that are easy to conflate are deliberately three routes.
# Saving a draft persists text and promises nothing. Validating checks text and
# saves nothing — and its response is informative, never an authorization to
# save later. Saving an executable revision re-validates the exact submitted
# text and only then retains and selects it. Nothing here starts a task.


class StepDescriptorOut(BaseModel):
    name: str
    kind: str
    session: str | None
    # Agent steps declare an abstract role; commands, decisions, and gates
    # have none and never reach a model.
    role: str | None
    # A declared route can pass this step by, or its own `when` can hold it
    # back, so it may not run.
    conditional: bool


class WorkflowOut(BaseModel):
    name: str
    # What a *new* launch of this name would pin, and the semantics version it
    # is read under.
    revision: str
    format: int
    primary_session: str
    sessions: list[str]
    steps: list[StepDescriptorOut]


@router.get("/workflows", response_model=list[WorkflowOut])
def list_workflows_route(engine: Engine = Depends(_engine)) -> list[dict[str, Any]]:
    """Only what a new launch may select: non-archived, present, readable.

    Derived from the same library read the WebSocket snapshot uses, so the two
    cannot offer different catalogs.
    """
    return [
        asdict(descriptor)
        for descriptor in launchable_descriptors(list_entries(engine))
    ]


class WorkflowRevisionOut(BaseModel):
    revision: str
    name: str
    format: int
    primary_session: str
    sessions: list[str]
    # The whole normalized document. Read-only, and the same bytes the
    # revision identity is taken over, so what an operator inspects is
    # literally what executes.
    definition: dict[str, Any]


def _revision_or_error(engine: Engine, revision: str) -> WorkflowRevision:
    try:
        return get_revision(engine, revision)
    except WorkflowRevisionUnavailableError as exc:
        if exc.reason == "missing":
            raise HTTPException(status.HTTP_404_NOT_FOUND, exc.detail) from exc
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "reason": "workflow_definition_unavailable",
                "unavailable_reason": exc.reason,
                "message": exc.detail,
                "revision": revision,
            },
        ) from exc


@router.get("/workflows/revisions/{revision}", response_model=WorkflowRevisionOut)
def get_workflow_revision_route(
    revision: str, engine: Engine = Depends(_engine)
) -> dict[str, Any]:
    """One retained definition, by content identity.

    Deliberately not addressable by workflow name: a name says what a new
    launch would get, and this endpoint exists to answer "what did *that* task
    accept". A stored document that cannot be read comes back as a classified
    409 rather than a silent substitution — it is never executed to answer a
    read.
    """
    retained = _revision_or_error(engine, revision)
    return {
        "revision": retained.revision,
        "name": retained.name,
        "format": retained.format,
        "primary_session": retained.definition.primary,
        "sessions": list(retained.definition.sessions),
        "definition": retained.document,
    }


class WorkflowYamlOut(BaseModel):
    revision: str
    name: str
    format: int
    yaml: str


@router.get("/workflows/revisions/{revision}/yaml", response_model=WorkflowYamlOut)
def export_workflow_revision_route(
    revision: str, engine: Engine = Depends(_engine)
) -> dict[str, Any]:
    """One retained revision as a standalone YAML definition.

    Emitted from the integrity-checked document, not from any draft, and
    verified to load back to this same identity before it is returned. What
    comes out is a complete definition an operator can keep and import again;
    the formatting and comments of whatever they originally typed are not
    preserved, because a revision is a canonical document and the entry's
    draft is where their text lives.
    """
    retained = _revision_or_error(engine, revision)
    return {
        "revision": retained.revision,
        "name": retained.name,
        "format": retained.format,
        "yaml": export_yaml(retained),
    }


# The starter a new workflow opens on: the smallest thing that is a real
# format-2 definition. One agent step that gets the operator's prompt, and an
# ending that says what finishing meant — because format 2 does not let a run
# stop by falling off the end of the list.
STARTER_TEMPLATE = """\
# A new workflow. Edit it, Validate it, then save an executable revision.
# Saving a draft keeps your text; only an executable save makes this
# launchable.
format: 2
name: {name}
sessions: [main]
primary: main
steps:
  - name: work
    kind: agent
    session: main
    # `null` asks this step for no result document. Declare `results` here
    # when a later decision has to route on what the step found.
    outcome: null
    prompt:
      parts:
        - value: {{op: input, name: task.prompt}}

  - name: finish
    kind: decision
    cases:
      - when: true
        next: {{complete: true, result: done}}
    otherwise: {{complete: true, result: done}}
"""


class WorkflowLibraryCreate(BaseModel):
    """A new custom entry.

    Exactly one source, or none: `yaml` is pasted or imported text,
    `source_revision` duplicates a retained revision under the new name, and
    omitting both opens the starter. Supplying both is refused rather than
    resolved by precedence — an ambiguous create is an operator who meant one
    of two different things.
    """

    model_config = ConfigDict(extra="forbid")

    # Deliberately not validated by a field validator: FastAPI answers one of
    # those with its own list-shaped `detail`, which carries no `message` for a
    # client to show. The name is checked by `create_entry`, so a bad one comes
    # back through the same refusal shape as every other authoring error.
    name: str
    yaml: str | None = None
    source_revision: str | None = None


class WorkflowDraftSave(BaseModel):
    """Inert text plus the edit version it is replacing."""

    model_config = ConfigDict(extra="forbid")

    yaml: str
    expected_version: int


class WorkflowValidateIn(BaseModel):
    """Text to check. `name` binds the check to an existing entry's immutable
    identity, so an editor is told about a rename before it saves rather than
    after."""

    model_config = ConfigDict(extra="forbid")

    yaml: str
    name: str | None = None


class WorkflowEntryVersionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int


class WorkflowLibraryEntryOut(BaseModel):
    name: str
    origin: str
    archived: bool
    version: int
    has_draft: bool
    current_revision: str | None
    current_format: int | None
    available: bool
    unavailable_reason: str | None
    unavailable_detail: str | None
    created_at: str
    updated_at: str
    descriptor: WorkflowOut | None


class RevisionSummaryOut(BaseModel):
    revision: str
    workflow_name: str
    format: int
    created_at: str


class WorkflowLibraryDetailOut(BaseModel):
    entry: WorkflowLibraryEntryOut
    # The raw text of the editor's last saved draft, exactly as submitted. For
    # a built-in this is its packaged text, which is read-only.
    draft_yaml: str | None
    revisions: list[RevisionSummaryOut]


class WorkflowValidationOut(BaseModel):
    revision: str
    name: str
    format: int
    definition: dict[str, Any]
    descriptor: WorkflowOut


def _entry_payload(entry: LibraryEntry) -> dict[str, Any]:
    """Event and response shape for one entry — the same one either way, so a
    client's reducer has a single path for both."""
    return asdict(entry)


def _detail_payload(detail: LibraryDetail) -> dict[str, Any]:
    return {
        "entry": _entry_payload(detail.entry),
        "draft_yaml": detail.draft_yaml,
        "revisions": [asdict(summary) for summary in detail.revisions],
    }


def _publish_entry(events: EventHub, detail: LibraryDetail) -> None:
    """One full-entry upsert per committed mutation.

    Full, not a patch: the reducer that applies it also has to update the
    launch catalog, and it can only decide whether this entry is still
    eligible from the whole entry. Ordering is carried by the entry's edit
    version rather than by delivery order.
    """
    events.publish("workflow_library_updated", _entry_payload(detail.entry))


def _document_error_detail(exc: WorkflowDocumentError) -> dict[str, Any]:
    """A structured refusal an editor can point at.

    Carries where in the document the problem is, why, and — when the parser
    supplied them — the source line and column. The submitted text is never
    echoed back: an error is not a place to mirror a megabyte.

    One shape, two carriers. A document that cannot be *parsed* is an HTTP
    refusal; a document that parses but is not a valid workflow is a
    diagnostic reported beside the draft it describes, because that draft is
    still work an operator is allowed to keep.
    """
    detail: dict[str, Any] = {
        "reason": "workflow_document_invalid",
        "location": getattr(exc, "location", "") or None,
        "message": getattr(exc, "reason", None) or str(exc),
    }
    if isinstance(exc, UnsupportedWorkflowFormatError):
        detail["reason"] = "workflow_format_unsupported"
        detail["format"] = exc.version
    mark = getattr(exc, "__cause__", None)
    position = getattr(mark, "problem_mark", None) or getattr(mark, "context_mark", None)
    if position is not None:
        detail["line"] = position.line + 1
        detail["column"] = position.column + 1
    return detail


def _document_error(exc: WorkflowDocumentError) -> HTTPException:
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_CONTENT, _document_error_detail(exc)
    )


def _library_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (WorkflowEntryNotFoundError, UnknownWorkflowNameError)):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, WorkflowVersionConflictError):
        # The conflict says what the entry's version actually is, so an editor
        # can offer to reload rather than guess. Nothing was written.
        return HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "reason": "workflow_version_conflict",
                "message": str(exc),
                "name": exc.name,
                "expected_version": exc.expected,
                "current_version": exc.actual,
            },
        )
    if isinstance(exc, DuplicateWorkflowNameError):
        return HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "reason": "workflow_name_taken",
                "message": str(exc),
                "name": exc.name,
                "origin": exc.origin,
                "archived": exc.archived,
            },
        )
    if isinstance(exc, BuiltinWorkflowReadOnlyError):
        return HTTPException(
            status.HTTP_409_CONFLICT,
            {"reason": "workflow_builtin_read_only", "message": str(exc)},
        )
    if isinstance(exc, ArchivedWorkflowError):
        return HTTPException(
            status.HTTP_409_CONFLICT,
            {"reason": "workflow_archived", "message": str(exc)},
        )
    if isinstance(exc, WorkflowDocumentError):
        return _document_error(exc)
    # Invalid names, oversized text, and a document renaming its entry: all
    # things the operator can fix in the editor.
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))


def _validated(text: str) -> WorkflowRevision:
    """Parse and validate submitted text, outside any write reservation.

    Reading, not executing: the loader evaluates nothing, runs no command,
    fetches no URL, and opens no path named in the document.
    """
    try:
        check_draft_size(text)
    except WorkflowDraftTooLargeError as exc:
        raise _library_error(exc) from exc
    try:
        return load_definition(text)
    except WorkflowDocumentError as exc:
        raise _document_error(exc) from exc


@router.get("/workflow-library", response_model=list[WorkflowLibraryEntryOut])
def list_workflow_library_route(
    engine: Engine = Depends(_engine),
) -> list[dict[str, Any]]:
    """Every entry, in name order — draft-only, archived, and damaged included.

    The library is what exists. What can launch is `GET /workflows`.
    """
    return [_entry_payload(entry) for entry in list_entries(engine)]


@router.post(
    "/workflow-library",
    response_model=WorkflowLibraryDetailOut,
    status_code=status.HTTP_201_CREATED,
)
def create_workflow_library_route(
    body: WorkflowLibraryCreate,
    engine: Engine = Depends(_engine),
    events: EventHub = Depends(_events),
) -> dict[str, Any]:
    """Create, duplicate, or start from the packaged starter.

    None of the three validates or selects a revision: what is created is a
    draft, and a draft cannot launch. Duplication reads the chosen retained
    document and changes only its top-level name before serializing it, so the
    copy is the same procedure under a new identity — a genuinely different
    document, and therefore a different content revision.
    """
    if body.yaml is not None and body.source_revision is not None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "supply either 'yaml' or 'source_revision', not both",
        )
    if body.yaml is not None:
        text = body.yaml
    elif body.source_revision is not None:
        source = _revision_or_error(engine, body.source_revision)
        renamed = dict(source.document)
        renamed["name"] = body.name
        try:
            text = export_yaml(make_revision(load_canonical_document(renamed)))
        except WorkflowDocumentError as exc:
            raise _document_error(exc) from exc
    else:
        text = STARTER_TEMPLATE.format(name=body.name)
    try:
        detail = create_entry(engine, name=body.name, yaml_text=text)
    except (
        InvalidWorkflowNameError,
        DuplicateWorkflowNameError,
        WorkflowDraftTooLargeError,
    ) as exc:
        raise _library_error(exc) from exc
    _publish_entry(events, detail)
    return _detail_payload(detail)


@router.post("/workflow-library/validate", response_model=WorkflowValidationOut)
def validate_workflow_route(
    body: WorkflowValidateIn, engine: Engine = Depends(_engine)
) -> dict[str, Any]:
    """Check this exact text. Nothing is persisted and nothing is authorized.

    Structural only: it says the document is a workflow this engine can read,
    never that the commands it names are installed, that a model will produce
    what a step asks for, or that the run will succeed.
    """
    revision = _validated(body.yaml)
    if body.name is not None and body.name != revision.name:
        raise _library_error(WorkflowNameMismatchError(body.name, revision.name))
    return {
        "revision": revision.revision,
        "name": revision.name,
        "format": revision.format,
        "definition": revision.document,
        "descriptor": asdict(describe(revision)),
    }


# --- authoring conversion ------------------------------------------------------
# The visual editor edits a *document*; the library stores *text*. This is the
# only translation between the two, and it is deliberately the narrowest thing
# that can be: authenticated, stateless, and inert.
#
# It persists nothing, retains no revision, publishes no event, runs no
# command, and authorizes no later save. Saving a draft and saving an
# executable revision still take exact YAML and the loaded `expected_version`,
# and an executable save still runs its own validation on what it is given —
# a conversion answer is never a token.
#
# Its response separates two questions an editor keeps conflating. *Can this
# be read at all* is the parse, and a failure there is an HTTP refusal.
# *Is it a workflow* is the validation, and a failure there comes back beside
# the unchanged draft, because an unfinished card with a missing destination
# is work an operator is allowed to keep, reopen, and finish.


class WorkflowDocumentIn(BaseModel):
    """One draft, in whichever direction it is being converted.

    Exactly one of `yaml` and `document`: supplying both is an editor that
    does not know which representation it is holding, which is the bug this
    interface exists to make impossible. `name` binds the check to an existing
    entry's identity, exactly as `validate` does.
    """

    model_config = ConfigDict(extra="forbid")

    yaml: str | None = None
    document: dict[str, Any] | None = None
    name: str | None = None


class WorkflowDocumentValidOut(BaseModel):
    """The same projection `validate` returns, for a draft that is a
    workflow. `definition` is the canonical document, not the draft."""

    ok: Literal[True] = True
    revision: str
    name: str
    format: int
    definition: dict[str, Any]
    descriptor: WorkflowOut


class WorkflowDocumentInvalidOut(BaseModel):
    """Why this draft is not executable, and where to look."""

    ok: Literal[False] = False
    reason: str
    location: str | None = None
    message: str
    line: int | None = None
    column: int | None = None
    format: Any | None = None


class WorkflowDocumentOut(BaseModel):
    # The parsed draft, exactly as submitted — *not* a canonicalized
    # definition. Unknown fields and incomplete values survive, so an editor
    # reading this back cannot silently drop what it does not understand.
    document: dict[str, Any]
    # The text form of that same draft. For YAML input it is the submitted
    # text unchanged, so opening the visual editor and closing it again
    # rewrites nothing.
    yaml: str
    validation: WorkflowDocumentValidOut | WorkflowDocumentInvalidOut


def _draft_validation(document: Mapping[str, Any], name: str | None) -> dict[str, Any]:
    """Semantic validation of a parsed draft, reported rather than raised."""
    try:
        revision = make_revision(definition_from_document(document))
    except WorkflowDocumentError as exc:
        return {"ok": False, **_document_error_detail(exc)}
    if name is not None and name != revision.name:
        mismatch = WorkflowNameMismatchError(name, revision.name)
        return {
            "ok": False,
            "reason": "workflow_name_mismatch",
            "location": "name",
            "message": str(mismatch),
        }
    return {
        "ok": True,
        "revision": revision.revision,
        "name": revision.name,
        "format": revision.format,
        "definition": revision.document,
        "descriptor": asdict(describe(revision)),
    }


@router.post("/workflow-library/document", response_model=WorkflowDocumentOut)
def convert_workflow_document_route(body: WorkflowDocumentIn) -> dict[str, Any]:
    """Translate one draft between text and data, and say what it means.

    YAML in goes through the production bounded parser — the same depth, node,
    size, anchor, and scalar rules a save applies — and the submitted text
    comes back untouched. Data in is bounded first, emitted through the
    revision emitter, and then *parsed back*: what is returned as `document`
    is what the loader would see, so the two representations cannot drift.

    Neither direction converts a format. An unsupported version is reported as
    an unsupported version; syntactically safe conversion is not semantic
    support, and it is certainly not permission to launch.
    """
    if (body.yaml is None) == (body.document is None):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "supply exactly one of 'yaml' or 'document'",
        )
    if body.yaml is not None:
        text = body.yaml
        try:
            check_draft_size(text)
        except WorkflowDraftTooLargeError as exc:
            raise _library_error(exc) from exc
        try:
            document = parse_yaml_document(text)
        except WorkflowDocumentError as exc:
            raise _document_error(exc) from exc
    else:
        try:
            text = emit_draft_yaml(body.document or {})
            document = parse_yaml_document(text)
        except WorkflowDocumentError as exc:
            raise _document_error(exc) from exc
    return {
        "document": document,
        "yaml": text,
        "validation": _draft_validation(document, body.name),
    }


@router.get("/workflow-library/{name}", response_model=WorkflowLibraryDetailOut)
def get_workflow_library_route(
    name: str, engine: Engine = Depends(_engine)
) -> dict[str, Any]:
    """One entry: its summary, its raw text, and its retained history.

    A built-in has no stored draft, so its text comes from the package — the
    example an operator reads before duplicating it.
    """
    try:
        detail = get_detail(engine, name)
    except WorkflowEntryNotFoundError as exc:
        raise _library_error(exc) from exc
    payload = _detail_payload(detail)
    if detail.entry.origin == ORIGIN_BUILTIN:
        try:
            payload["draft_yaml"] = packaged_yaml(name)
        except OSError:
            # A built-in this package no longer ships. Its history stays
            # readable; only its example text is gone.
            payload["draft_yaml"] = None
    return payload


@router.put(
    "/workflow-library/{name}/draft", response_model=WorkflowLibraryDetailOut
)
def save_workflow_draft_route(
    name: str,
    body: WorkflowDraftSave,
    engine: Engine = Depends(_engine),
    events: EventHub = Depends(_events),
) -> dict[str, Any]:
    """Persist the editor's text as it stands, valid or not.

    A draft is work in progress. Saving one never touches the entry's current
    revision, so a half-finished edit cannot take a launchable workflow away.
    """
    try:
        detail = save_draft(
            engine,
            name,
            yaml_text=body.yaml,
            expected_version=body.expected_version,
        )
    except (
        WorkflowEntryNotFoundError,
        WorkflowVersionConflictError,
        BuiltinWorkflowReadOnlyError,
        ArchivedWorkflowError,
        WorkflowDraftTooLargeError,
    ) as exc:
        raise _library_error(exc) from exc
    _publish_entry(events, detail)
    return _detail_payload(detail)


@router.post(
    "/workflow-library/{name}/revisions", response_model=WorkflowLibraryDetailOut
)
def save_workflow_revision_route(
    name: str,
    body: WorkflowDraftSave,
    engine: Engine = Depends(_engine),
    events: EventHub = Depends(_events),
) -> dict[str, Any]:
    """Validate this text, retain it, and make it the entry's current choice.

    The submitted text is validated here and now — an earlier `validate` call
    is informative, not a token — and only then does the reservation open to
    compare the version, retain the document, and move the selection, all
    together. Re-saving semantically identical YAML reuses the existing
    content revision. It never starts a task.
    """
    revision = _validated(body.yaml)
    try:
        detail = save_revision(
            engine,
            name,
            revision=revision,
            yaml_text=body.yaml,
            expected_version=body.expected_version,
        )
    except (
        WorkflowEntryNotFoundError,
        WorkflowVersionConflictError,
        BuiltinWorkflowReadOnlyError,
        ArchivedWorkflowError,
        WorkflowNameMismatchError,
        WorkflowDraftTooLargeError,
    ) as exc:
        raise _library_error(exc) from exc
    _publish_entry(events, detail)
    return _detail_payload(detail)


@router.post(
    "/workflow-library/{name}/archive", response_model=WorkflowLibraryDetailOut
)
def archive_workflow_route(
    name: str,
    body: WorkflowEntryVersionIn,
    engine: Engine = Depends(_engine),
    events: EventHub = Depends(_events),
) -> dict[str, Any]:
    """Take an entry out of future launch choices. Nothing is deleted."""
    return _set_archived(engine, events, name, True, body.expected_version)


@router.post(
    "/workflow-library/{name}/restore", response_model=WorkflowLibraryDetailOut
)
def restore_workflow_route(
    name: str,
    body: WorkflowEntryVersionIn,
    engine: Engine = Depends(_engine),
    events: EventHub = Depends(_events),
) -> dict[str, Any]:
    """Make the retained current revision eligible again, if it is readable.

    A draft-only entry comes back draft-only: archiving never granted it a
    revision, so restoring cannot either.
    """
    return _set_archived(engine, events, name, False, body.expected_version)


def _set_archived(
    engine: Engine, events: EventHub, name: str, archived: bool, expected_version: int
) -> dict[str, Any]:
    try:
        detail = set_archived(
            engine, name, archived=archived, expected_version=expected_version
        )
    except (
        WorkflowEntryNotFoundError,
        WorkflowVersionConflictError,
        BuiltinWorkflowReadOnlyError,
    ) as exc:
        raise _library_error(exc) from exc
    _publish_entry(events, detail)
    return _detail_payload(detail)


# --- Task launch --------------------------------------------------------------


class WorkspaceOverridesIn(BaseModel):
    """Task-local overrides of the project's workspace defaults.

    Each field is genuinely optional: leaving a key out inherits the project
    value. An explicit empty `preamble` is an override to "no preamble", which
    is why the route distinguishes omitted from present via `model_fields_set`
    rather than treating empty as absent. The other three have no meaningful
    null — a task cannot run with no base branch — so an explicit null is
    refused instead of being read as a reset.
    """

    model_config = ConfigDict(extra="forbid")

    base_branch: str | None = None
    branch_pattern: str | None = None
    workshop_additions: str | None = None
    preamble: str | None = None


class ConsumerOverrideIn(BaseModel):
    """One model consumer's row-level selections (ADR-0027).

    Each dimension is independently optional: omitted or null means inherit,
    a value means the operator chose it. `extra="forbid"` keeps a typo like
    `model` or `thinking` from being silently ignored — those are profile
    settings, deliberately not a third override hierarchy.
    """

    model_config = ConfigDict(extra="forbid")

    model_profile: str | None = None
    role: str | None = None


class TaskCreate(BaseModel):
    """The one supported creation contract (ADR-0026, ADR-0027).

    `extra="forbid"` is load-bearing: `template_name`, and the old scalar
    `model`/`thinking` spawn overrides, must be refused rather than ignored,
    or a stale caller would silently get a launch it did not ask for.
    """

    model_config = ConfigDict(extra="forbid")

    project_name: str
    workflow_name: str
    slug: str
    prompt: str
    # Omitted or null means "inherit the project default". A name selects that
    # profile for this task and replaces the inheritance.
    model_profile: str | None = None
    workspace_overrides: WorkspaceOverridesIn | None = None
    # Per-consumer overrides, keyed by declared agent step name. Absent means
    # "everything inherits". `auxiliary_overrides` is kept only so a caller
    # still naming the retired judge is refused with a field-level error
    # rather than having its choice silently dropped (ADR-0028).
    step_overrides: dict[str, ConsumerOverrideIn] = Field(default_factory=dict)
    auxiliary_overrides: dict[str, ConsumerOverrideIn] = Field(default_factory=dict)

    @field_validator("slug")
    @classmethod
    def _validate_slug(cls, value: str) -> str:
        validate_task_slug(value)
        return value


class TaskAccept(TaskCreate):
    """Creation adds the reviewed resolution's token. It identifies *what was
    reviewed*, not who is asking: it authorizes nothing, and a mismatch is a
    409 asking for a fresh review rather than a permission error."""

    preview_token: str


def _launch_request(body: TaskCreate) -> LaunchRequest:
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
    supplied: Mapping[str, ConsumerOverrideIn], field: str
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


def _preview_changed(resolved) -> HTTPException:
    """409 with a machine-readable reason and the current resolution, so the
    form can show the operator exactly what changed instead of retrying under
    settings they never reviewed."""
    exc = PreviewChangedError(resolved)
    return HTTPException(
        status.HTTP_409_CONFLICT,
        {
            "reason": exc.reason,
            "message": str(exc),
            "preview": resolution_payload(resolved) if resolved is not None else None,
        },
    )


def _launch_error(exc: Exception) -> HTTPException:
    if isinstance(exc, LaunchInputError):
        return HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"{exc.field}: {exc.detail}"
        )
    if isinstance(exc, ProjectNotLaunchableError):
        return HTTPException(status.HTTP_409_CONFLICT, exc.detail)
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))


@router.post("/tasks/preview")
def preview_task_route(
    body: TaskCreate, engine: Engine = Depends(_engine)
) -> dict[str, Any]:
    """Resolve the operator's selections without creating anything.

    Same rules, same module, and same output as acceptance — that identity is
    what makes reviewing a preview worth anything.
    """
    request = _launch_request(body)
    with engine.connect() as conn:
        try:
            resolved = resolve_launch(conn, request)
        except (LaunchInputError, ProjectNotLaunchableError) as exc:
            raise _launch_error(exc) from exc
    return resolution_payload(resolved)


class TaskOut(BaseModel):
    id: int
    project_name: str
    execution_inputs: dict[str, Any] | None
    needs_configuration: bool
    slug: str
    branch: str
    clone_path: str
    state: str
    prompt: str
    error: str | None
    workshop_id: str | None
    workflow_name: str
    # The pinned definition (ADR-0028). `workflow_revision` is null only for a
    # task that predates retained revisions; `workflow_primary_session` and
    # `workflow_sessions` are null whenever the definition cannot be resolved,
    # rather than a plausible-looking guess.
    workflow_revision: str | None
    workflow_revision_source: str | None
    workflow_ready: bool
    workflow_readiness_reason: str | None
    workflow_readiness_detail: str | None
    workflow_primary_session: str | None
    workflow_sessions: list[str] | None
    workflow_status: str | None
    workflow_step: str | None
    # The declared ending a finished format-2 run reached (ADR-0029). Null
    # while it runs, and for every format-1 run: those have no name for their
    # ending, and none is invented for them.
    workflow_result: str | None
    pr_url: str | None
    pr_state: str | None
    pr_merged_at: str | None
    spawn_completed_at: str | None
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True}


@router.get("/tasks", response_model=list[TaskOut])
def list_tasks_route(engine: Engine = Depends(_engine)) -> list[dict[str, Any]]:
    return [task_payload(task, engine=engine) for task in list_tasks(engine)]


class TaskDetailOut(TaskOut):
    # Derived on demand from the workshop CLI, never persisted (design D-3).
    workshop_status: str | None
    # The run's executed attempts, in order — the same records the snapshot
    # replays, from the same projection. A gate's question and its answer, an
    # attempt's frozen evidence, and an uncertainty pause all live here, so a
    # reader that is not holding a socket open still sees the whole history
    # rather than only the task row's summary.
    workflow_steps: list[dict[str, Any]]


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
    return TaskDetailOut(
        **task_payload(task, engine=engine),
        workshop_status=derived_status,
        workflow_steps=[asdict(record) for record in list_step_records(engine, task_id)],
    )


@router.post("/tasks", response_model=TaskOut, status_code=status.HTTP_202_ACCEPTED)
async def spawn_task_route(
    body: TaskAccept,
    request: Request,
    engine: Engine = Depends(_engine),
    config: Config = Depends(_config),
    events: EventHub = Depends(_events),
) -> dict[str, Any]:
    """Accept one reviewed launch.

    The order is deliberate. Everything slow — git, mention validation, path
    checks — runs first, against a resolution made outside any lock. Then the
    same rules run again on a reserved connection, the reviewed token is
    compared against what they now produce, and the task plus its pinned
    inputs are inserted before the reservation is released. Nothing is
    published or scheduled until that transaction has committed, so a refused
    or stale submission leaves no task, workspace, agent, or background job.
    """
    launch_request = _launch_request(body)

    # First resolution: validation only. Its results are what the Git work
    # below is done against; the authoritative one is taken again under the
    # write reservation.
    with engine.connect() as conn:
        try:
            resolved = resolve_launch(conn, launch_request)
        except (LaunchInputError, ProjectNotLaunchableError) as exc:
            raise _launch_error(exc) from exc
    if resolved.fingerprint != body.preview_token:
        raise _preview_changed(resolved)

    # Mentions are validated before anything is created: Omp drops one it
    # cannot resolve without a word (findings-omp-file-mentions.md), so a
    # mention that will not survive into the clone must be refused here, not
    # discovered after the workspace is built. Against the *accepted* base
    # branch and checkout, not today's project defaults.
    try:
        rejections = await validate_mentions(
            body.prompt,
            checkout_path=resolved.inputs.checkout_path,
            base_branch=resolved.inputs.workspace.base_branch,
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
        clone_path = clone_path_for(
            config.task_dir_root, resolved.inputs.project_name, body.slug
        )
    except ClonePathOutsideRootError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc

    # The consistency boundary. `BEGIN IMMEDIATE` up front means the re-read
    # cannot go stale before the insert commits, and nothing awaited, spawned,
    # or published happens inside it.
    with reserved_write(engine) as conn:
        try:
            final = resolve_launch(conn, launch_request)
        except (LaunchInputError, ProjectNotLaunchableError) as exc:
            raise _launch_error(exc) from exc
        if final.fingerprint != body.preview_token:
            raise _preview_changed(final)
        try:
            task = create_task(
                engine,
                project_name=final.inputs.project_name,
                slug=body.slug,
                branch=final.inputs.branch,
                clone_path=str(clone_path),
                prompt=body.prompt,
                execution_inputs=final.inputs,
                workflow_name=final.inputs.workflow_name,
                conn=conn,
            )
        except DuplicateTaskError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc

    payload = task_payload(task, engine=engine)
    events.publish("task_created", payload)
    job = asyncio.create_task(
        run_spawn_pipeline(
            engine,
            events,
            config,
            task.id,
            request.app.state.workflow_runner,
        )
    )
    # Keep a reference so the job isn't garbage-collected mid-pipeline.
    jobs: set[asyncio.Task] = request.app.state.spawn_jobs
    jobs.add(job)
    job.add_done_callback(jobs.discard)
    return payload


# --- Upgrade reconciliation ---------------------------------------------------
# Two separate blockers with two separate flows: a project's carried-over
# launch configuration, and a legacy task's missing continuation configuration.
# Neither is ever resolved by guessing (ADR-0026).


class ProjectReconciliationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Names exactly the evidence the operator read. A decision made against a
    # stale reading is refused rather than applied.
    evidence_fingerprint: str
    base_branch: str
    branch_pattern: str
    workshop_additions: str
    preamble: str = ""
    # Explicit either way: a name, or `null` meaning "this project has no
    # default; a profile is selected at each launch".
    default_model_profile: str | None = None
    acknowledge_model_candidates: bool = False
    acknowledge_judge_model: bool = False


@router.get("/projects/{name}/launch-reconciliation")
def get_project_reconciliation_route(
    name: str, engine: Engine = Depends(_engine)
) -> dict[str, Any]:
    try:
        return launchconfig.project_reconciliation(engine, name)
    except ProjectNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post("/projects/{name}/launch-reconciliation", response_model=ProjectOut)
def confirm_project_reconciliation_route(
    name: str,
    body: ProjectReconciliationIn,
    engine: Engine = Depends(_engine),
    events: EventHub = Depends(_events),
) -> Project:
    try:
        project = launchconfig.confirm_project_reconciliation(
            engine,
            name,
            launchconfig.ProjectDecision(
                evidence_fingerprint=body.evidence_fingerprint,
                base_branch=body.base_branch,
                branch_pattern=body.branch_pattern,
                workshop_additions=body.workshop_additions,
                preamble=body.preamble,
                default_model_profile=body.default_model_profile,
                acknowledge_model_candidates=body.acknowledge_model_candidates,
                acknowledge_judge_model=body.acknowledge_judge_model,
            ),
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except launchconfig.ReconciliationConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except (LaunchInputError, InvalidBranchPatternError, InvalidWorkshopAdditionsError) as exc:
        raise _launch_error(exc) from exc
    events.publish("project_updated", asdict(project))
    return project


class TaskContinuationIn(BaseModel):
    """What the operator supplies to continue a task the upgrade left blocked.

    Every field is optional at the schema level because two different gaps
    share this route (ADR-0026, ADR-0028): a task that was never configured
    needs all of them, while a task that merely predates retained workflow
    definitions needs none — its model, branch, and preamble were reviewed
    once and are not re-decided. Which are actually required is decided
    against the task, and a missing one comes back as a field-level error.
    """

    model_config = ConfigDict(extra="forbid")

    model_profile: str | None = None
    base_branch: str | None = None
    workshop_additions: str = DEFAULT_WORKSHOP_ADDITIONS
    preamble: str = ""


class TaskContinuationConfirmIn(TaskContinuationIn):
    preview_token: str
    # The operator says, in as many words, that the original model, thinking
    # level, preamble, and overrides are unrecoverable. Confirmation pins what
    # happens next; it never claims the past turns used these values.
    acknowledge_unknown: bool = False
    # And, separately, that the exact procedure this task already ran was
    # never recorded. Two acknowledgements because they are two different
    # things the daemon cannot recover, and a task can need only one of them.
    acknowledge_workflow: bool = False


def _continuation(body: TaskContinuationIn) -> launchconfig.TaskContinuation:
    return launchconfig.TaskContinuation(
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
        return launchconfig.task_configuration(engine, task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post("/tasks/{task_id}/configuration/preview")
def preview_task_configuration_route(
    task_id: int,
    body: TaskContinuationIn,
    engine: Engine = Depends(_engine),
) -> dict[str, Any]:
    try:
        return launchconfig.preview_task_configuration(
            engine, task_id, _continuation(body)
        )
    except TaskNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ProjectNotFoundError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except launchconfig.ReconciliationConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except (LaunchInputError, InvalidWorkshopAdditionsError) as exc:
        raise _launch_error(exc) from exc


@router.post("/tasks/{task_id}/configuration/confirm", response_model=TaskOut)
def confirm_task_configuration_route(
    task_id: int,
    body: TaskContinuationConfirmIn,
    engine: Engine = Depends(_engine),
    events: EventHub = Depends(_events),
) -> dict[str, Any]:
    try:
        task = launchconfig.confirm_task_configuration(
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
    except (launchconfig.ReconciliationConflictError, TaskInputsAlreadyPinnedError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except (LaunchInputError, InvalidWorkshopAdditionsError) as exc:
        raise _launch_error(exc) from exc
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
        task = get_task(engine, task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    try:
        require_task_inputs(task)
    except TaskConfigurationRequiredError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if task.workflow_status not in ("running", "waiting"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"task {task_id} workflow is {task.workflow_status or 'not running'}; "
            "only an interrupted run can be continued",
        )
    await request.app.state.continue_task(task)
    return task_payload(task, engine=engine)


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
) -> dict[str, Any]:
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
    payload = task_payload(archived, engine=engine)
    events.publish("task_updated", payload)
    return payload


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


def _task_definition(engine: Engine, task: Task) -> WorkflowDefinition:
    """The definition *this task pinned* (ADR-0028), or a 409 saying why not.

    Never the catalog's current definition of the same name: admitting a
    session, or picking a primary, from today's meaning of a workflow name is
    exactly the substitution pinning exists to prevent.
    """
    try:
        return resolve_task_definition(engine, task).definition
    except TaskDefinitionUnavailableError as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "reason": "workflow_definition_unavailable",
                "unavailable_reason": exc.reason,
                "message": exc.detail,
                "task_id": task.id,
            },
        ) from exc


def _require_declared_session(engine: Engine, task: Task, session: str) -> None:
    """404 on a session the task's pinned definition does not declare
    (design D-1).

    The retired engine-reserved `judge` session is no longer admitted: nothing
    spawns or prompts it any more, so treating it as a live workflow session
    would offer an interaction that cannot happen. Its transcript stays
    readable through the task's step history.
    """
    definition = _task_definition(engine, task)
    if session not in definition.sessions:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"task {task.id} workflow {definition.name!r} declares no session {session!r}",
        )


def _primary_session(engine: Engine, task: Task) -> str:
    """The pinned definition's primary session (design D-8): the target of
    task-scoped operations that mean "the agent" (review, ship)."""
    return _task_definition(engine, task).primary


@router.post("/tasks/{task_id}/sessions/{session}/agent/stop")
async def stop_agent_route(
    task_id: int,
    session: str,
    engine: Engine = Depends(_engine),
    supervisor: AgentSupervisor = Depends(_supervisor),
    sessions: SessionTracker = Depends(_sessions),
) -> dict[str, object]:
    task = _require_task(engine, task_id)
    _require_declared_session(engine, task, session)
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


async def _require_live_agent(
    supervisor: AgentSupervisor, task_id: int, session: str
) -> AgentHandle:
    """The session's live agent, taken inside its own boundary (ADR-0027).

    A composer action prompts the session, so it must not be handed a child
    that a concurrent policy handoff has already started retiring. It runs on
    whatever policy that session last applied — never a reset to the task
    default.
    """
    handle = await supervisor.acquire(task_id, session)
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
    _require_declared_session(engine, task, session)
    handle = await _require_live_agent(supervisor, task_id, session)
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
    _require_declared_session(engine, task, session)
    handle = await _require_live_agent(supervisor, task_id, session)
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
    _require_declared_session(engine, task, session)
    handle = await _require_live_agent(supervisor, task_id, session)
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
    _require_declared_session(engine, task, session)
    handle = await _require_live_agent(supervisor, task_id, session)
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
    _require_declared_session(engine, task, session)
    handle = await _require_live_agent(supervisor, task_id, session)
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
    _require_declared_session(engine, task, session)
    handle = await _require_live_agent(supervisor, task_id, session)
    response = await _agent_request(handle, "get_session_stats")
    data = response.get("data")
    return data if isinstance(data, dict) else {}


# --- Workflow gates and uncertainty pauses ------------------------------------
# One route, two distinct things a run can be waiting on (ADR-0028). A declared
# gate is the definition asking a person to look; resuming finishes it and
# continues. An uncertainty pause is the engine refusing to guess; retrying
# opens another attempt at the step that could not be decided. `expected_seq`
# names the attempt the operator was actually looking at, so a stale tab or a
# double submit is refused rather than applied to a different attempt.


class WorkflowResumeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # The waiting attempt's sequence number, as shown. Required: without it a
    # resume is a request to advance "whatever is waiting now", which is not
    # what the operator decided.
    expected_seq: int
    # A format-2 gate's declared choice. Required there and rejected
    # everywhere else: a format-1 gate has no choices to name, and an
    # uncertainty pause is not a question with options, so accepting one would
    # be answering something nobody asked.
    choice_id: str | None = None
    # Feedback for the choice, and the note a format-1 resume already carried.
    # Data either way: it is recorded and shown, and it never names a route.
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
    waiting = latest_step_record(engine, task_id)
    paused = waiting is not None and waiting.status == "waiting" and waiting.pause is not None
    offers_choices = (
        waiting is not None
        and waiting.status == "waiting"
        and waiting.pause is None
        and (waiting.outcome or {}).get("version") == GATE_SNAPSHOT_VERSION
    )
    if body.choice_id is not None and not offers_choices:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "this task is not waiting at a gate with declared choices; "
            "a retry or a format-1 resume takes no 'choice_id'",
        )
    if offers_choices and body.choice_id is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "this gate asks a question with named choices; 'choice_id' names "
            "the one being answered",
        )
    try:
        if paused:
            # A retry re-enters the blocked step. It never continues past it,
            # and it never edits the recorded evidence: if the same evidence
            # is still unreadable the run pauses again, which is the honest
            # answer rather than a second chance at guessing.
            revision = resolve_task_definition(engine, task)
            updated = runner.retry_step(task, revision, expected_seq=body.expected_seq)
            return {
                "task_id": task.id,
                "workflow": "retried",
                "step": updated.workflow_step,
            }
        if offers_choices:
            assert body.choice_id is not None
            revision = resolve_task_definition(engine, task)
            updated = runner.answer_gate(
                task,
                revision,
                expected_seq=body.expected_seq,
                choice_id=body.choice_id,
                note=body.note,
            )
            return {
                "task_id": task.id,
                "workflow": "answered",
                "choice_id": body.choice_id,
                "step": updated.workflow_step,
                "result": updated.workflow_result,
            }
        runner.resume_gate(task.id, expected_seq=body.expected_seq, note=body.note)
    except WorkflowGateChoiceError as exc:
        # The operator is looking at the right question and gave an answer it
        # does not accept, so name the field rather than telling them to
        # reload.
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"field": exc.field, "detail": exc.detail},
        ) from exc
    except WorkflowWaitConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except WorkflowNotWaitingError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except TaskDefinitionUnavailableError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, exc.detail) from exc
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
    primary = _primary_session(engine, task)
    session_info = sessions.get(task_id, primary)
    if session_info is None or session_info.status != "idle":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"task {task_id} session {primary!r} is not idle",
        )
    if await supervisor.acquire(task_id, primary) is None:
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
