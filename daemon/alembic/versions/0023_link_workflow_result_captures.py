"""link durable result captures to workflow attempts

Revision ID: 0023
Revises: 0022
Create Date: 2026-09-09

Workflow-owned capture records its producing attempt before reading the
workspace. Existing manual captures retain NULL linkage: no historical capture
is guessed to have been produced by a recent workflow step.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0023"
down_revision: Union[str, Sequence[str], None] = "0022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("task_results", sa.Column("workflow_seq", sa.Integer(), nullable=True))
    op.add_column(
        "task_results", sa.Column("workflow_provenance_json", sa.Text(), nullable=True)
    )
    op.create_index(
        "uq_task_results_workflow_seq",
        "task_results",
        ["task_id", "workflow_seq"],
        unique=True,
        sqlite_where=sa.text("workflow_seq IS NOT NULL"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("uq_task_results_workflow_seq", table_name="task_results")
    op.drop_column("task_results", "workflow_provenance_json")
    op.drop_column("task_results", "workflow_seq")
