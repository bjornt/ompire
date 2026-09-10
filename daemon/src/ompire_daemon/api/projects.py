"""Work router: projects, checkouts, setup retry, and reconciliation.

Thin by construction — each route converts its wire body, calls one
application command or public work query, and maps that owner's domain
errors to status codes. Admission rules live in the command layer; storage
lives in the work package; neither is reimplemented here.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import Engine

from ompire_daemon.api.deps import _config, _engine, _events, _project_setup, _settings
from ompire_daemon.api.errors import launch_error
from ompire_daemon.api.work_models import (
    CheckoutInspectIn,
    CheckoutInspectOut,
    ProjectCreate,
    ProjectFilesOut,
    ProjectOut,
    ProjectReconciliationIn,
    ProjectUpdate,
    RemoteOut,
)
from ompire_daemon.application import work
from ompire_daemon.config import Config
from ompire_daemon.events import EventHub
from ompire_daemon.registry.settings import SettingsStore
from ompire_daemon.work.checkout import (
    InvalidRemoteNameError,
    InvalidRepoUrlError,
    inspect_checkout,
    inspection_message,
    validate_remote_name,
    validate_repo_url,
)
from ompire_daemon.work.files import (
    DEFAULT_LIMIT as FILE_SEARCH_DEFAULT_LIMIT,
)
from ompire_daemon.work.files import (
    MAX_LIMIT as FILE_SEARCH_MAX_LIMIT,
)
from ompire_daemon.work.files import (
    ProjectFilesError,
    search_project_files,
)
from ompire_daemon.work.launch import LaunchInputError
from ompire_daemon.work.profiles import UnknownModelProfileReferenceError
from ompire_daemon.work.projects import (
    DuplicateProjectError,
    InvalidBranchPatternError,
    InvalidWorkshopAdditionsError,
    Project,
    ProjectHasReferencingTasksError,
    ProjectNotFoundError,
    ProjectNotReadyError,
    ProjectSetupBusyError,
    get_project,
    list_projects,
)
from ompire_daemon.work.reconciliation import (
    ProjectDecision,
    ReconciliationConflictError,
    project_reconciliation,
)
from ompire_daemon.work.setup import ProjectSetupManager

router = APIRouter()


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


def _supplied_workspace_defaults(body: ProjectUpdate) -> dict[str, Any]:
    """Only the workspace defaults the caller actually mentioned. Everything
    else stays `UNSUPPLIED`, so a client written before these fields existed
    cannot blank them by omission — and an explicit null is refused rather
    than read as a reset, because none of them has a meaningful empty value
    except `preamble`, whose empty string is a real choice."""
    supplied: dict[str, Any] = {}
    for field in ("base_branch", "branch_pattern", "workshop_additions", "preamble"):
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
    if project.setup_state != "ready":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            str(ProjectNotReadyError(project.name, project.setup_state)),
        )
    return project


@router.get("/projects", response_model=list[ProjectOut])
def list_projects_route(engine: Engine = Depends(_engine)) -> list[Project]:
    return list_projects(engine)


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
    upstream_url, fork_url = _validated_urls(body)
    try:
        return await work.register_project(
            engine,
            config,
            events,
            settings,
            setup,
            work.ProjectRegistration(
                name=body.name,
                title=body.title,
                upstream_url=upstream_url,
                fork_url=fork_url,
                checkout_path=body.checkout_path,
                checkout_mode=body.checkout_mode,
                fetch_remote=body.fetch_remote,
                default_model_profile=body.default_model_profile,
                base_branch=body.base_branch,
                branch_pattern=body.branch_pattern,
                workshop_additions=body.workshop_additions,
                preamble=body.preamble,
            ),
        )
    except work.CheckoutPathSuppliedInCloneModeError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except work.DestinationExistsError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except work.UnusableCheckoutError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
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
        return await work.retry_project_setup(setup, name)
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
        return await work.change_project(
            engine,
            config,
            events,
            name,
            work.ProjectChanges(
                title=body.title,
                upstream_url=upstream_url,
                fork_url=fork_url,
                checkout_path=body.checkout_path,
                fetch_remote=fetch_remote,
                new_name=body.new_name,
                profile_supplied="default_model_profile" in body.model_fields_set,
                profile=body.default_model_profile,
                supplied_workspace_defaults=_supplied_workspace_defaults(body),
            ),
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except (
        ProjectSetupBusyError,
        work.ImmutableCheckoutPathError,
        DuplicateProjectError,
        ProjectHasReferencingTasksError,
    ) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except work.UnusableCheckoutError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except (
        UnknownModelProfileReferenceError,
        InvalidBranchPatternError,
        InvalidWorkshopAdditionsError,
    ) as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)
        ) from exc


@router.delete("/projects/{name}")
def delete_project_route(
    name: str, engine: Engine = Depends(_engine), events: EventHub = Depends(_events)
) -> dict[str, str]:
    try:
        work.remove_project(engine, events, name)
    except ProjectNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except ProjectSetupBusyError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ProjectHasReferencingTasksError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {"deleted": name}


# --- Upgrade reconciliation: a project's carried-over launch configuration ----
# Never resolved by guessing (ADR-0026).


@router.get("/projects/{name}/launch-reconciliation")
def get_project_reconciliation_route(
    name: str, engine: Engine = Depends(_engine)
) -> dict[str, Any]:
    try:
        return project_reconciliation(engine, name)
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
        return work.confirm_reconciliation(
            engine,
            events,
            name,
            ProjectDecision(
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
    except ReconciliationConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except (LaunchInputError, InvalidBranchPatternError, InvalidWorkshopAdditionsError) as exc:
        raise launch_error(exc) from exc
