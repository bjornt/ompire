"""retain durable task results outside the disposable workspace

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-08

A task's useful output stops being whatever happens to still be lying in its
clone (ADR-0034). `task_results` holds one immutable capture — its canonical
manifest, the identity that manifest hashes to, its provenance, its acceptance
decision and its purge tombstone — and `task_result_files` holds the exact
retained bytes, one row per file, so a capture's payload and its `ready` state
commit or roll back together.

Nothing is invented for existing tasks. No row is created here: a task that ran
before results existed has none, and its Results panel says "no captured
results" rather than reconstructing a bundle from outcome text, from the
workspace, or from a step record that was never evidence of authorship.

`tasks.results_version` starts at 0 for every existing row. It is the ordering
key for the result projection alone, deliberately separate from `updated_at`:
capturing a result changes nothing about the task itself.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0020"
down_revision: Union[str, Sequence[str], None] = "0019"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "tasks",
        sa.Column(
            "results_version", sa.Integer(), nullable=False, server_default="0"
        ),
    )

    op.create_table(
        "task_results",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("selection_fingerprint", sa.String(), nullable=False),
        sa.Column("selection_json", sa.Text(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("unavailable_reason", sa.Text(), nullable=True),
        sa.Column("manifest_json", sa.Text(), nullable=True),
        sa.Column("manifest_id", sa.String(), nullable=True),
        sa.Column("content_id", sa.String(), nullable=True),
        sa.Column("predecessor_id", sa.String(), nullable=True),
        sa.Column("started_at", sa.String(), nullable=False),
        sa.Column("finished_at", sa.String(), nullable=True),
        sa.Column("accepted_at", sa.String(), nullable=True),
        sa.Column("accepted_by", sa.String(), nullable=True),
        sa.Column("purged_at", sa.String(), nullable=True),
        sa.Column("purged_by", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_task_results_task", "task_results", ["task_id", "started_at"]
    )
    # One capture per (task, request id). This is what makes a repeated
    # request a *replay* rather than a second bundle: the insert loses, and
    # the caller is answered with the operation that already exists.
    op.create_index(
        "uq_task_results_request",
        "task_results",
        ["task_id", "request_id"],
        unique=True,
    )

    op.create_table(
        "task_result_files",
        sa.Column("result_id", sa.String(), nullable=False),
        sa.Column("relative_path", sa.String(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.ForeignKeyConstraint(["result_id"], ["task_results.id"]),
        sa.PrimaryKeyConstraint("result_id", "relative_path"),
    )


def downgrade() -> None:
    """Downgrade schema.

    This drops retained result bytes, manifests, and the operator's acceptance
    and purge decisions. Nothing else in the database references them, so no
    task, review, or delivery history is affected — but a downgrade is a real
    loss of the only copy of an accepted exploration result, not a reversible
    schema tweak.
    """
    op.drop_table("task_result_files")
    op.drop_index("uq_task_results_request", table_name="task_results")
    op.drop_index("ix_task_results_task", table_name="task_results")
    op.drop_table("task_results")
    op.drop_column("tasks", "results_version")
