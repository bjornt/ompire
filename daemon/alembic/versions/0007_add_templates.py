"""add templates table, tasks.template_name; move spawn defaults off projects

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-14

SPEC Decision 6: templates own base_branch/branch_pattern. One template is
seeded per existing project (named after the project, inheriting its spawn
defaults), then the project columns are dropped. Existing task rows keep
template_name NULL — they predate templates.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0007'
down_revision: Union[str, Sequence[str], None] = '0006'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'templates',
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('project_name', sa.String(), nullable=False),
        sa.Column('base_branch', sa.String(), server_default='main', nullable=False),
        sa.Column('branch_pattern', sa.String(), nullable=False),
        sa.Column('workflow', sa.String(), server_default='single-step', nullable=False),
        sa.Column('workshop_additions', sa.String(), server_default='project', nullable=False),
        sa.Column('model', sa.String(), nullable=True),
        sa.Column('thinking', sa.String(), nullable=True),
        sa.Column('preamble', sa.Text(), server_default='', nullable=False),
        sa.Column('created_at', sa.String(), nullable=False),
        sa.Column('updated_at', sa.String(), nullable=False),
        sa.ForeignKeyConstraint(['project_name'], ['projects.name']),
        sa.PrimaryKeyConstraint('name'),
    )
    with op.batch_alter_table('tasks') as batch_op:
        batch_op.add_column(sa.Column('template_name', sa.String(), nullable=True))

    # Seed one template per project, inheriting its spawn defaults. Project
    # names are valid slugs already, so template names never collide.
    connection = op.get_bind()
    now = connection.execute(sa.text("SELECT datetime('now')")).scalar_one()
    connection.execute(
        sa.text(
            "INSERT INTO templates (name, project_name, base_branch, branch_pattern, "
            "workflow, workshop_additions, model, thinking, preamble, created_at, updated_at) "
            "SELECT name, name, base_branch, branch_pattern, 'single-step', 'project', "
            "NULL, NULL, '', :now, :now FROM projects"
        ),
        {"now": now},
    )

    # SQLite can't drop columns; batch mode rebuilds the table.
    with op.batch_alter_table('projects') as batch_op:
        batch_op.drop_column('branch_pattern')
        batch_op.drop_column('base_branch')


def downgrade() -> None:
    """Downgrade schema. Restores the two project columns; template data (and
    any edits made since the upgrade) is dropped with the templates table —
    accepted per design D-2 (single operator, pre-release dogfooding)."""
    with op.batch_alter_table('projects') as batch_op:
        batch_op.add_column(
            sa.Column('base_branch', sa.String(), server_default='main', nullable=False)
        )
        batch_op.add_column(
            sa.Column(
                'branch_pattern', sa.String(), server_default='ompire/<slug>', nullable=False
            )
        )
    with op.batch_alter_table('tasks') as batch_op:
        batch_op.drop_column('template_name')
    op.drop_table('templates')
