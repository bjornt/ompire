"""retain declarative workflow revisions and pin them to tasks

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-06

Workflow definitions stop being Python and start being retained documents
(ADR-0028). Three schema changes and one conversion, all bounded by what the
existing rows actually say:

- `workflow_revisions` retains each definition's canonical document under its
  content identity. This revision creates the table empty; the daemon fills it
  from its packaged definitions at startup. A migration must not invent a
  revision, because it has no way to know what the code it is upgrading *from*
  actually executed.

- `workflow_step_records.pause_json` gives an uncertainty pause somewhere to
  live that is not a gate's outcome. Existing rows get NULL: none of them was
  paused by an engine that could not yet pause.

- `tasks.execution_inputs_json` version 2 → 3. The version-3 document adds a
  `workflow_binding` and drops the engine's auxiliary judge consumer. The
  binding is explicitly **NULL** for every existing task: version 2 recorded a
  workflow *name*, and a name is not a definition. Filling it in from whatever
  ships today would claim the task had accepted a document nobody showed it.
  The operator confirms a continuation revision instead, per task, in the UI.

  The original version-2 document and each retired judge binding are copied
  verbatim into `launch_migration_evidence` first. That table is inert by
  construction — nothing reads it to execute anything — so the old model
  choice for the removed judge stays inspectable without staying live. The
  `judge` session rows, their applied policies, and all step history are left
  exactly as they are: they are what happened.
"""
import json
from datetime import UTC, datetime
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: Union[str, Sequence[str], None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_SCOPE_TASK = "task"
_KIND_LEGACY_INPUTS = "legacy-execution-inputs"
_KIND_RETIRED_AUXILIARY = "retired-auxiliary-binding"
_SOURCE_INPUTS_V2 = "execution-inputs-v2"
_JUDGE = "judge"


def _record_evidence(conn, now: str, *, kind: str, scope: str, source: str, payload) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO launch_migration_evidence "
            "(kind, scope_kind, scope, source, payload_json, recorded_at) "
            "VALUES (:kind, :scope_kind, :scope, :source, :payload, :now)"
        ),
        {
            "kind": kind,
            "scope_kind": _SCOPE_TASK,
            "scope": scope,
            "source": source,
            "payload": json.dumps(payload),
            "now": now,
        },
    )


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()
    now = datetime.now(UTC).isoformat()

    op.create_table(
        "workflow_revisions",
        sa.Column("revision", sa.String(), nullable=False),
        sa.Column("workflow_name", sa.String(), nullable=False),
        sa.Column("format", sa.Integer(), nullable=False),
        sa.Column("document_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("revision"),
    )
    op.create_index(
        "ix_workflow_revisions_workflow_name", "workflow_revisions", ["workflow_name"]
    )

    with op.batch_alter_table("workflow_step_records") as batch_op:
        batch_op.add_column(sa.Column("pause_json", sa.Text(), nullable=True))

    rows = conn.execute(
        sa.text(
            "SELECT id, execution_inputs_json FROM tasks "
            "WHERE execution_inputs_json IS NOT NULL"
        )
    ).all()
    for task_id, raw in rows:
        document = json.loads(raw)
        if document.get("version") != 2:
            # Not a shape this revision understands. Leaving it alone is the
            # honest move: a daemon that cannot read it refuses the task
            # explicitly rather than running it under guessed semantics.
            continue
        _record_evidence(
            conn,
            now,
            kind=_KIND_LEGACY_INPUTS,
            scope=str(task_id),
            source=_SOURCE_INPUTS_V2,
            payload=document,
        )
        for name, binding in sorted((document.get("auxiliary_bindings") or {}).items()):
            _record_evidence(
                conn,
                now,
                kind=_KIND_RETIRED_AUXILIARY,
                scope=str(task_id),
                source=f"auxiliary_bindings.{name}",
                payload={"consumer": name, "binding": binding},
            )
        document.pop("auxiliary_bindings", None)
        # NULL, deliberately: no pre-upgrade task ever accepted a retained
        # definition, and there is nothing to reconstruct one from.
        document["workflow_binding"] = None
        document["version"] = 3
        conn.execute(
            sa.text("UPDATE tasks SET execution_inputs_json = :doc WHERE id = :id"),
            {"doc": json.dumps(document), "id": task_id},
        )


def downgrade() -> None:
    """Downgrade schema.

    Version 3 goes back to version 2 by restoring each task's retired
    auxiliary bindings from the evidence rows this revision wrote. A task with
    no such evidence — one accepted *after* the upgrade, which never had a
    judge binding — cannot be expressed in version 2 at all, so it is left at
    version 3 for a version-2 daemon to refuse explicitly rather than run with
    a fabricated judge policy.

    The pinned workflow revision itself has no version-2 home. Dropping it is
    a real loss of the accepted definition, which is why this direction exists
    only as an escape hatch.
    """
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT id, execution_inputs_json FROM tasks "
            "WHERE execution_inputs_json IS NOT NULL"
        )
    ).all()
    for task_id, raw in rows:
        document = json.loads(raw)
        if document.get("version") != 3:
            continue
        evidence = conn.execute(
            sa.text(
                "SELECT source, payload_json FROM launch_migration_evidence "
                "WHERE kind = :kind AND scope_kind = :scope_kind AND scope = :scope "
                "ORDER BY id"
            ),
            {
                "kind": _KIND_RETIRED_AUXILIARY,
                "scope_kind": _SCOPE_TASK,
                "scope": str(task_id),
            },
        ).all()
        auxiliary = {}
        for _source, payload_json in evidence:
            payload = json.loads(payload_json)
            auxiliary[payload["consumer"]] = payload["binding"]
        if _JUDGE not in auxiliary:
            continue
        document["auxiliary_bindings"] = auxiliary
        document.pop("workflow_binding", None)
        document["version"] = 2
        conn.execute(
            sa.text("UPDATE tasks SET execution_inputs_json = :doc WHERE id = :id"),
            {"doc": json.dumps(document), "id": task_id},
        )

    with op.batch_alter_table("workflow_step_records") as batch_op:
        batch_op.drop_column("pause_json")
    op.drop_index("ix_workflow_revisions_workflow_name", table_name="workflow_revisions")
    op.drop_table("workflow_revisions")
