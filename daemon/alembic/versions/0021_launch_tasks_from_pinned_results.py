"""launch tasks from pinned result revisions

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-09

A launch can now pin accepted result revisions as immutable inputs (ADR-0026,
ADR-0035). Two things change durably.

`task_result_references` indexes which consumer task pinned which retained
revision. The execution-inputs document is still the execution contract; this
table exists so a result purge can be refused — and can name the consumers
holding it — without decoding every launch document in the database.

`tasks.execution_inputs_json` moves from version 3 to version 4. The upgrade is
deliberately *empty of invention*: an existing task gains an empty attachment
list, a null source commit, no base comparison, and no acknowledgement. A task
that predates pinned inputs keeps NULL — a launch nobody reviewed under this
daemon must not acquire a commit observation it never made.
"""
from typing import Sequence, Union

import json

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0021"
down_revision: Union[str, Sequence[str], None] = "0020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "task_result_references",
        sa.Column("consumer_task_id", sa.Integer(), nullable=False),
        sa.Column("result_id", sa.String(), nullable=False),
        sa.Column("producer_task_id", sa.Integer(), nullable=False),
        sa.Column("manifest_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["consumer_task_id"], ["tasks.id"]),
        sa.ForeignKeyConstraint(["result_id"], ["task_results.id"]),
        sa.PrimaryKeyConstraint("consumer_task_id", "result_id"),
    )
    op.create_index(
        "ix_task_result_references_result", "task_result_references", ["result_id"]
    )

    # Forward-migrate the pinned launch documents in place. Only version 3 is
    # rewritten: a NULL stays NULL, and a document written by some other
    # version is left exactly as it is for the reader to refuse honestly rather
    # than being half-understood here.
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT id, execution_inputs_json FROM tasks "
            "WHERE execution_inputs_json IS NOT NULL"
        )
    ).all()
    for row in rows:
        try:
            document = json.loads(row.execution_inputs_json)
        except (TypeError, ValueError):
            continue
        if not isinstance(document, dict) or document.get("version") != 3:
            continue
        document["version"] = 4
        # No attachments, and no invented observation of the commit this task
        # was actually built from: it was never recorded, and today's clone is
        # not evidence about a checkout resolved months ago.
        document["result_attachments"] = []
        document["source_commit"] = None
        document["base_comparisons"] = []
        document["acknowledged_base_difference"] = False
        connection.execute(
            sa.text(
                "UPDATE tasks SET execution_inputs_json = :document WHERE id = :id"
            ),
            {"document": json.dumps(document), "id": row.id},
        )


def downgrade() -> None:
    """Downgrade schema.

    Drops the reference index, so a result whose only protection was a
    consumer's pin becomes purgeable again, and rewrites version-4 documents
    back to version 3 by *dropping* their attachments. A task that was launched
    with handoff inputs would keep those files in its clone while its pinned
    inputs no longer record them — which is why this is a real downgrade, not a
    reversible tweak.
    """
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT id, execution_inputs_json FROM tasks "
            "WHERE execution_inputs_json IS NOT NULL"
        )
    ).all()
    for row in rows:
        try:
            document = json.loads(row.execution_inputs_json)
        except (TypeError, ValueError):
            continue
        if not isinstance(document, dict) or document.get("version") != 4:
            continue
        document["version"] = 3
        for key in (
            "result_attachments",
            "source_commit",
            "base_comparisons",
            "acknowledged_base_difference",
        ):
            document.pop(key, None)
        connection.execute(
            sa.text(
                "UPDATE tasks SET execution_inputs_json = :document WHERE id = :id"
            ),
            {"document": json.dumps(document), "id": row.id},
        )

    op.drop_index(
        "ix_task_result_references_result", table_name="task_result_references"
    )
    op.drop_table("task_result_references")
