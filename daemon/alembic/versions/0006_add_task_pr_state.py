"""add task pr_state and pr_merged_at

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-14

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0006'
down_revision: Union[str, Sequence[str], None] = '0005'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('tasks', sa.Column('pr_state', sa.String(), nullable=True))
    op.add_column('tasks', sa.Column('pr_merged_at', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('tasks', 'pr_merged_at')
    op.drop_column('tasks', 'pr_state')
