"""REST endpoints under /api/. Commands only — events go out over the WebSocket.

Architecture: ADR-0004 (docs/adr/0004-use-rest-and-websocket-snapshot-deltas.md)
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Mapping
from dataclasses import asdict
from importlib.metadata import version as package_version
from typing import Any, Literal

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    Response,
    WebSocket,
    status,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine

from ompire_daemon import auth
from ompire_daemon.advisories import AdvisorySampler
from ompire_daemon.agent import AgentHandle, AgentSupervisor, NoLiveAgentError
from ompire_daemon.api.deps import (
    _advisories,
    _cleanup_service,
    _config,
    _engine,
    _events,
    _exports,
    _gh,
    _gpg,
    _guard,
    _notifications,
    _results,
    _reviews,
    _sessions,
    _settings,
    _ships,
    _supervisor,
)
from ompire_daemon.api.work_models import TaskOut
from ompire_daemon.application.cleanup import (
    CleanupConflictError,
    CleanupService,
    WorkshopTeardownError,
)
from ompire_daemon.auth import require_bearer_token
from ompire_daemon.config import Config
from ompire_daemon.datadir import audit_log_path_for
from ompire_daemon.events import EventHub
from ompire_daemon.gh import GitHubProbe
from ompire_daemon.gpg import (
    FINGERPRINT_RE,
    GpgProbe,
)
from ompire_daemon.isolation import (
    WorkspaceBlockedError,
    WorkspaceBusyError,
    WorkspaceGuard,
)
from ompire_daemon.notifications import AttentionNotifier
from ompire_daemon.registry.result_exports import (
    CheckoutBusyError,
    ExportNotFoundError,
    ExportRequestMismatchError,
    ExportsActiveError,
    ExportStateError,
)
from ompire_daemon.registry.results import (
    CAPTURE_DEADLINE_SECONDS,
    MAX_FILE_BYTES,
    MAX_FILES,
    MAX_PATH_BYTES,
    MAX_PATH_COMPONENTS,
    MAX_TOTAL_BYTES,
    SUPPORTED_EXTENSIONS,
    CaptureInProgressError,
    InvalidSelectionError,
    ResultNotAttachableError,
    ResultNotFoundError,
    ResultPurgedError,
    ResultReferencedError,
    ResultsRetainedError,
    ResultStateError,
    SelectionMismatchError,
    StaleRevisionError,
    results_version,
)
from ompire_daemon.registry.settings import (
    SettingsStore,
    SettingsValidationError,
)
from ompire_daemon.registry.ships import DeliveryConflictError
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
)
from ompire_daemon.result_exports import (
    ExportError,
    ExportUnsupportedError,
    ResultExportManager,
)
from ompire_daemon.results import (
    CaptureError,
    ResultManager,
    ResultUnavailableError,
)
from ompire_daemon.review import (
    ReviewAlreadyOpenError,
    ReviewContentError,
    ReviewError,
    ReviewManager,
)
from ompire_daemon.rpc import AgentGoneError, RequestFailedError
from ompire_daemon.runauthority import (
    SOURCE_LEGACY_CONTINUATION,
    resolve_authority,
    review_admission,
    writer_refusal,
)
from ompire_daemon.sessions import SessionTracker
from ompire_daemon.ship import (
    DeliveryBlockedError,
    PreviewMismatchError,
    ShipError,
    ShipManager,
    UnresolvedEffectError,
)
from ompire_daemon.taskdefinition import (
    TaskDefinitionUnavailableError,
    resolve_task_definition,
)
from ompire_daemon.work.projects import (
    Project,
    ProjectNotReadyError,
    get_project,
)
from ompire_daemon.work.tasks import (
    Task,
    TaskConfigurationRequiredError,
    TaskNotArchivedError,
    TaskNotFoundError,
    get_task,
    purge_task,
)
from ompire_daemon.workflow_definitions import (
    GateStep,
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

# REST authentication boundary: ADR-0002
# (docs/adr/0002-run-as-local-daemon-with-stateless-web-ui.md)
router = APIRouter(prefix="/api", dependencies=[Depends(require_bearer_token)])

# The work routers are included once, here, so every route stays behind the
# common bearer-authenticated `/api` boundary (ADR-0002).
from ompire_daemon.api.profiles import router as profiles_router
from ompire_daemon.api.projects import router as projects_router
from ompire_daemon.api.tasks import router as tasks_router

router.include_router(projects_router)
router.include_router(profiles_router)
router.include_router(tasks_router)




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
    # Format 3: the one privileged effect a delivery step performs, and the
    # gate whose answer is the only thing that can authorize it.
    action: str | None = None
    approval: str | None = None


class WorkflowOut(BaseModel):
    name: str
    # What a *new* launch of this name would pin, and the semantics version it
    # is read under.
    revision: str
    format: int
    primary_session: str
    sessions: list[str]
    steps: list[StepDescriptorOut]
    # Every privileged effect this definition can perform, in effect order.
    # An empty list is a statement — this workflow cannot publish anything —
    # not an absence of information.
    actions: list[str] = []
    # Whether the definition declares independent review at all.
    reviews: bool = False


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
# format-4 definition. One agent step that gets the operator's prompt, and an
# ending that says what finishing meant — because format 2 onwards does not
# let a run stop by falling off the end of the list.
#
# It publishes nothing. Review, an approval gate, and the delivery actions it
# authorizes are things an author adds deliberately; a new workflow that could
# already sign and push would make publication the default rather than a
# decision somebody made.
STARTER_TEMPLATE = """\
# A new workflow. Edit it, Validate it, then save an executable revision.
# Saving a draft keeps your text; only an executable save makes this
# launchable.
#
# This workflow publishes nothing. To publish, add a `review` step, a gate
# whose `delivery` binds that review, and the `delivery` actions one of its
# choices authorizes.
format: 4
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






@router.post("/tasks/{task_id}/cleanup", response_model=TaskOut)
async def cleanup_task_route(
    task_id: int,
    cleanup: CleanupService = Depends(_cleanup_service),
) -> dict[str, Any]:
    """Tear one task's workspace down and archive it.

    Wire and error mapping only: the admission rules, guarded teardown, and
    finalization live in the application cleanup service.

    409 carries either a plain refusal message or the code-plus-message pair
    a run-position refusal produces; 502 means the container could not be
    torn down and the clone was retained.
    """
    try:
        return await cleanup.cleanup_task(task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except CleanupConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, exc.detail) from exc
    except WorkshopTeardownError as exc:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"workshop remove failed; clone retained:\n{exc.stderr}",
        ) from exc


# --- Durable task results (ADR-0034) ----------------------------------------
#
# Task-scoped, because a result belongs to the task that produced it and the
# task in the path is part of the authorization, not decoration. Everything
# here addresses *retained* bytes: no route in this section reads the task's
# workspace, and none of them grants a workflow, review, or publication effect.


class ResultCaptureIn(BaseModel):
    """One capture request.

    `request_id` is the caller's replay key. A repeated request with the same
    selection answers with the original operation — including its failure —
    so a lost response is recovered from the task's result history rather than
    by capturing whatever the workspace holds now.
    """

    paths: list[str] = Field(min_length=1, max_length=MAX_FILES)
    request_id: str = Field(min_length=1, max_length=128)


class ResultDecisionIn(BaseModel):
    """The exact revision a decision is about. Acceptance and purge both carry
    it, so a decision can never be applied to content the operator did not
    review."""

    expected_manifest_id: str = Field(min_length=1, max_length=128)


class ResultPurgeIn(ResultDecisionIn):
    """Purge additionally carries the task's result version the operator was
    looking at, and an explicit acknowledgement. Deleting the only copy of an
    accepted result is not a click that should succeed against a stale page."""

    expected_version: int = Field(ge=0)
    acknowledge_purge: bool


def _result_limits() -> dict[str, Any]:
    """The fixed bounds, served so the UI can state them before submission
    rather than reproducing them and drifting."""
    return {
        "max_files": MAX_FILES,
        "max_file_bytes": MAX_FILE_BYTES,
        "max_total_bytes": MAX_TOTAL_BYTES,
        "max_path_components": MAX_PATH_COMPONENTS,
        "capture_deadline_seconds": CAPTURE_DEADLINE_SECONDS,
        "supported_extensions": list(SUPPORTED_EXTENSIONS),
    }


def _result_error(exc: Exception) -> HTTPException:
    """One place where every result refusal picks its status code.

    422 is a request that was never well formed, 404 an unknown revision or
    file, 409 a state or expectation that no longer holds, and 410 a purged
    revision — which is a real, permanent answer with a readable record behind
    it, not a missing resource.
    """
    if isinstance(exc, InvalidSelectionError):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    if isinstance(exc, ResultNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, ResultPurgedError):
        return HTTPException(status.HTTP_410_GONE, str(exc))
    if isinstance(
        exc,
        (
            CaptureInProgressError,
            SelectionMismatchError,
            StaleRevisionError,
            ResultStateError,
            ResultReferencedError,
            ResultNotAttachableError,
            ResultUnavailableError,
            WorkspaceBusyError,
            WorkspaceBlockedError,
            CaptureError,
        ),
    ):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    raise exc


def _require_result(results: ResultManager, task_id: int, result_id: str):
    try:
        return results.require_result(task_id, result_id)
    except ResultNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


# Downloads are ordinary authenticated JSON-era responses with a body: the
# bearer token travels in the header, never in a URL a browser would put in
# history, and nothing is served from a static directory.
_DOWNLOAD_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "private, no-store",
}


@router.get("/tasks/{task_id}/results")
def list_task_results_route(
    task_id: int,
    engine: Engine = Depends(_engine),
    results: ResultManager = Depends(_results),
) -> dict[str, Any]:
    _require_task(engine, task_id)
    return {**results.projection(task_id), "limits": _result_limits()}


@router.post("/tasks/{task_id}/results", status_code=status.HTTP_202_ACCEPTED)
async def capture_task_result_route(
    task_id: int,
    body: ResultCaptureIn,
    engine: Engine = Depends(_engine),
    results: ResultManager = Depends(_results),
) -> dict[str, Any]:
    """Admit a capture and return the metadata projection.

    202, because the operation outlives the request. A browser that goes away
    still gets a committed `ready` or `failed` revision it can find in the
    task's result history.
    """
    _require_task(engine, task_id)
    try:
        projection = await results.capture(
            task_id, paths=body.paths, request_id=body.request_id
        )
    except Exception as exc:
        raise _result_error(exc) from exc
    return {**projection, "limits": _result_limits()}


@router.get("/tasks/{task_id}/results/{result_id}")
def get_task_result_route(
    task_id: int,
    result_id: str,
    engine: Engine = Depends(_engine),
    results: ResultManager = Depends(_results),
) -> dict[str, Any]:
    _require_task(engine, task_id)
    result = _require_result(results, task_id, result_id)
    return {
        "result": results.result_payload(result),
        "manifest": result.manifest,
        "version": results_version(engine, task_id),
    }


@router.get("/tasks/{task_id}/results/{result_id}/file")
def get_task_result_file_route(
    task_id: int,
    result_id: str,
    path: str,
    engine: Engine = Depends(_engine),
    results: ResultManager = Depends(_results),
) -> dict[str, Any]:
    """One retained file as text, for the escaped source preview.

    Returned as JSON rather than as a rendered document on purpose: the client
    displays it as inert text, and nothing here invites a browser to interpret
    agent-authored Markdown, HTML, or SVG as active content.
    """
    _require_task(engine, task_id)
    result = _require_result(results, task_id, result_id)
    try:
        data, entry = results.read_file(result, path)
    except Exception as exc:
        raise _result_error(exc) from exc
    return {
        "result_id": result.id,
        "manifest_id": result.manifest_id,
        "path": entry.path,
        "media_type": entry.media_type,
        "length": entry.length,
        "sha256": entry.sha256,
        "text": data.decode("utf-8", errors="replace"),
    }


@router.get("/tasks/{task_id}/results/{result_id}/diff")
def get_task_result_diff_route(
    task_id: int,
    result_id: str,
    engine: Engine = Depends(_engine),
    results: ResultManager = Depends(_results),
) -> dict[str, Any]:
    _require_task(engine, task_id)
    result = _require_result(results, task_id, result_id)
    try:
        return results.diff(result)
    except Exception as exc:
        raise _result_error(exc) from exc


@router.get("/tasks/{task_id}/results/{result_id}/download")
def download_task_result_route(
    task_id: int,
    result_id: str,
    path: str | None = None,
    engine: Engine = Depends(_engine),
    results: ResultManager = Depends(_results),
) -> Response:
    """The whole revision as one ZIP, or one file as an attachment.

    Always the retained bytes, never a fresh read of the workspace — including
    for a task whose workspace no longer exists.
    """
    _require_task(engine, task_id)
    result = _require_result(results, task_id, result_id)
    try:
        if path is not None:
            data, entry = results.read_file(result, path)
            filename = entry.path.rsplit("/", 1)[-1]
            media_type = entry.media_type
        else:
            data = results.zip_bundle(result)
            filename = f"{result.id}.zip"
            media_type = "application/zip"
    except Exception as exc:
        raise _result_error(exc) from exc
    return Response(
        content=data,
        media_type=media_type,
        headers={
            **_DOWNLOAD_HEADERS,
            # The quoted name is a plain basename derived from a manifest path
            # that already passed the selection rules, so it carries no
            # separators, quotes, or control characters.
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@router.post("/tasks/{task_id}/results/{result_id}/accept")
def accept_task_result_route(
    task_id: int,
    result_id: str,
    body: ResultDecisionIn,
    engine: Engine = Depends(_engine),
    results: ResultManager = Depends(_results),
) -> dict[str, Any]:
    """Record that the operator reviewed and is keeping this exact revision.

    It advances no workflow, answers no review, and authorizes no publication.
    """
    _require_task(engine, task_id)
    result = _require_result(results, task_id, result_id)
    try:
        projection = results.accept(
            result, expected_manifest_id=body.expected_manifest_id
        )
    except Exception as exc:
        raise _result_error(exc) from exc
    return {**projection, "limits": _result_limits()}


@router.delete("/tasks/{task_id}/results/{result_id}")
def purge_task_result_route(
    task_id: int,
    result_id: str,
    body: ResultPurgeIn,
    engine: Engine = Depends(_engine),
    results: ResultManager = Depends(_results),
) -> dict[str, Any]:
    """Remove one revision's retained bytes, leaving its record behind.

    Never reachable from cleanup, and it has no force variant: the two
    expectations and the acknowledgement all have to name the revision the
    operator was actually looking at.
    """
    _require_task(engine, task_id)
    result = _require_result(results, task_id, result_id)
    if not body.acknowledge_purge:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "purging a result deletes its retained files permanently; confirm "
            "the purge explicitly",
        )
    try:
        projection = results.purge(
            result,
            expected_manifest_id=body.expected_manifest_id,
            expected_version=body.expected_version,
        )
    except Exception as exc:
        raise _result_error(exc) from exc
    return {**projection, "limits": _result_limits()}


# --- Checkout export (ADR-0036) ---------------------------------------------
#
# Nested under the revision it delivers, because an export is a decision about
# one exact retained revision and the task scope in the path is part of the
# authorization. Nothing here accepts a host directory: the only destination
# root is the producing project's own registered checkout, resolved by the
# daemon.


class ExportSelectionIn(BaseModel):
    """The subset of a revision to export, and where to put it.

    `paths` are literal manifest paths — not directories, not globs. `prefix`
    is one optional checkout-relative directory; an empty prefix keeps the
    revision's own repository-relative destinations. Individual files are never
    renamed.
    """

    expected_manifest_id: str = Field(min_length=1, max_length=128)
    paths: list[str] = Field(min_length=1, max_length=MAX_FILES)
    prefix: str = Field(default="", max_length=MAX_PATH_BYTES)


class ExportConfirmIn(ExportSelectionIn):
    """A confirmation of exactly one preview.

    `preview_token` names the document the operator reviewed; the daemon
    recomputes it from a fresh observation and refuses anything else, so a
    client can neither supply a verdict nor confirm against a checkout that has
    changed. `request_id` is the replay key.
    """

    preview_token: str = Field(min_length=1, max_length=128)
    request_id: str = Field(min_length=1, max_length=128)
    acknowledge_export: bool


class ExportVersionIn(BaseModel):
    expected_version: int = Field(ge=0)


class ExportAcknowledgeIn(ExportVersionIn):
    acknowledge_unknown_outcome: bool


def _export_error(exc: Exception) -> HTTPException:
    """Where every export refusal picks its status code.

    422 for a request that was never well formed, 404 for a revision or export
    outside this task, 409 for a state, preview, or reservation that no longer
    holds, and 410 for a purged source. A *blocked* preview is not an error: it
    is a successful read whose answer is "not like this".
    """
    if isinstance(exc, ExportUnsupportedError):
        return HTTPException(status.HTTP_409_CONFLICT, exc.detail)
    if isinstance(exc, ExportError):
        if exc.reason in (
            "empty-selection",
            "duplicate-selection",
            "unknown-file",
            "invalid-prefix",
            "invalid-destination",
            "reserved-destination",
            "destination-collision",
        ):
            return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, exc.detail)
        return HTTPException(status.HTTP_409_CONFLICT, exc.detail)
    if isinstance(exc, ExportNotFoundError):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(
        exc,
        (
            ExportStateError,
            ExportRequestMismatchError,
            CheckoutBusyError,
            ExportsActiveError,
        ),
    ):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    return _result_error(exc)


def _require_export(exports: ResultExportManager, task_id: int, export_id: str):
    try:
        return exports.require_export(task_id, export_id)
    except ExportNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post("/tasks/{task_id}/results/{result_id}/exports/preview")
def preview_result_export_route(
    task_id: int,
    result_id: str,
    body: ExportSelectionIn,
    engine: Engine = Depends(_engine),
    exports: ResultExportManager = Depends(_exports),
) -> Response:
    """Read the retained revision and the real checkout, and classify.

    Read-only: it creates nothing, writes nothing, and runs no Git command. A
    conflict is reported, not resolved — a difference is never an approval to
    replace. The response is `no-store` because it can carry the contents of
    the operator's own checkout.
    """
    _require_task(engine, task_id)
    try:
        payload = exports.preview(
            task_id=task_id,
            result_id=result_id,
            expected_manifest_id=body.expected_manifest_id,
            paths=body.paths,
            prefix=body.prefix,
        )
    except Exception as exc:
        raise _export_error(exc) from exc
    return JSONResponse(payload, headers={"Cache-Control": "private, no-store"})


@router.post(
    "/tasks/{task_id}/results/{result_id}/exports",
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_result_export_route(
    task_id: int,
    result_id: str,
    body: ExportConfirmIn,
    engine: Engine = Depends(_engine),
    results: ResultManager = Depends(_results),
    exports: ResultExportManager = Depends(_exports),
) -> dict[str, Any]:
    """Admit one approved export and return the result projection.

    202, because the operation outlives the request: a browser that goes away
    still gets a settled, inspectable record rather than an effect nobody knows
    about. A repeated `request_id` with the same confirmation returns the
    original operation instead of exporting twice.
    """
    _require_task(engine, task_id)
    if not body.acknowledge_export:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "exporting writes files into your project checkout; confirm the "
            "reviewed preview explicitly",
        )
    try:
        record, _created = await exports.start(
            task_id=task_id,
            result_id=result_id,
            expected_manifest_id=body.expected_manifest_id,
            paths=body.paths,
            prefix=body.prefix,
            token=body.preview_token,
            request_id=body.request_id,
        )
    except Exception as exc:
        raise _export_error(exc) from exc
    return {
        **results.projection(task_id),
        "limits": _result_limits(),
        "export_id": record.id,
    }


@router.get("/tasks/{task_id}/results/{result_id}/exports/{export_id}")
def get_result_export_route(
    task_id: int,
    result_id: str,
    export_id: str,
    engine: Engine = Depends(_engine),
    results: ResultManager = Depends(_results),
    exports: ResultExportManager = Depends(_exports),
) -> dict[str, Any]:
    """One export's approved detail and per-destination history.

    Reading never installs anything, and the approved preview document is
    returned as it was recorded — the operator's own record of what they
    agreed to, not a fresh observation dressed up as one.
    """
    _require_task(engine, task_id)
    record = _require_export(exports, task_id, export_id)
    if record.result_id != result_id:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"export {export_id!r} not found"
        )
    return {
        "export": exports.export_payload(record),
        "preview": record.preview,
        "version": results_version(engine, task_id),
    }


@router.post("/tasks/{task_id}/results/{result_id}/exports/{export_id}/reconcile")
def reconcile_result_export_route(
    task_id: int,
    result_id: str,
    export_id: str,
    body: ExportVersionIn,
    engine: Engine = Depends(_engine),
    results: ResultManager = Depends(_results),
    exports: ResultExportManager = Depends(_exports),
) -> dict[str, Any]:
    """Re-observe an unresolved export's destinations, read-only.

    Never a retry. It can only move an outcome from unknown to something that
    can be established, and it writes no file in the checkout.
    """
    _require_task(engine, task_id)
    record = _require_export(exports, task_id, export_id)
    if record.result_id != result_id:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"export {export_id!r} not found"
        )
    actual = results_version(engine, task_id)
    if actual != body.expected_version:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"the task's results changed (version {actual}, not "
            f"{body.expected_version}); reload before reconciling",
        )
    try:
        exports.recheck(record)
    except Exception as exc:
        raise _export_error(exc) from exc
    return {**results.projection(task_id), "limits": _result_limits()}


@router.post("/tasks/{task_id}/results/{result_id}/exports/{export_id}/acknowledge")
def acknowledge_result_export_route(
    task_id: int,
    result_id: str,
    export_id: str,
    body: ExportAcknowledgeIn,
    engine: Engine = Depends(_engine),
    results: ResultManager = Depends(_results),
    exports: ResultExportManager = Depends(_exports),
) -> dict[str, Any]:
    """Close an unresolved export without claiming its unknowns resolved.

    It touches no file and rewrites no per-destination outcome: the record
    keeps saying exactly what could not be established, and only stops holding
    the checkout and the retained bytes.
    """
    _require_task(engine, task_id)
    record = _require_export(exports, task_id, export_id)
    if record.result_id != result_id:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"export {export_id!r} not found"
        )
    if not body.acknowledge_unknown_outcome:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "this export has effects nobody could classify; acknowledge that "
            "explicitly to close it",
        )
    try:
        exports.acknowledge(record, expected_version=body.expected_version)
    except Exception as exc:
        raise _export_error(exc) from exc
    return {**results.projection(task_id), "limits": _result_limits()}


# --- Agent control surface --------------------------------------------------
# Start and prompt are the workflow engine's job now (workflow-engine design
# D-4); stop stays as the manual kill switch, feeding the tracker's
# operator-stop reason. Everything session-scoped is addressed
# `/tasks/{id}/sessions/{name}/agent/*` (design D-1).


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


def _refuse_run_writer(engine: Engine, task_id: int) -> None:
    """Refuse a daemon-managed writer the run's current position forbids."""
    try:
        task = get_task(engine, task_id)
    except TaskNotFoundError:
        return
    refusal = writer_refusal(engine, task)
    if refusal is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, {"code": refusal[0], "message": refusal[1]}
        )


async def _require_live_agent(
    supervisor: AgentSupervisor,
    task_id: int,
    session: str,
    guard: WorkspaceGuard | None = None,
    engine: Engine | None = None,
) -> AgentHandle:
    """The session's live agent, taken inside its own boundary (ADR-0027).

    A composer action prompts the session, so it must not be handed a child
    that a concurrent policy handoff has already started retiring. It runs on
    whatever policy that session last applied — never a reset to the task
    default.

    It is also a workspace writer, so it is admitted through the same guard a
    review, a delivery, or a workflow step takes (ADR-0032): the daemon refuses
    the new turn rather than interrupting whoever holds the workspace, and
    refuses it outright while an unresolved privileged effect is outstanding.

    And it is admitted against the run's *position* as well (ADR-0033). A run
    parked at a publication decision holds no lock — it is waiting for a
    person — so the guard alone would let a turn change the very content that
    decision is about.
    """
    if guard is not None:
        try:
            guard.assert_host_free(task_id)
        except (WorkspaceBusyError, WorkspaceBlockedError) as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    if engine is not None:
        _refuse_run_writer(engine, task_id)
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
    guard: WorkspaceGuard = Depends(_guard),
) -> dict[str, object]:
    task = _require_task(engine, task_id)
    _require_declared_session(engine, task, session)
    handle = await _require_live_agent(
        supervisor, task_id, session, guard, engine
    )
    return await _agent_request(handle, "steer", message=body.message)


@router.post("/tasks/{task_id}/sessions/{session}/agent/follow-up")
async def follow_up_agent_route(
    task_id: int,
    session: str,
    body: AgentMessage,
    engine: Engine = Depends(_engine),
    supervisor: AgentSupervisor = Depends(_supervisor),
    guard: WorkspaceGuard = Depends(_guard),
) -> dict[str, object]:
    task = _require_task(engine, task_id)
    _require_declared_session(engine, task, session)
    handle = await _require_live_agent(
        supervisor, task_id, session, guard, engine
    )
    return await _agent_request(handle, "follow_up", message=body.message)


@router.post("/tasks/{task_id}/sessions/{session}/agent/interrupt")
async def interrupt_agent_route(
    task_id: int,
    session: str,
    body: AgentMessage,
    engine: Engine = Depends(_engine),
    supervisor: AgentSupervisor = Depends(_supervisor),
    sessions: SessionTracker = Depends(_sessions),
    guard: WorkspaceGuard = Depends(_guard),
) -> dict[str, object]:
    task = _require_task(engine, task_id)
    _require_declared_session(engine, task, session)
    handle = await _require_live_agent(
        supervisor, task_id, session, guard, engine
    )
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
    # An answer that authorizes publication carries the delivery it confirmed.
    # A generic resume cannot answer one: without the token there is no
    # evidence the operator saw the content, the target, and the identities
    # the confirmation is about, and that evidence is the authorization.
    preview_token: str | None = None
    request_id: str | None = None
    message: str = ""
    pr_title: str = ""
    pr_body: str = ""


def _authorizes_delivery(
    revision: WorkflowRevision, waiting: Any, choice_id: str
) -> bool:
    """Whether this answer would grant publication, per the pinned document."""
    step = revision.definition.step_named(waiting.step)
    if not isinstance(step, GateStep):
        return False
    choice = step.choice_named(choice_id)
    return choice is not None and choice.authorize is not None


async def _answer_with_delivery(
    task: Task, body: WorkflowResumeBody, request: Request, engine: Engine
) -> dict[str, Any]:
    """Answer an approving choice, with the delivery it authorizes.

    Deliberately the same operation Ship flow's confirm button calls. Two
    surfaces, one authorization: whichever page the operator is on, the
    decision, the grant, and the run's move to its first action are one
    transaction over the same checked preview.
    """
    ships: ShipManager = request.app.state.ships
    if not body.preview_token or not body.request_id:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {
                "field": "preview_token",
                "detail": (
                    "this answer authorizes publication; confirm it against a "
                    "delivery preview ('request_id' and 'preview_token') so "
                    "what is authorized is what was shown"
                ),
            },
        )
    assert body.choice_id is not None
    try:
        resolved = await ships.preview(
            task,
            commit_message=body.message,
            pr_title=body.pr_title,
            pr_body=body.pr_body,
            request_id=body.request_id,
            gate_seq=body.expected_seq,
            choice_id=body.choice_id,
        )
        if resolved.fingerprint != body.preview_token:
            raise PreviewMismatchError(
                "the delivery changed since it was previewed; review the new "
                "preview before confirming"
            )
        _delivery_id, _projection = await ships.confirm(
            task,
            resolved,
            runner=request.app.state.workflow_runner,
            note=body.note,
        )
    except PreviewMismatchError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, {"message": str(exc)}) from exc
    except DeliveryBlockedError as exc:
        raise _delivery_conflict(exc) from exc
    except DeliveryConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, {"message": str(exc)}) from exc
    except ShipError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, {"message": str(exc)}) from exc
    updated = get_task(engine, task.id)
    return {
        "task_id": task.id,
        "workflow": "answered",
        "choice_id": body.choice_id,
        "step": updated.workflow_step,
        "result": updated.workflow_result,
        "delivery": resolved.ending,
    }


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
            if _authorizes_delivery(revision, waiting, body.choice_id):
                return await _answer_with_delivery(task, body, request, engine)
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

    # Whether a review may start is a property of the run, not of an agent
    # (ADR-0033). A format-3 workflow starts its own review when it reaches
    # the step that declares one, and starting a second by hand would grade
    # content the run is still changing.
    authority = resolve_authority(engine, task)
    refusal = review_admission(authority, task)
    if refusal is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, {"code": refusal[0], "message": refusal[1]}
        )
    if not authority.declares_review:
        # An older definition has no review step, so the operator drives it —
        # and the primary session is the conversation the comments go back to,
        # which is why it has to be there and idle.
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
        state = await reviews.start_review(
            task, workflow_seq=authority.review_seq
        )
    except (WorkspaceBusyError, WorkspaceBlockedError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except (ReviewAlreadyOpenError, ReviewContentError) as exc:
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
        "candidate_id": state.candidate_id,
        "iterations": [
            {
                "outcome": it.outcome,
                "comment_count": it.comment_count,
                "stderr": it.stderr,
                "candidate_id": it.candidate_id,
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
        "candidate_id": state.candidate_id,
        "iterations": [
            {
                "outcome": it.outcome,
                "comment_count": it.comment_count,
                "stderr": it.stderr,
                "candidate_id": it.candidate_id,
                "recorded_at": it.recorded_at,
            }
            for it in state.iterations
        ],
    }


class ShipDraftBody(BaseModel):
    replace: bool = False


class ShipDraftSaveBody(BaseModel):
    """Operator-entered publication text. Inert: it authorizes nothing."""

    commit_message: str = ""
    pr_title: str = ""
    pr_body: str = ""


class ShipPreviewBody(BaseModel):
    """The decision being previewed, and the text it would publish.

    `ending` and `mode` are *not* requests. The run's pinned chain decides how
    far a delivery goes and how it composes history; supplying either is a
    caller stating what it believes, and a disagreement is reported rather
    than silently resolved in the caller's favour.
    """

    ending: str | None = None
    mode: str | None = None
    message: str = ""
    pr_title: str = ""
    pr_body: str = ""
    request_id: str
    delivery_id: int | None = None
    # Which question is being answered, and with which answer. Required when
    # the run is waiting at an approval: a preview that did not name them
    # would describe "whatever this task could publish", which is not a
    # decision anyone can confirm.
    gate_seq: int | None = None
    choice_id: str | None = None


class ShipCommitBody(BaseModel):
    """One authorization. Every field the preview fingerprinted is required —
    there is no omitted-ending legacy shape and no tokenless path."""

    ending: str | None = None
    mode: str | None = None
    message: str = ""
    pr_title: str = ""
    pr_body: str = ""
    request_id: str
    preview_token: str
    delivery_id: int | None = None
    expected_version: int | None = None
    gate_seq: int | None = None
    choice_id: str | None = None
    # Feedback recorded with the decision, exactly as a gate answer's is.
    note: str | None = None


class ShipContinueBody(BaseModel):
    """Continue an interrupted or pre-upgrade chain for a verified result."""

    ending: str | None = None
    pr_title: str = ""
    pr_body: str = ""
    request_id: str
    preview_token: str
    delivery_id: int
    expected_version: int


class ShipReconcileBody(BaseModel):
    delivery_id: int
    action_id: int
    expected_version: int
    decision: str
    note: str | None = None
    adopt_reference: str | None = None


def _delivery_conflict(exc: Exception) -> HTTPException:
    """Conflicts return the safe current state and the reason, never a bare
    string the operator has to guess at."""
    detail: dict[str, Any] = {"message": str(exc)}
    if isinstance(exc, DeliveryBlockedError):
        detail["blockers"] = [asdict(blocker) for blocker in exc.blockers]
    return HTTPException(status.HTTP_409_CONFLICT, detail)


@router.get("/tasks/{task_id}/ship")
def get_ship_route(
    task_id: int,
    engine: Engine = Depends(_engine),
    ships: ShipManager = Depends(_ships),
) -> dict[str, Any]:
    """The task's current delivery projection.

    The same document the snapshot and every command response carry, for a
    caller that wants to read it without holding a WebSocket. Read-only.
    """
    task = _require_task(engine, task_id)
    return ships.projection(task) or ships.empty_projection(task_id)


@router.post("/tasks/{task_id}/ship/draft")
async def draft_ship_route(
    task_id: int,
    body: ShipDraftBody | None = None,
    engine: Engine = Depends(_engine),
    ships: ShipManager = Depends(_ships),
) -> dict[str, Any]:
    task = _require_task(engine, task_id)
    try:
        return await ships.draft(
            task, replace=body.replace if body is not None else False
        )
    except (WorkspaceBusyError, WorkspaceBlockedError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    except ShipError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.put("/tasks/{task_id}/ship/draft")
def save_ship_draft_route(
    task_id: int,
    body: ShipDraftSaveBody,
    engine: Engine = Depends(_engine),
    ships: ShipManager = Depends(_ships),
) -> dict[str, Any]:
    task = _require_task(engine, task_id)
    try:
        return ships.save_manual_draft(task, body.model_dump())
    except ShipError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc


@router.post("/tasks/{task_id}/ship/preview")
async def preview_ship_route(
    task_id: int,
    body: ShipPreviewBody,
    engine: Engine = Depends(_engine),
    ships: ShipManager = Depends(_ships),
) -> dict[str, Any]:
    """Resolve one requested ending. Read-only, and authorizes nothing.

    Everything a confirmation needs is here: the candidate and the review that
    covers it, the remaining actions, the safe targets and identities, every
    reason the delivery is currently refused, and the fingerprint the
    confirmation must carry.
    """
    task = _require_task(engine, task_id)
    try:
        resolved = await ships.preview(
            task,
            ending=body.ending,
            mode=body.mode,
            commit_message=body.message,
            pr_title=body.pr_title,
            pr_body=body.pr_body,
            request_id=body.request_id,
            delivery_id=body.delivery_id,
            gate_seq=body.gate_seq,
            choice_id=body.choice_id,
        )
    except DeliveryBlockedError as exc:
        raise _delivery_conflict(exc) from exc
    except PreviewMismatchError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except TaskConfigurationRequiredError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return resolved.payload()


@router.post("/tasks/{task_id}/ship/commit")
async def commit_ship_route(
    task_id: int,
    body: ShipCommitBody,
    request: Request,
    engine: Engine = Depends(_engine),
    ships: ShipManager = Depends(_ships),
) -> dict[str, Any]:
    """Accept one authorization and start its authorized action prefix.

    This route parses and authenticates. Every safety check — review binding,
    accepted target, mode, credentials, exclusivity, replay — happens inside
    the delivery service, so a direct service or API caller gets exactly the
    same admission the UI does.
    """
    task = _require_task(engine, task_id)
    runner: WorkflowRunner = request.app.state.workflow_runner
    try:
        resolved = await ships.preview(
            task,
            ending=body.ending,
            mode=body.mode,
            commit_message=body.message,
            pr_title=body.pr_title,
            pr_body=body.pr_body,
            request_id=body.request_id,
            delivery_id=body.delivery_id,
            gate_seq=body.gate_seq,
            choice_id=body.choice_id,
        )
        if resolved.fingerprint != body.preview_token:
            raise PreviewMismatchError(
                "the delivery changed since it was previewed; review the new "
                "preview before confirming"
            )
        delivery_id, projection = await ships.confirm(
            task,
            resolved,
            runner=runner,
            note=body.note,
            expected_version=body.expected_version,
        )
    except PreviewMismatchError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, {"message": str(exc)}) from exc
    except DeliveryBlockedError as exc:
        raise _delivery_conflict(exc) from exc
    except WorkflowGateChoiceError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"field": exc.field, "detail": exc.detail},
        ) from exc
    except (WorkflowWaitConflictError, WorkflowNotWaitingError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, {"message": str(exc)}) from exc
    except DeliveryConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, {"message": str(exc)}) from exc
    except (WorkspaceBusyError, WorkspaceBlockedError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, {"message": str(exc)}) from exc
    except ShipError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, {"message": str(exc)}) from exc

    if resolved.source == SOURCE_LEGACY_CONTINUATION:
        # Only a pre-upgrade grant is scheduled here. A workflow decision —
        # an approval, or a continuation of an interrupted action — moved the
        # run itself, and the run performs its own steps.
        ships.start_delivery(
            task, delivery_id, body.request_id, request.app.state.spawn_scheduler.jobs
        )
    return projection


async def _continue_delivery(
    task_id: int,
    body: ShipContinueBody,
    request: Request,
    engine: Engine,
    ships: ShipManager,
    required: str,
) -> dict[str, Any]:
    """Shared body for the push and pull-request continuation routes.

    Neither starts an implicit earlier action: the requested ending must be one
    this delivery has a verified result for up to, and the service refuses
    anything else.
    """
    task = _require_task(engine, task_id)
    try:
        resolved = await ships.preview(
            task,
            ending=body.ending,
            commit_message="",
            pr_title=body.pr_title,
            pr_body=body.pr_body,
            request_id=body.request_id,
            delivery_id=body.delivery_id,
        )
        if required not in resolved.remaining_actions:
            raise PreviewMismatchError(
                f"this delivery has no remaining {required} action to authorize"
            )
        if resolved.fingerprint != body.preview_token:
            raise PreviewMismatchError(
                "the delivery changed since it was previewed; review the new "
                "preview before confirming"
            )
        delivery_id, projection = await ships.confirm(
            task,
            resolved,
            runner=request.app.state.workflow_runner,
            expected_version=body.expected_version,
        )
    except PreviewMismatchError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, {"message": str(exc)}) from exc
    except DeliveryBlockedError as exc:
        raise _delivery_conflict(exc) from exc
    except DeliveryConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, {"message": str(exc)}) from exc
    except (WorkspaceBusyError, WorkspaceBlockedError) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, {"message": str(exc)}) from exc
    except ShipError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, {"message": str(exc)}) from exc

    ships.start_delivery(
        task, delivery_id, body.request_id, request.app.state.spawn_scheduler.jobs
    )
    return projection


@router.post("/tasks/{task_id}/ship/push")
async def push_ship_route(
    task_id: int,
    body: ShipContinueBody,
    request: Request,
    engine: Engine = Depends(_engine),
    ships: ShipManager = Depends(_ships),
) -> dict[str, Any]:
    """Push an existing verified signed result, and optionally go on to a PR."""
    return await _continue_delivery(task_id, body, request, engine, ships, "push")


@router.post("/tasks/{task_id}/ship/pr")
async def pr_ship_route(
    task_id: int,
    body: ShipContinueBody,
    request: Request,
    engine: Engine = Depends(_engine),
    ships: ShipManager = Depends(_ships),
) -> dict[str, Any]:
    """Open a pull request for an existing verified pushed result."""
    return await _continue_delivery(task_id, body, request, engine, ships, "pr")


@router.post("/tasks/{task_id}/ship/reconcile")
async def reconcile_ship_route(
    task_id: int,
    body: ShipReconcileBody,
    engine: Engine = Depends(_engine),
    ships: ShipManager = Depends(_ships),
) -> dict[str, Any]:
    """Record one operator decision about an unresolved delivery effect.

    None of the decisions write anything privileged. `retry` only makes a
    proven-not-executed action eligible for a fresh preview and confirmation;
    it is not an unconditional write button.
    """
    task = _require_task(engine, task_id)
    if body.decision not in ("recheck", "adopt", "retry", "abandon"):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {"field": "decision", "detail": f"unknown decision {body.decision!r}"},
        )
    try:
        return await ships.reconcile(
            task,
            delivery_id=body.delivery_id,
            action_id=body.action_id,
            expected_version=body.expected_version,
            decision=body.decision,
            note=body.note,
            adopt_reference=body.adopt_reference,
        )
    except PreviewMismatchError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, {"message": str(exc)}) from exc
    except UnresolvedEffectError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, {"message": str(exc)}) from exc
    except DeliveryConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, {"message": str(exc)}) from exc
    except ShipError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, {"message": str(exc)}) from exc


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
    guard: WorkspaceGuard = Depends(_guard),
    notifications: AttentionNotifier = Depends(_notifications),
    results: ResultManager = Depends(_results),
) -> dict[str, int]:
    try:
        storage_paths, released_producers = purge_task(engine, task_id)
    except TaskNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    except (TaskNotArchivedError, ResultsRetainedError) as exc:
        # A retained result is the operator's, not this task's to take with it
        # (ADR-0034). The refusal names the revisions to purge first, and — by
        # construction — nothing has been deleted by the time it is raised.
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    # This consumer released its pinned inputs (ADR-0035). Publish the
    # producers *after* the commit, so a purge dialog open in another tab stops
    # showing a blocker that no longer exists.
    for producer_task_id in released_producers:
        results.publish(producer_task_id)
    reviews.drop_review(task_id)
    ships.drop_ship(task_id)
    ships.purge_candidate_storage(storage_paths)
    guard.discard(task_id)
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
