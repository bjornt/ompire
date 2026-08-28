"""add reviews and review_iterations

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-28

Durable review history (review capability; ADR-0016's review slice): one
`reviews` row per task plus ordered `review_iterations` rows. Nothing is
backfilled — tasks that predate this revision legitimately have no review
history, and inventing one from git or PR state would fabricate provenance.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: Union[str, Sequence[str], None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "reviews",
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("process_started_at", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("task_id"),
    )
    op.create_table(
        "review_iterations",
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("comment_count", sa.Integer(), nullable=True),
        sa.Column("stderr", sa.Text(), nullable=True),
        sa.Column("recorded_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("task_id", "seq"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("review_iterations")
    op.drop_table("reviews")
