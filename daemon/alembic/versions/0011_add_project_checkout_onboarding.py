"""add project checkout onboarding columns

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-28

Project onboarding (add-project-adopt-or-clone-onboarding; ADR-0022): a
project now records whether Ompire adopted the operator's checkout or created
it, which remote to fetch in that checkout, and whether setup finished.

Existing rows backfill to `adopted` / `origin` / `ready` / NULL. That is the
only honest reading of a row written before this revision: the operator
supplied or derived the path themselves, spawn already assumed `origin`, and
nothing on disk is read or written here to decide otherwise.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: Union[str, Sequence[str], None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "projects",
        sa.Column(
            "checkout_mode", sa.String(), nullable=False, server_default="adopted"
        ),
    )
    op.add_column(
        "projects",
        sa.Column("fetch_remote", sa.String(), nullable=False, server_default="origin"),
    )
    op.add_column(
        "projects",
        sa.Column("setup_state", sa.String(), nullable=False, server_default="ready"),
    )
    op.add_column("projects", sa.Column("setup_error", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("projects", "setup_error")
    op.drop_column("projects", "setup_state")
    op.drop_column("projects", "fetch_remote")
    op.drop_column("projects", "checkout_mode")
