"""The workflow library: which procedures exist and which revision each one
currently means.

Two stores, deliberately separate (ADR-0031):

- `workflow_revisions` (see `registry/workflow_definitions.py`) is append-only.
  A row there is an executable document identified by its content, and a task
  pins one. Nothing in this module updates or deletes one.
- `workflow_library` is the mutable part an operator owns: one entry per name,
  carrying inert draft text, an optional *current* revision, an archive flag,
  and an edit version.

The edit version is not the content revision. Two tabs comparing revisions
would let a comment-only change silently overwrite a colleague's edit, because
comments do not change what a definition means; the edit version counts edits,
so a lost update is refused whether or not the semantics moved.

Draft text is inert by construction. It is stored as submitted — invalid,
empty, or nonsense alike — and nothing here parses it. Only an explicit
executable save validates text, and only a validated document is retained and
selected. That is why a broken draft can never poison the catalog, block a
launch, or stop the daemon at startup.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Connection, Engine

from ompire_daemon.db import workflow_library
from ompire_daemon.registry.model_profiles import reserved_write
from ompire_daemon.registry.workflow_definitions import (
    RevisionSummary,
    WorkflowRevisionUnavailableError,
    get_revision_conn,
    insert_revision,
    list_revisions_conn,
)
from ompire_daemon.workflow_definitions import (
    MAX_DOCUMENT_BYTES,
    WorkflowDescriptor,
    WorkflowRevision,
    describe,
    is_slug_name,
)

logger = logging.getLogger(__name__)

ORIGIN_BUILTIN = "builtin"
ORIGIN_CUSTOM = "custom"

# Why an entry cannot be launched. Distinguishable on purpose: "you have not
# saved an executable revision yet" and "the revision you saved cannot be read
# back" are different problems with different fixes, and an operator who is
# told only "unavailable" has to guess which one they have.
UNAVAILABLE_DRAFT_ONLY = "draft_only"
UNAVAILABLE_ARCHIVED = "archived"
UNAVAILABLE_NOT_PACKAGED = "not_packaged"

# `missing`, `unsupported_format`, `integrity`, and `invalid` come through
# unchanged from the retained-revision read, so the reason an entry is
# unlaunchable is the same reason the revision itself reports.


class UnknownWorkflowNameError(ValueError):
    """No library entry carries this name."""

    def __init__(self, name: str) -> None:
        super().__init__(f"unknown workflow {name!r}")
        self.name = name


class WorkflowNotLaunchableError(Exception):
    """The entry exists but cannot be the current choice for a new launch.

    Never a substitution: an archived, draft-only, or damaged entry refuses
    the launch and says why, rather than resolving to some other revision.
    """

    def __init__(self, name: str, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.name = name
        self.reason = reason
        self.detail = detail


class InvalidWorkflowNameError(ValueError):
    def __init__(self, name: str) -> None:
        super().__init__(
            f"invalid workflow name {name!r}: must be lowercase alphanumerics "
            "and hyphens"
        )
        self.name = name


class DuplicateWorkflowNameError(Exception):
    """The name is taken — by a live entry, an archived one, or a built-in.

    Archived names stay reserved: an archived entry keeps its drafts, its
    revisions, and the tasks that ran it, so handing the name to a new
    procedure would make that history read as this one's.
    """

    def __init__(self, name: str, origin: str, archived: bool) -> None:
        detail = (
            f"the built-in workflow {name!r} owns this name"
            if origin == ORIGIN_BUILTIN
            else f"an archived workflow named {name!r} still reserves this name"
            if archived
            else f"a workflow named {name!r} already exists"
        )
        super().__init__(detail)
        self.name = name
        self.origin = origin
        self.archived = archived


class WorkflowEntryNotFoundError(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(f"workflow {name!r} is not in the library")
        self.name = name


class WorkflowVersionConflictError(Exception):
    """Someone else committed an edit to this entry first. Nothing changed."""

    def __init__(self, name: str, expected: int, actual: int) -> None:
        super().__init__(
            f"workflow {name!r} was edited elsewhere (expected version "
            f"{expected}, found {actual}); reload before saving again"
        )
        self.name = name
        self.expected = expected
        self.actual = actual


class BuiltinWorkflowReadOnlyError(Exception):
    """Built-ins are packaged examples. Duplication is their editing path."""

    def __init__(self, name: str) -> None:
        super().__init__(
            f"{name!r} is a built-in workflow and cannot be edited or archived; "
            "duplicate it under a new name to customize it"
        )
        self.name = name


class ArchivedWorkflowError(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(
            f"workflow {name!r} is archived; restore it before editing it"
        )
        self.name = name


class WorkflowDraftTooLargeError(ValueError):
    def __init__(self, size: int) -> None:
        super().__init__(
            f"workflow text is {size} bytes; the limit is {MAX_DOCUMENT_BYTES}"
        )
        self.size = size
        self.limit = MAX_DOCUMENT_BYTES


class WorkflowNameMismatchError(ValueError):
    """The submitted document renames the entry it was saved into.

    A name is an entry's identity, not a field: renaming means creating a
    distinct entry, so this is a validation error rather than a rename.
    """

    def __init__(self, entry_name: str, document_name: str) -> None:
        super().__init__(
            f"this document declares the name {document_name!r}, but it is "
            f"being saved into the workflow {entry_name!r}; a name is an "
            "entry's identity — create a separate workflow to rename it"
        )
        self.entry_name = entry_name
        self.document_name = document_name


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class LibraryEntry:
    """One library entry's summary: everything but its text and its history.

    Raw draft text and retained revision history are fetched on detail rather
    than carried on every list, snapshot, and event — they are large, and a
    list view answers "which workflows exist and can they launch".
    """

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
    # The launch preview's shape, present only when this entry is actually
    # eligible: Spawn renders from this, so an archived or damaged entry
    # cannot be offered by a client reading the same payload.
    descriptor: WorkflowDescriptor | None


@dataclass(frozen=True)
class LibraryDetail:
    entry: LibraryEntry
    draft_yaml: str | None
    revisions: list[RevisionSummary]


def validate_workflow_name(name: str) -> None:
    if not is_slug_name(name):
        raise InvalidWorkflowNameError(name)


def check_draft_size(text: str) -> None:
    size = len(text.encode("utf-8"))
    if size > MAX_DOCUMENT_BYTES:
        raise WorkflowDraftTooLargeError(size)


def _summarize(conn: Connection, row) -> LibraryEntry:
    """One row plus whatever its current revision can be read as.

    A revision that cannot be decoded makes *this* entry unavailable with a
    reason. It is caught here, per entry, so one damaged pointer costs the
    operator one workflow rather than the whole library.
    """
    archived = bool(row.archived)
    revision: WorkflowRevision | None = None
    reason: str | None = None
    detail: str | None = None
    if row.current_revision is None:
        reason = (
            UNAVAILABLE_NOT_PACKAGED
            if row.origin == ORIGIN_BUILTIN
            else UNAVAILABLE_DRAFT_ONLY
        )
        detail = (
            "this built-in is not shipped by the running package any more; its "
            "retained revisions stay readable and existing tasks are unaffected"
            if row.origin == ORIGIN_BUILTIN
            else "no executable revision has been saved yet; a draft cannot run"
        )
    else:
        try:
            revision = get_revision_conn(conn, row.current_revision)
        except WorkflowRevisionUnavailableError as exc:
            reason = exc.reason
            detail = exc.detail
    if reason is None and archived:
        reason = UNAVAILABLE_ARCHIVED
        detail = "archived; restore it to launch it again"
    available = reason is None
    return LibraryEntry(
        name=row.name,
        origin=row.origin,
        archived=archived,
        version=row.version,
        has_draft=row.draft_yaml is not None,
        current_revision=row.current_revision,
        current_format=revision.format if revision is not None else None,
        available=available,
        unavailable_reason=reason,
        unavailable_detail=detail,
        created_at=row.created_at,
        updated_at=row.updated_at,
        descriptor=describe(revision) if available and revision is not None else None,
    )


def _read_row(conn: Connection, name: str):
    return conn.execute(
        workflow_library.select().where(workflow_library.c.name == name)
    ).first()


def _require_row(conn: Connection, name: str):
    row = _read_row(conn, name)
    if row is None:
        raise WorkflowEntryNotFoundError(name)
    return row


def _require_mutable(row, expected_version: int):
    """The two checks every mutation of an existing entry shares.

    Both run inside the caller's write reservation, against the row just read
    there — a version compared outside it would be a
    time-of-check-to-time-of-use bug wearing a version number.

    Archive state is checked by each caller instead, because restoring is the
    one mutation an archived entry has to accept.
    """
    if row.origin == ORIGIN_BUILTIN:
        raise BuiltinWorkflowReadOnlyError(row.name)
    if row.version != expected_version:
        raise WorkflowVersionConflictError(row.name, expected_version, row.version)


def list_entries_conn(conn: Connection) -> list[LibraryEntry]:
    rows = conn.execute(
        workflow_library.select().order_by(workflow_library.c.name)
    ).all()
    return [_summarize(conn, row) for row in rows]


def list_entries(engine: Engine) -> list[LibraryEntry]:
    """Every entry, in name order: draft-only, archived, and damaged included.

    The library is what exists, not what can launch. Filtering happens where
    launching happens.
    """
    with engine.connect() as conn:
        return list_entries_conn(conn)


def launchable_descriptors(entries: Sequence[LibraryEntry]) -> list[WorkflowDescriptor]:
    """The eligible launch catalog, derived from the very same entry list.

    Deriving it here rather than from a second query is what keeps the catalog
    and the library from disagreeing: one read, one archive flag, one
    availability decision.
    """
    return [entry.descriptor for entry in entries if entry.descriptor is not None]


def get_detail(engine: Engine, name: str) -> LibraryDetail:
    with engine.connect() as conn:
        row = _require_row(conn, name)
        return LibraryDetail(
            entry=_summarize(conn, row),
            draft_yaml=row.draft_yaml,
            revisions=list_revisions_conn(conn, workflow_name=name),
        )


def resolve_current(conn: Connection, name: str) -> WorkflowRevision:
    """What a *new* launch of this name would pin, read on this connection.

    Read through the caller's connection on purpose: acceptance resolves the
    project, the profile, and the workflow inside one reservation, so an
    archive or an executable save committing alongside it cannot slip between
    the check and the write. A task already accepted never comes back here —
    it reads its own pinned revision.
    """
    row = _read_row(conn, name)
    if row is None:
        raise UnknownWorkflowNameError(name)
    entry = _summarize(conn, row)
    if not entry.available:
        raise WorkflowNotLaunchableError(
            name,
            entry.unavailable_reason or "unavailable",
            f"workflow {name!r} cannot be launched: {entry.unavailable_detail}",
        )
    # `available` is only true when the revision decoded, so this hits the
    # revision cache rather than re-reading the row.
    return get_revision_conn(conn, row.current_revision)


def create_entry(engine: Engine, *, name: str, yaml_text: str) -> LibraryDetail:
    """A new custom entry holding inert draft text and no executable revision.

    Creation never validates and never selects a revision: a new workflow is
    something to write, and a draft that cannot run yet is the normal state of
    one. Launching it is refused until an executable save succeeds.

    Built-in names are refused by the same existence check as any other taken
    name, because a built-in *is* a row here — including one a later package
    stopped shipping, whose entry stays behind for the tasks that ran it.
    """
    validate_workflow_name(name)
    check_draft_size(yaml_text)
    now = _now_iso()
    with reserved_write(engine) as conn:
        clash = _read_row(conn, name)
        if clash is not None:
            raise DuplicateWorkflowNameError(name, clash.origin, bool(clash.archived))
        conn.execute(
            workflow_library.insert().values(
                name=name,
                origin=ORIGIN_CUSTOM,
                draft_yaml=yaml_text,
                current_revision=None,
                archived=0,
                version=1,
                created_at=now,
                updated_at=now,
            )
        )
        row = _require_row(conn, name)
        return LibraryDetail(
            entry=_summarize(conn, row), draft_yaml=row.draft_yaml, revisions=[]
        )


def save_draft(
    engine: Engine, name: str, *, yaml_text: str, expected_version: int
) -> LibraryDetail:
    """Persist the editor's text exactly as submitted.

    Any UTF-8 within the size limit, including empty and invalid YAML: a draft
    is text somebody is still working on. It never displaces the entry's
    current revision, so saving something broken cannot take a launchable
    workflow away.
    """
    check_draft_size(yaml_text)
    now = _now_iso()
    with reserved_write(engine) as conn:
        row = _require_row(conn, name)
        _require_mutable(row, expected_version)
        if row.archived:
            raise ArchivedWorkflowError(name)
        conn.execute(
            workflow_library.update()
            .where(workflow_library.c.name == name)
            .values(draft_yaml=yaml_text, version=row.version + 1, updated_at=now)
        )
        return _committed_detail(conn, name)


def save_revision(
    engine: Engine,
    name: str,
    *,
    revision: WorkflowRevision,
    yaml_text: str,
    expected_version: int,
) -> LibraryDetail:
    """Retain the validated document, select it, and save its text — as one
    transaction.

    Parsing and validation happen in the caller, outside this reservation: the
    write lock covers the version check, the retention, and the selection, and
    nothing else. Retention is idempotent, so re-saving semantically identical
    YAML reuses the existing content revision instead of inventing a second
    version of the same procedure — while the entry's *edit* version still
    advances, because a comment change is a real edit.
    """
    check_draft_size(yaml_text)
    if revision.name != name:
        raise WorkflowNameMismatchError(name, revision.name)
    now = _now_iso()
    with reserved_write(engine) as conn:
        row = _require_row(conn, name)
        _require_mutable(row, expected_version)
        if row.archived:
            raise ArchivedWorkflowError(name)
        insert_revision(conn, revision)
        conn.execute(
            workflow_library.update()
            .where(workflow_library.c.name == name)
            .values(
                draft_yaml=yaml_text,
                current_revision=revision.revision,
                version=row.version + 1,
                updated_at=now,
            )
        )
        return _committed_detail(conn, name)


def set_archived(
    engine: Engine, name: str, *, archived: bool, expected_version: int
) -> LibraryDetail:
    """Take an entry out of future launch choices, or put it back.

    Nothing is deleted: the draft, every retained revision, and every task that
    ran one are untouched. Restoring makes the retained current revision
    eligible again if it is still readable; a draft-only entry comes back
    draft-only, because archiving never granted it a revision it did not have.
    """
    now = _now_iso()
    with reserved_write(engine) as conn:
        row = _require_row(conn, name)
        _require_mutable(row, expected_version)
        conn.execute(
            workflow_library.update()
            .where(workflow_library.c.name == name)
            .values(
                archived=1 if archived else 0,
                version=row.version + 1,
                updated_at=now,
            )
        )
        return _committed_detail(conn, name)


def _committed_detail(conn: Connection, name: str) -> LibraryDetail:
    """The committed row, read back inside the same transaction that wrote it.

    A second read on a fresh connection would be a different point in time,
    and the response an operator's editor reconciles against has to be the one
    this write produced.
    """
    row = _require_row(conn, name)
    return LibraryDetail(
        entry=_summarize(conn, row),
        draft_yaml=row.draft_yaml,
        revisions=list_revisions_conn(conn, workflow_name=name),
    )


@dataclass(frozen=True)
class BuiltinConflict:
    name: str
    detail: str


def synchronize_builtins(
    engine: Engine, revisions: Sequence[WorkflowRevision]
) -> list[BuiltinConflict]:
    """Point each packaged name at what this package ships, in one transaction.

    Built-in entries are package-owned: their current revision follows the
    installed definitions, and they carry no draft because their text is in the
    package. A name the package no longer ships keeps its entry and its
    retained history but stops being launchable — deleting it would take an old
    task's readable procedure with it.

    Custom work is never overwritten. If a package introduces a built-in whose
    name an operator already used, the collision is reported and the custom
    entry is left exactly as it is: refusing to start would leave nobody able
    to rename it.
    """
    now = _now_iso()
    conflicts: list[BuiltinConflict] = []
    packaged = {revision.name: revision for revision in revisions}
    with reserved_write(engine) as conn:
        for name, revision in sorted(packaged.items()):
            insert_revision(conn, revision)
            row = _read_row(conn, name)
            if row is None:
                conn.execute(
                    workflow_library.insert().values(
                        name=name,
                        origin=ORIGIN_BUILTIN,
                        draft_yaml=None,
                        current_revision=revision.revision,
                        archived=0,
                        version=1,
                        created_at=now,
                        updated_at=now,
                    )
                )
                continue
            if row.origin != ORIGIN_BUILTIN:
                conflicts.append(
                    BuiltinConflict(
                        name=name,
                        detail=(
                            f"the packaged workflow {name!r} cannot be installed: "
                            "a custom workflow already owns that name, and it was "
                            "left untouched"
                        ),
                    )
                )
                continue
            if row.current_revision == revision.revision:
                continue
            conn.execute(
                workflow_library.update()
                .where(workflow_library.c.name == name)
                .values(
                    current_revision=revision.revision,
                    version=row.version + 1,
                    updated_at=now,
                )
            )
        stale = conn.execute(
            workflow_library.select()
            .with_only_columns(workflow_library.c.name, workflow_library.c.version)
            .where(workflow_library.c.origin == ORIGIN_BUILTIN)
            .where(workflow_library.c.current_revision.is_not(None))
        ).all()
        for row in stale:
            if row.name in packaged:
                continue
            conn.execute(
                workflow_library.update()
                .where(workflow_library.c.name == row.name)
                .values(
                    current_revision=None, version=row.version + 1, updated_at=now
                )
            )
    for conflict in conflicts:
        logger.error("%s", conflict.detail)
    return conflicts
