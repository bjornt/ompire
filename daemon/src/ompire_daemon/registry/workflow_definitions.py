"""Retained workflow revisions: Core queries against `workflow_revisions`.

Append-only by construction. There is no update and no delete, because the
value of a retained revision is precisely that it cannot change: a task points
at one, and what that task ran has to stay readable after the packaged
definition moves on, after the workflow's name leaves the catalog, and after
the workspace has been cleaned up.

Reads are cached by *revision*, never by workflow name — caching by name is
the exact mistake this table exists to prevent — and a cached entry is only
ever a document whose content was decoded, re-validated, and re-hashed back to
the key it was stored under. A row that fails any of those is reported as
unavailable rather than executed.

ADR-0028 (docs/adr/0028-retain-declarative-workflow-revisions.md)
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Connection, Engine

from ompire_daemon.db import workflow_revisions
from ompire_daemon.workflow_definitions import (
    WorkflowDocumentError,
    WorkflowRevision,
    load_canonical_document,
    make_revision,
)

logger = logging.getLogger(__name__)

# Why a retained revision cannot be executed. These are the distinguishable
# readiness reasons the API and the UI report; "something went wrong" would
# leave the operator unable to tell a daemon downgrade from a damaged row.
UNAVAILABLE_MISSING = "missing"
UNAVAILABLE_UNSUPPORTED = "unsupported_format"
UNAVAILABLE_INTEGRITY = "integrity"
UNAVAILABLE_INVALID = "invalid"


class WorkflowRevisionUnavailableError(Exception):
    """A pinned revision cannot be read as an executable definition.

    Blocks the affected task's execution only. It never substitutes the
    catalog's current definition, and it never removes the task from a list:
    one damaged row must not take the dashboard with it.
    """

    def __init__(self, revision: str, reason: str, detail: str) -> None:
        super().__init__(f"workflow revision {revision} is unavailable: {detail}")
        self.revision = revision
        self.reason = reason
        self.detail = detail


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class _RevisionCache:
    """Process-local, keyed by content identity, therefore never stale."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, WorkflowRevision] = {}

    def get(self, revision: str) -> WorkflowRevision | None:
        with self._lock:
            return self._entries.get(revision)

    def put(self, revision: WorkflowRevision) -> None:
        with self._lock:
            self._entries[revision.revision] = revision

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_cache = _RevisionCache()


def clear_cache() -> None:
    """Test support: forget decoded revisions between isolated databases."""
    _cache.clear()


def _decode(revision: str, format_version: int, raw: str) -> WorkflowRevision:
    """Decode, re-validate, and verify a stored row's content identity."""
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WorkflowRevisionUnavailableError(
            revision, UNAVAILABLE_INTEGRITY, f"stored document is not JSON: {exc}"
        ) from exc
    try:
        definition = load_canonical_document(document)
    except WorkflowDocumentError as exc:
        reason = (
            UNAVAILABLE_UNSUPPORTED
            if getattr(exc, "location", "") == "format"
            else UNAVAILABLE_INVALID
        )
        raise WorkflowRevisionUnavailableError(revision, reason, str(exc)) from exc
    rebuilt = make_revision(definition)
    if rebuilt.revision != revision:
        # The stored bytes do not hash back to the key they are filed under.
        # Executing them would be executing a definition nobody accepted.
        raise WorkflowRevisionUnavailableError(
            revision,
            UNAVAILABLE_INTEGRITY,
            "stored document does not match its content identity",
        )
    if rebuilt.format != format_version:
        raise WorkflowRevisionUnavailableError(
            revision,
            UNAVAILABLE_INTEGRITY,
            f"stored format {format_version} does not match the document",
        )
    return rebuilt


def insert_revision(conn: Connection, revision: WorkflowRevision) -> bool:
    """Retain one revision if it is not already there. True when inserted.

    Idempotent on purpose: every daemon start re-registers the packaged
    definitions, and an unchanged definition must not produce a new row or a
    new identity.
    """
    existing = conn.execute(
        workflow_revisions.select()
        .with_only_columns(workflow_revisions.c.revision)
        .where(workflow_revisions.c.revision == revision.revision)
    ).first()
    if existing is not None:
        return False
    conn.execute(
        workflow_revisions.insert().values(
            revision=revision.revision,
            workflow_name=revision.name,
            format=revision.format,
            document_json=json.dumps(
                revision.document, sort_keys=True, separators=(",", ":")
            ),
            created_at=_now_iso(),
        )
    )
    return True


def register_revisions(engine: Engine, revisions: list[WorkflowRevision]) -> list[str]:
    """Retain a set of revisions in one transaction. Returns the new ones."""
    inserted: list[str] = []
    with engine.begin() as conn:
        for revision in revisions:
            if insert_revision(conn, revision):
                inserted.append(revision.revision)
            _cache.put(revision)
    return inserted


def get_revision_conn(conn: Connection, revision: str) -> WorkflowRevision:
    cached = _cache.get(revision)
    if cached is not None:
        return cached
    row = conn.execute(
        workflow_revisions.select().where(workflow_revisions.c.revision == revision)
    ).first()
    if row is None:
        raise WorkflowRevisionUnavailableError(
            revision, UNAVAILABLE_MISSING, "no such retained revision"
        )
    decoded = _decode(revision, row.format, row.document_json)
    _cache.put(decoded)
    return decoded


def get_revision(engine: Engine, revision: str) -> WorkflowRevision:
    """The retained definition, decoded and verified, or a classified refusal."""
    cached = _cache.get(revision)
    if cached is not None:
        return cached
    with engine.connect() as conn:
        return get_revision_conn(conn, revision)


@dataclass(frozen=True)
class RevisionSummary:
    revision: str
    workflow_name: str
    format: int
    created_at: str


def list_revisions_conn(
    conn: Connection, *, workflow_name: str | None = None
) -> list[RevisionSummary]:
    """Retained history, oldest first, on a caller-supplied connection.

    Takes a connection so an executable save can return the history it just
    appended to from inside its own transaction, rather than from a later read
    that might have moved on.
    """
    query = workflow_revisions.select().order_by(workflow_revisions.c.created_at)
    if workflow_name is not None:
        query = query.where(workflow_revisions.c.workflow_name == workflow_name)
    return [
        RevisionSummary(
            revision=row.revision,
            workflow_name=row.workflow_name,
            format=row.format,
            created_at=row.created_at,
        )
        for row in conn.execute(query).all()
    ]


def list_revisions(engine: Engine, *, workflow_name: str | None = None) -> list[RevisionSummary]:
    with engine.connect() as conn:
        return list_revisions_conn(conn, workflow_name=workflow_name)
