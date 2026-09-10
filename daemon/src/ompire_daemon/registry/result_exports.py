"""The durable journal behind checkout export: what an operator approved, what
Ompire intended to write, and what actually happened to each destination.

An export is a *privileged external effect* on a directory Ompire does not own
(ADR-0036). That shapes everything here:

- the approved preview document is stored whole, because the approval is that
  document. A row that kept only a token could say an export was approved but
  not what was approved;
- intent is written before the effect, per file, so a crash leaves a record of
  what was about to happen rather than a silence to interpret;
- a per-file outcome distinguishes `created`, `already-identical`,
  `not-installed`, and `unknown`, and nothing here ever promotes `unknown` to a
  success. An acknowledgement records that an operator read the uncertainty; it
  does not resolve it;
- the active root reservation is a partial unique index, not an in-memory lock,
  so two project registrations aliasing one directory cannot both install into
  it and a restart does not drop the reservation.

No filesystem work happens in this module, and no reserved write spans an
`await` or an `open`. `result_exports.py` owns that side of the boundary.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, Engine, select

from ompire_daemon.db import (
    projects,
    result_export_files,
    result_exports,
    tasks,
)
from ompire_daemon.platform.transactions import reserved_write
from ompire_daemon.registry.results import (
    ResultNotFoundError,
    StaleRevisionError,
    bump_results_version,
    canonical_json,
    verify_attachable_on,
    verify_payload_on,
)

# --- Operation states -------------------------------------------------------
#
# Four, and no "failed": an export that wrote nothing is still `incomplete`,
# because the honest statement is about how much of the approved set was
# delivered, not about whether a code path raised.

STATE_RUNNING = "running"
STATE_COMPLETED = "completed"
STATE_INCOMPLETE = "incomplete"
STATE_UNRESOLVED = "unresolved"

TERMINAL_STATES = (STATE_COMPLETED, STATE_INCOMPLETE)
ACTIVE_STATES = (STATE_RUNNING, STATE_UNRESOLVED)

# --- Per-destination outcomes -----------------------------------------------

OUTCOME_PENDING = "pending"
OUTCOME_CREATED = "created"
OUTCOME_IDENTICAL = "already-identical"
OUTCOME_NOT_INSTALLED = "not-installed"
OUTCOME_UNKNOWN = "unknown"

# --- Approved classifications -----------------------------------------------

CLASS_CREATE = "create"
CLASS_IDENTICAL = "identical"
CLASS_CONFLICT = "conflict"

PREVIEW_FORMAT = 1


class ExportNotFoundError(Exception):
    def __init__(self, export_id: str) -> None:
        super().__init__(f"export {export_id!r} not found")
        self.export_id = export_id


class ExportStateError(Exception):
    """The export exists but is not in a state that admits this command."""

    def __init__(self, export_id: str, detail: str) -> None:
        super().__init__(f"export {export_id} {detail}")
        self.export_id = export_id
        self.detail = detail


class ExportRequestMismatchError(Exception):
    """A repeated request id carrying a different confirmation.

    Answering it would return an operation the caller never asked for under an
    id they believe identifies their own request.
    """

    def __init__(self, request_id: str, export_id: str) -> None:
        super().__init__(
            f"request {request_id!r} already confirmed a different export "
            f"({export_id}); use a new request id to export again"
        )
        self.request_id = request_id
        self.export_id = export_id


class CheckoutBusyError(Exception):
    """Another export owns this checkout root, or left it unresolved.

    Keyed by the root's device and inode, so it is raised for two project
    registrations that name the same directory by different paths.
    """

    def __init__(self, export_id: str, checkout_path: str, state: str) -> None:
        super().__init__(
            f"export {export_id} is {state} for the checkout at "
            f"{checkout_path}; resolve it before exporting there again"
        )
        self.export_id = export_id
        self.checkout_path = checkout_path
        self.state = state


class ExportsActiveError(Exception):
    """A running or unresolved export is holding something the caller wants to
    remove or repoint. Names the exports, because "an export is active" is not
    something an operator can act on."""

    def __init__(self, subject: str, export_ids: list[str]) -> None:
        listed = ", ".join(export_ids)
        super().__init__(
            f"{subject} has {len(export_ids)} unfinished checkout export(s) "
            f"({listed}); resolve or acknowledge them first"
        )
        self.subject = subject
        self.export_ids = export_ids


@dataclass(frozen=True)
class ExportFileRecord:
    manifest_path: str
    seq: int
    destination: str
    classification: str
    expected_length: int
    expected_sha256: str
    before: dict[str, Any] | None
    staged_device: int | None
    staged_inode: int | None
    outcome: str
    observed: dict[str, Any] | None
    error: str | None


@dataclass(frozen=True)
class ExportRecord:
    id: str
    task_id: int
    result_id: str
    manifest_id: str
    request_id: str
    selection: tuple[str, ...]
    selection_fingerprint: str
    prefix: str
    preview_token: str
    preview: dict[str, Any]
    project_name: str
    checkout_path: str
    root_device: int
    root_inode: int
    state: str
    error: str | None
    staging_name: str
    staging_device: int | None
    staging_inode: int | None
    staging_error: str | None
    created_directories: tuple[str, ...]
    actor: str
    confirmed_at: str
    started_at: str | None
    finished_at: str | None
    acknowledged_at: str | None
    acknowledged_by: str | None
    files: tuple[ExportFileRecord, ...]

    @property
    def active(self) -> bool:
        return self.state in ACTIVE_STATES


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def new_export_id() -> str:
    return f"exp_{secrets.token_hex(16)}"


def staging_name_for(export_id: str) -> str:
    """The one staging directory name an export may create.

    Derived from the daemon-generated export id, never from anything an
    operator or an agent supplies, so no input can aim staging at an existing
    path.
    """
    return f".ompire-export-{export_id}"


def _loads(value: str | None) -> Any:
    return json.loads(value) if value else None


def _row_to_file(row) -> ExportFileRecord:
    return ExportFileRecord(
        manifest_path=row.manifest_path,
        seq=int(row.seq),
        destination=row.destination,
        classification=row.classification,
        expected_length=int(row.expected_length),
        expected_sha256=row.expected_sha256,
        before=_loads(row.before_json),
        staged_device=row.staged_device,
        staged_inode=row.staged_inode,
        outcome=row.outcome,
        observed=_loads(row.observed_json),
        error=row.error,
    )


def _row_to_export(row, files: Sequence[ExportFileRecord]) -> ExportRecord:
    return ExportRecord(
        id=row.id,
        task_id=int(row.task_id),
        result_id=row.result_id,
        manifest_id=row.manifest_id,
        request_id=row.request_id,
        selection=tuple(json.loads(row.selection_json)),
        selection_fingerprint=row.selection_fingerprint,
        prefix=row.prefix,
        preview_token=row.preview_token,
        preview=json.loads(row.preview_json),
        project_name=row.project_name,
        checkout_path=row.checkout_path,
        root_device=int(row.root_device),
        root_inode=int(row.root_inode),
        state=row.state,
        error=row.error,
        staging_name=row.staging_name,
        staging_device=row.staging_device,
        staging_inode=row.staging_inode,
        staging_error=row.staging_error,
        created_directories=tuple(_loads(row.created_directories_json) or ()),
        actor=row.actor,
        confirmed_at=row.confirmed_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
        acknowledged_at=row.acknowledged_at,
        acknowledged_by=row.acknowledged_by,
        files=tuple(files),
    )


def _read_export_on(conn: Connection, export_id: str) -> ExportRecord | None:
    row = conn.execute(
        result_exports.select().where(result_exports.c.id == export_id)
    ).first()
    if row is None:
        return None
    return _row_to_export(row, _read_files_on(conn, export_id))


def _read_files_on(conn: Connection, export_id: str) -> list[ExportFileRecord]:
    rows = conn.execute(
        result_export_files.select()
        .where(result_export_files.c.export_id == export_id)
        .order_by(result_export_files.c.seq)
    ).all()
    return [_row_to_file(row) for row in rows]


# --- Reads ------------------------------------------------------------------


def get_export(engine: Engine, export_id: str) -> ExportRecord:
    with engine.connect() as conn:
        record = _read_export_on(conn, export_id)
    if record is None:
        raise ExportNotFoundError(export_id)
    return record


def require_export(engine: Engine, task_id: int, export_id: str) -> ExportRecord:
    """An export, proven to belong to the task in the route.

    The task scope is part of the authorization, exactly as it is for the
    result it exports: an export addressed under the wrong task is unknown, not
    someone else's history.
    """
    record = get_export(engine, export_id)
    if record.task_id != task_id:
        raise ExportNotFoundError(export_id)
    return record


def list_task_exports(engine: Engine, task_id: int) -> list[ExportRecord]:
    """Newest first, with their per-destination history."""
    with engine.connect() as conn:
        rows = conn.execute(
            result_exports.select()
            .where(result_exports.c.task_id == task_id)
            .order_by(
                result_exports.c.confirmed_at.desc(), result_exports.c.id.desc()
            )
        ).all()
        return [_row_to_export(row, _read_files_on(conn, row.id)) for row in rows]


def exports_by_result(engine: Engine) -> dict[str, list[ExportRecord]]:
    """Every export in the database, grouped by the revision it delivered.

    One query for the whole fleet projection, in the same shape
    `consumers_by_result` uses, so a snapshot does not fan out per revision.
    """
    grouped: dict[str, list[ExportRecord]] = {}
    with engine.connect() as conn:
        rows = conn.execute(
            result_exports.select().order_by(
                result_exports.c.confirmed_at.desc(), result_exports.c.id.desc()
            )
        ).all()
        files: dict[str, list[ExportFileRecord]] = {}
        for file_row in conn.execute(
            result_export_files.select().order_by(result_export_files.c.seq)
        ).all():
            files.setdefault(file_row.export_id, []).append(_row_to_file(file_row))
    for row in rows:
        grouped.setdefault(row.result_id, []).append(
            _row_to_export(row, files.get(row.id, []))
        )
    return grouped


def find_replay(engine: Engine, task_id: int, request_id: str) -> ExportRecord | None:
    """The operation a repeated request id already started, if any.

    Looked up *before* the filesystem is re-observed. A completed export has
    changed the very destinations a fresh preview would classify, so re-deriving
    an approval first would answer a retry with "the checkout changed" — which
    is true, and is the wrong answer to "did my request go through?".
    """
    with engine.connect() as conn:
        row = conn.execute(
            result_exports.select()
            .where(result_exports.c.task_id == task_id)
            .where(result_exports.c.request_id == request_id)
        ).first()
        if row is None:
            return None
        return _row_to_export(row, _read_files_on(conn, row.id))


def export_payload(record: ExportRecord) -> dict[str, Any]:
    """One export's wire shape, shared by REST and the result projection.

    Metadata only. Destination text, diffs, and retained content are fetched
    for the export an operator selected and are never broadcast to every
    connected client — a conflict diff can contain the contents of the
    operator's own checkout.
    """
    return {
        "id": record.id,
        "task_id": record.task_id,
        "result_id": record.result_id,
        "manifest_id": record.manifest_id,
        "state": record.state,
        "error": record.error,
        "project_name": record.project_name,
        "checkout_path": record.checkout_path,
        "prefix": record.prefix,
        "selection": list(record.selection),
        "actor": record.actor,
        "confirmed_at": record.confirmed_at,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "acknowledged_at": record.acknowledged_at,
        "acknowledged_by": record.acknowledged_by,
        "staging_error": record.staging_error,
        "created_directories": list(record.created_directories),
        "files": [
            {
                "path": entry.manifest_path,
                "destination": entry.destination,
                "classification": entry.classification,
                "length": entry.expected_length,
                "sha256": entry.expected_sha256,
                "outcome": entry.outcome,
                "error": entry.error,
            }
            for entry in record.files
        ],
        "created_count": sum(
            1 for entry in record.files if entry.outcome == OUTCOME_CREATED
        ),
        "identical_count": sum(
            1 for entry in record.files if entry.outcome == OUTCOME_IDENTICAL
        ),
        "unknown_count": sum(
            1 for entry in record.files if entry.outcome == OUTCOME_UNKNOWN
        ),
        "incomplete_count": sum(
            1
            for entry in record.files
            if entry.outcome in (OUTCOME_PENDING, OUTCOME_NOT_INSTALLED)
        ),
    }


def list_unsettled_exports(engine: Engine) -> list[ExportRecord]:
    """Every export a stopped daemon left `running`, oldest first.

    Reconciliation reads these before any new export is admitted, so a client
    can never see a `running` row belonging to a process that is gone.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            result_exports.select()
            .where(result_exports.c.state == STATE_RUNNING)
            .order_by(result_exports.c.confirmed_at)
        ).all()
        return [_row_to_export(row, _read_files_on(conn, row.id)) for row in rows]


# --- Admission --------------------------------------------------------------


@dataclass(frozen=True)
class ApprovedDestination:
    """One destination the operator approved, as the preview classified it."""

    manifest_path: str
    destination: str
    classification: str
    expected_length: int
    expected_sha256: str
    before: dict[str, Any] | None


def preview_token(document: Mapping[str, Any]) -> str:
    """The approval identity: a digest of the canonical preview document.

    Recomputed by the daemon at confirmation from a fresh observation, so a
    client cannot supply a verdict, and any change to the revision, selection,
    prefix, root, or a relevant destination yields a different token and a
    refusal.
    """
    import hashlib

    return hashlib.sha256(
        canonical_json(document).encode("utf-8")
    ).hexdigest()


def admit_export(
    engine: Engine,
    *,
    task_id: int,
    result_id: str,
    expected_manifest_id: str,
    request_id: str,
    selection: Sequence[str],
    selection_fingerprint: str,
    prefix: str,
    document: Mapping[str, Any],
    token: str,
    project_name: str,
    checkout_path: str,
    root_device: int,
    root_inode: int,
    destinations: Sequence[ApprovedDestination],
    actor: str = "operator",
) -> tuple[ExportRecord, bool]:
    """Reserve the root and record the approval, in one write reservation.

    Returns `(record, created)`. `created` is False for a replay: a repeated
    request id carrying the same confirmation answers with the original
    operation, including its failure, so a lost response never becomes a second
    export.

    Everything that could have changed since the filesystem was observed is
    rechecked here — the project's registration, the result's acceptance,
    identity and retained bytes, and whether any other export holds this root.
    The caller revalidates the *filesystem* observation again after this
    commits; a database reservation cannot prove anything about a directory.
    """
    now = _now_iso()
    export_id = new_export_id()
    with reserved_write(engine) as conn:
        prior = conn.execute(
            result_exports.select()
            .where(result_exports.c.task_id == task_id)
            .where(result_exports.c.request_id == request_id)
        ).first()
        if prior is not None:
            replay = _row_to_export(prior, _read_files_on(conn, prior.id))
            if (
                replay.result_id != result_id
                or replay.manifest_id != expected_manifest_id
                or replay.preview_token != token
            ):
                raise ExportRequestMismatchError(request_id, replay.id)
            return replay, False

        # The revision, rechecked on this reservation: accepted, ready,
        # readable, and still the manifest the operator reviewed.
        result = verify_attachable_on(
            conn, result_id, expected_manifest_id=expected_manifest_id
        )
        if result.task_id != task_id:
            raise ResultNotFoundError(result_id)
        verify_payload_on(conn, result)

        project = conn.execute(
            projects.select().where(projects.c.name == project_name)
        ).first()
        if project is None or project.checkout_path != checkout_path:
            raise ExportStateError(
                export_id,
                "cannot start: the project's registered checkout changed "
                "while the preview was being reviewed; preview again",
            )
        if project.setup_state != "ready":
            raise ExportStateError(
                export_id,
                f"cannot start: project {project_name!r} is "
                f"{project.setup_state}, not ready",
            )

        _assert_root_free_on(conn, root_device, root_inode)

        conn.execute(
            result_exports.insert().values(
                id=export_id,
                task_id=task_id,
                result_id=result_id,
                manifest_id=expected_manifest_id,
                request_id=request_id,
                selection_json=json.dumps(list(selection)),
                selection_fingerprint=selection_fingerprint,
                prefix=prefix,
                preview_token=token,
                preview_json=canonical_json(document),
                project_name=project_name,
                checkout_path=checkout_path,
                root_device=root_device,
                root_inode=root_inode,
                state=STATE_RUNNING,
                error=None,
                staging_name=staging_name_for(export_id),
                staging_device=None,
                staging_inode=None,
                staging_error=None,
                created_directories_json=None,
                actor=actor,
                confirmed_at=now,
                started_at=None,
                finished_at=None,
                acknowledged_at=None,
                acknowledged_by=None,
            )
        )
        # Every intended destination is written before any effect, so an
        # interrupted export leaves a record of what it was about to do.
        conn.execute(
            result_export_files.insert(),
            [
                {
                    "export_id": export_id,
                    "manifest_path": entry.manifest_path,
                    "seq": index,
                    "destination": entry.destination,
                    "classification": entry.classification,
                    "expected_length": entry.expected_length,
                    "expected_sha256": entry.expected_sha256,
                    "before_json": (
                        json.dumps(entry.before, sort_keys=True)
                        if entry.before is not None
                        else None
                    ),
                    "staged_device": None,
                    "staged_inode": None,
                    "outcome": OUTCOME_PENDING,
                    "observed_json": None,
                    "error": None,
                }
                for index, entry in enumerate(destinations)
            ],
        )
        bump_results_version(conn, task_id)
        record = _read_export_on(conn, export_id)
        assert record is not None
    return record, True


def _assert_root_free_on(conn: Connection, device: int, inode: int) -> None:
    row = conn.execute(
        result_exports.select()
        .where(result_exports.c.root_device == device)
        .where(result_exports.c.root_inode == inode)
        .where(result_exports.c.state.in_(ACTIVE_STATES))
        .order_by(result_exports.c.confirmed_at)
    ).first()
    if row is not None:
        raise CheckoutBusyError(row.id, row.checkout_path, row.state)


# --- Progress ---------------------------------------------------------------


def mark_started(engine: Engine, export_id: str) -> ExportRecord:
    """Record that filesystem work began. Separate from confirmation, so an
    export that never got that far is distinguishable from one that did."""
    return _update(engine, export_id, started_at=_now_iso())


def record_staging(
    engine: Engine, export_id: str, *, device: int, inode: int
) -> ExportRecord:
    """The staging directory's identity, written after it was created.

    Its *name* was journalled at admission. Recording the identity afterwards
    is what later lets cleanup and recovery prove they are removing the
    directory this export made, rather than something that merely shares a
    name.
    """
    return _update(engine, export_id, staging_device=device, staging_inode=inode)


def record_staged_file(
    engine: Engine, export_id: str, path: str, *, device: int, inode: int
) -> None:
    """Correlate one verified staged file with its destination intent.

    Written before the file is installed. After a crash this pair — plus the
    destination's identity — is what can establish that a rename happened
    before its journal update. Matching bytes alone cannot: anyone could have
    written the same text.
    """
    with reserved_write(engine) as conn:
        conn.execute(
            result_export_files.update()
            .where(result_export_files.c.export_id == export_id)
            .where(result_export_files.c.manifest_path == path)
            .values(staged_device=device, staged_inode=inode)
        )


def record_file_outcome(
    engine: Engine,
    export_id: str,
    path: str,
    *,
    outcome: str,
    observed: Mapping[str, Any] | None = None,
    error: str | None = None,
) -> None:
    with reserved_write(engine) as conn:
        conn.execute(
            result_export_files.update()
            .where(result_export_files.c.export_id == export_id)
            .where(result_export_files.c.manifest_path == path)
            .values(
                outcome=outcome,
                observed_json=(
                    json.dumps(dict(observed), sort_keys=True)
                    if observed is not None
                    else None
                ),
                error=error,
            )
        )


def record_created_directories(
    engine: Engine, export_id: str, directories: Sequence[str]
) -> None:
    """Journal the directories this export created in the operator's checkout.

    They are effects too. Nothing removes them on failure — an export is not
    atomic across files and does not pretend to be — so they are shown with the
    rest of what happened.
    """
    with reserved_write(engine) as conn:
        conn.execute(
            result_exports.update()
            .where(result_exports.c.id == export_id)
            .values(created_directories_json=json.dumps(sorted(set(directories))))
        )


def finish_export(
    engine: Engine,
    export_id: str,
    *,
    state: str,
    error: str | None = None,
    staging_error: str | None = None,
) -> ExportRecord:
    """Settle an operation into `completed`, `incomplete`, or `unresolved`.

    Completion is established from the recorded per-file outcomes, not claimed
    by the caller: `completed` requires every approved destination to have been
    created or verified already-identical, and any unknown outcome forces
    `unresolved`.
    """
    assert state in (STATE_COMPLETED, STATE_INCOMPLETE, STATE_UNRESOLVED)
    now = _now_iso()
    with reserved_write(engine) as conn:
        row = conn.execute(
            result_exports.select().where(result_exports.c.id == export_id)
        ).first()
        if row is None:
            raise ExportNotFoundError(export_id)
        files = _read_files_on(conn, export_id)
        settled = state
        if any(entry.outcome == OUTCOME_UNKNOWN for entry in files):
            settled = STATE_UNRESOLVED
        elif settled == STATE_COMPLETED and not all(
            entry.outcome in (OUTCOME_CREATED, OUTCOME_IDENTICAL)
            for entry in files
        ):
            settled = STATE_INCOMPLETE
        if staging_error is not None and settled == STATE_COMPLETED:
            # A staging directory Ompire could not clean up is left behind in
            # the operator's checkout. Reporting terminal success while an
            # unexplained daemon-owned directory sits there would be the same
            # silence the journal exists to prevent.
            settled = STATE_INCOMPLETE
        conn.execute(
            result_exports.update()
            .where(result_exports.c.id == export_id)
            .values(
                state=settled,
                error=error,
                staging_error=staging_error,
                finished_at=now,
            )
        )
        bump_results_version(conn, int(row.task_id))
        record = _read_export_on(conn, export_id)
        assert record is not None
    return record


def record_reconciliation(
    engine: Engine,
    export_id: str,
    *,
    state: str,
    error: str | None,
    outcomes: Mapping[str, tuple[str, dict[str, Any] | None, str | None]],
) -> ExportRecord:
    """Commit a read-only re-observation of an interrupted export.

    One transaction for the per-file classifications and the operation state,
    so a client never sees files reclassified under a state that has not moved
    yet. This writes nothing to the filesystem and never retries an effect.
    """
    assert state in (STATE_COMPLETED, STATE_INCOMPLETE, STATE_UNRESOLVED)
    now = _now_iso()
    with reserved_write(engine) as conn:
        row = conn.execute(
            result_exports.select().where(result_exports.c.id == export_id)
        ).first()
        if row is None:
            raise ExportNotFoundError(export_id)
        for path, (outcome, observed, detail) in outcomes.items():
            conn.execute(
                result_export_files.update()
                .where(result_export_files.c.export_id == export_id)
                .where(result_export_files.c.manifest_path == path)
                .values(
                    outcome=outcome,
                    observed_json=(
                        json.dumps(dict(observed), sort_keys=True)
                        if observed is not None
                        else None
                    ),
                    error=detail,
                )
            )
        conn.execute(
            result_exports.update()
            .where(result_exports.c.id == export_id)
            .values(state=state, error=error, finished_at=now)
        )
        bump_results_version(conn, int(row.task_id))
        record = _read_export_on(conn, export_id)
        assert record is not None
    return record


def acknowledge_export(
    engine: Engine, export_id: str, *, expected_version: int, actor: str = "operator"
) -> ExportRecord:
    """Close an unresolved export without claiming its unknowns resolved.

    The operation becomes `incomplete` — the honest statement that not all of
    the approved set is known delivered — and every per-file outcome is left
    exactly as it is, `unknown` included. It touches no file.
    """
    now = _now_iso()
    with reserved_write(engine) as conn:
        row = conn.execute(
            result_exports.select().where(result_exports.c.id == export_id)
        ).first()
        if row is None:
            raise ExportNotFoundError(export_id)
        if row.state != STATE_UNRESOLVED:
            raise ExportStateError(
                export_id,
                f"is {row.state}, and only an unresolved export can be "
                "acknowledged",
            )
        current = conn.execute(
            select(tasks.c.results_version).where(tasks.c.id == row.task_id)
        ).first()
        actual = int(current.results_version) if current is not None else 0
        if actual != expected_version:
            raise StaleRevisionError(export_id, str(expected_version), str(actual))
        conn.execute(
            result_exports.update()
            .where(result_exports.c.id == export_id)
            .values(
                state=STATE_INCOMPLETE,
                acknowledged_at=now,
                acknowledged_by=actor,
            )
        )
        bump_results_version(conn, int(row.task_id))
        record = _read_export_on(conn, export_id)
        assert record is not None
    return record


def _update(engine: Engine, export_id: str, **values: Any) -> ExportRecord:
    with reserved_write(engine) as conn:
        row = conn.execute(
            result_exports.select().where(result_exports.c.id == export_id)
        ).first()
        if row is None:
            raise ExportNotFoundError(export_id)
        conn.execute(
            result_exports.update()
            .where(result_exports.c.id == export_id)
            .values(**values)
        )
        record = _read_export_on(conn, export_id)
        assert record is not None
    return record


# --- Guards -----------------------------------------------------------------


def assert_result_exports_settled_on(conn: Connection, result_id: str) -> None:
    """Refuse a result purge while an export of it is unfinished.

    The protection is *temporary* by design: a completed export is a delivered
    copy, not a perpetual consumer reference, so it releases the revision. What
    it must not do is let the bytes disappear while an operation is still
    reading them or while nobody knows what that operation did.
    """
    rows = conn.execute(
        select(result_exports.c.id)
        .where(result_exports.c.result_id == result_id)
        .where(result_exports.c.state.in_(ACTIVE_STATES))
        .order_by(result_exports.c.confirmed_at)
    ).all()
    if rows:
        raise ExportsActiveError(
            f"result {result_id}", [row.id for row in rows]
        )


def assert_task_exports_settled_on(conn: Connection, task_id: int) -> None:
    """Refuse a task purge while any of its exports is unfinished.

    Decided before any of the task's history is deleted, on the caller's own
    reservation, exactly like the retained-result refusal beside it.
    """
    rows = conn.execute(
        select(result_exports.c.id)
        .where(result_exports.c.task_id == task_id)
        .where(result_exports.c.state.in_(ACTIVE_STATES))
        .order_by(result_exports.c.confirmed_at)
    ).all()
    if rows:
        raise ExportsActiveError(f"task {task_id}", [row.id for row in rows])


def assert_project_exports_settled_on(conn: Connection, project_name: str) -> None:
    """Refuse repointing a project's checkout while an export owns the old one.

    Not a claim that the directory cannot move on disk — nothing in SQLite can
    promise that. It stops Ompire's own registration from being changed out
    from under an operation that is mid-flight or unexplained.
    """
    rows = conn.execute(
        select(result_exports.c.id)
        .where(result_exports.c.project_name == project_name)
        .where(result_exports.c.state.in_(ACTIVE_STATES))
        .order_by(result_exports.c.confirmed_at)
    ).all()
    if rows:
        raise ExportsActiveError(
            f"project {project_name!r}", [row.id for row in rows]
        )


def delete_task_exports(conn: Connection, task_id: int) -> None:
    """Delete a task's terminal export history with the rest of its record.

    Only reachable once `assert_task_exports_settled_on` has passed. Foreign-key
    cascades are not enabled on these connections, so both deletions are
    written explicitly. The exported *copies* in the operator's checkout are
    untouched: they are ordinary files in a directory Ompire does not own.
    """
    ids = [
        row.id
        for row in conn.execute(
            select(result_exports.c.id).where(result_exports.c.task_id == task_id)
        ).all()
    ]
    if not ids:
        return
    conn.execute(
        result_export_files.delete().where(
            result_export_files.c.export_id.in_(ids)
        )
    )
    conn.execute(result_exports.delete().where(result_exports.c.task_id == task_id))
