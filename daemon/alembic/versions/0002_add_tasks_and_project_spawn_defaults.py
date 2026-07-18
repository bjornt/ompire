"""add tasks table and project spawn defaults

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-18

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'projects',
        sa.Column('base_branch', sa.String(), nullable=False, server_default='main'),
    )
    op.add_column(
        'projects',
        sa.Column('branch_pattern', sa.String(), nullable=False, server_default='ompire/<slug>'),
    )
    op.create_table(
        'tasks',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('project_name', sa.String(), nullable=False),
        sa.Column('slug', sa.String(), nullable=False),
        sa.Column('branch', sa.String(), nullable=False),
        sa.Column('clone_path', sa.String(), nullable=False),
        sa.Column('state', sa.String(), nullable=False),
        sa.Column('prompt', sa.Text(), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('spawn_completed_at', sa.String(), nullable=True),
        sa.Column('created_at', sa.String(), nullable=False),
        sa.Column('updated_at', sa.String(), nullable=False),
        sa.ForeignKeyConstraint(['project_name'], ['projects.name']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        'uq_tasks_live_project_slug',
        'tasks',
        ['project_name', 'slug'],
        unique=True,
        sqlite_where=sa.text("state != 'archived'"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('uq_tasks_live_project_slug', table_name='tasks')
    op.drop_table('tasks')
    op.drop_column('projects', 'branch_pattern')
    op.drop_column('projects', 'base_branch')
