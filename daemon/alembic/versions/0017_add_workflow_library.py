"""add the operator-owned workflow library

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-07

Workflow definitions stop being packaged-only (ADR-0031). One new table,
`workflow_library`, holds the mutable part an operator now owns: which
procedures exist, the raw editable text of each, and which retained revision a
*new* launch of that name would pin.

The table is created empty. Nothing is copied out of `workflow_revisions`, and
no entry is invented for the built-ins: the daemon synchronizes its packaged
entries at startup, from the definitions the running package actually ships.
A migration cannot know what the *next* start will ship, and guessing would
file a built-in under a revision this package never contained.

Nothing existing is touched. `workflow_revisions` stays append-only, and every
task keeps the revision it pinned — the whole point of separating the mutable
selection from the retained document is that editing the library cannot reach
an accepted task.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0017"
down_revision: Union[str, Sequence[str], None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "workflow_library",
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("origin", sa.String(), nullable=False),
        sa.Column("draft_yaml", sa.Text(), nullable=True),
        sa.Column("current_revision", sa.String(), nullable=True),
        sa.Column("archived", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(
            ["current_revision"],
            ["workflow_revisions.revision"],
            name="fk_workflow_library_revision",
        ),
        sa.PrimaryKeyConstraint("name"),
    )
    op.create_index(
        "ix_workflow_library_current_revision",
        "workflow_library",
        ["current_revision"],
    )


def downgrade() -> None:
    """Downgrade schema.

    Dropping the library loses every custom entry, its draft text, and its
    current selection. The retained revisions those entries pointed at survive
    in `workflow_revisions`, so an accepted task stays readable and runnable;
    what a downgraded daemon cannot do is launch a custom workflow by name,
    because a packaged-only catalog has no such name.
    """
    op.drop_index("ix_workflow_library_current_revision", table_name="workflow_library")
    op.drop_table("workflow_library")
