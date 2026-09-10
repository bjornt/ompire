"""Upgrade evidence and reconciliation decisions: Core queries against
`launch_migration_evidence` and `launch_reconciliations`.

Everything here is history and bookkeeping. Nothing in this module returns a
value that execution consumes: an evidence row says what *used* to be
configured, and a reconciliation row says the operator has since decided. A
launch reads project columns, a profile, and the task's own pinned inputs —
never these tables. Keeping that separation is what stops the migration's
candidates from quietly becoming a second launch preset (ADR-0026).

Evidence is append-only and is deliberately not deleted when a reconciliation
completes: the unselected candidates and distinct old preambles are the only
surviving record of what the templates held.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Connection, Engine

from ompire_daemon.db import launch_migration_evidence, launch_reconciliations

# What an evidence row is about.
SCOPE_PROJECT = "project"
SCOPE_TASK = "task"
SCOPE_DAEMON = "daemon"

# Evidence kinds written by migration 0013 and by startup initialization.
KIND_TEMPLATE = "template"
KIND_TASK_TEMPLATE = "task-template"
KIND_WORKSPACE_CONFLICT = "workspace-conflict"
KIND_MODEL_CANDIDATES = "model-candidates"
KIND_NEW_DEFAULTS = "new-defaults"
KIND_RETIRED_JUDGE_MODEL = "retired-judge-model"

# Evidence kinds written by migration 0015 (ADR-0028). The whole pre-upgrade
# execution-inputs document, and each retired engine auxiliary binding, kept
# verbatim so the old judge model stays inspectable without staying live.
KIND_LEGACY_EXECUTION_INPUTS = "legacy-execution-inputs"
KIND_RETIRED_AUXILIARY_BINDING = "retired-auxiliary-binding"

# Reconciliation kinds: one row per decision the operator has made.
DECISION_LAUNCH_CONFIG = "launch-config"
DECISION_NEW_DEFAULTS = "new-defaults"
DECISION_JUDGE_MODEL = "judge-model"
# The operator confirmed which retained revision a legacy task continues under.
DECISION_WORKFLOW_CONTINUATION = "workflow-continuation"


@dataclass(frozen=True)
class Evidence:
    id: int
    kind: str
    scope_kind: str
    scope: str
    source: str
    payload: Any
    recorded_at: str


@dataclass(frozen=True)
class Decision:
    scope_kind: str
    scope: str
    kind: str
    acknowledged_value: str | None
    decided_at: str


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_evidence(row) -> Evidence:
    return Evidence(
        id=row.id,
        kind=row.kind,
        scope_kind=row.scope_kind,
        scope=row.scope,
        source=row.source,
        payload=json.loads(row.payload_json),
        recorded_at=row.recorded_at,
    )


def record_evidence(
    conn: Connection,
    *,
    kind: str,
    scope_kind: str,
    scope: str,
    source: str,
    payload: Any,
) -> None:
    conn.execute(
        launch_migration_evidence.insert().values(
            kind=kind,
            scope_kind=scope_kind,
            scope=str(scope),
            source=source,
            payload_json=json.dumps(payload),
            recorded_at=_now_iso(),
        )
    )


def list_evidence(
    engine: Engine, scope_kind: str, scope: str, *, kind: str | None = None
) -> list[Evidence]:
    query = (
        launch_migration_evidence.select()
        .where(launch_migration_evidence.c.scope_kind == scope_kind)
        .where(launch_migration_evidence.c.scope == str(scope))
        .order_by(launch_migration_evidence.c.id)
    )
    if kind is not None:
        query = query.where(launch_migration_evidence.c.kind == kind)
    with engine.connect() as conn:
        rows = conn.execute(query).all()
    return [_row_to_evidence(row) for row in rows]


def list_evidence_conn(
    conn: Connection, scope_kind: str, scope: str, *, kind: str | None = None
) -> list[Evidence]:
    """Same read, inside an already-open transaction."""
    query = (
        launch_migration_evidence.select()
        .where(launch_migration_evidence.c.scope_kind == scope_kind)
        .where(launch_migration_evidence.c.scope == str(scope))
        .order_by(launch_migration_evidence.c.id)
    )
    if kind is not None:
        query = query.where(launch_migration_evidence.c.kind == kind)
    return [_row_to_evidence(row) for row in conn.execute(query).all()]


def evidence_fingerprint(rows: list[Evidence]) -> str:
    """A stable value naming exactly the evidence the operator reviewed.

    Confirmation carries it back, so a decision made against one set of
    candidates cannot be applied after new evidence appears (a changed retired
    setting, say). It is a comparison value, not a credential.
    """
    digest = hashlib.sha256()
    for row in rows:
        digest.update(
            json.dumps(
                [row.id, row.kind, row.source, row.payload], sort_keys=True
            ).encode("utf-8")
        )
        digest.update(b"\x00")
    return digest.hexdigest()[:32]


def get_decision(
    conn: Connection, scope_kind: str, scope: str, kind: str
) -> Decision | None:
    row = conn.execute(
        launch_reconciliations.select()
        .where(launch_reconciliations.c.scope_kind == scope_kind)
        .where(launch_reconciliations.c.scope == str(scope))
        .where(launch_reconciliations.c.kind == kind)
    ).first()
    if row is None:
        return None
    return Decision(
        scope_kind=row.scope_kind,
        scope=row.scope,
        kind=row.kind,
        acknowledged_value=row.acknowledged_value,
        decided_at=row.decided_at,
    )


def record_decision(
    conn: Connection,
    *,
    scope_kind: str,
    scope: str,
    kind: str,
    acknowledged_value: str | None = None,
) -> None:
    """Write (or replace) one decision. Replacement matters for the retired
    judge setting: acknowledging a *new* value supersedes the old
    acknowledgement rather than accumulating rows."""
    conn.execute(
        launch_reconciliations.delete()
        .where(launch_reconciliations.c.scope_kind == scope_kind)
        .where(launch_reconciliations.c.scope == str(scope))
        .where(launch_reconciliations.c.kind == kind)
    )
    conn.execute(
        launch_reconciliations.insert().values(
            scope_kind=scope_kind,
            scope=str(scope),
            kind=kind,
            acknowledged_value=acknowledged_value,
            decided_at=_now_iso(),
        )
    )
