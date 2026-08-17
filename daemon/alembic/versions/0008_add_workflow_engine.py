"""workflow engine: task_sessions, workflow_step_records, tasks.workflow_*; drop tasks.session_id

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-17

Workflow-engine capability (design D-5): per-session omp identity moves off
the task row into `task_sessions`; workflow run state lands on `tasks`
(`workflow_name` denormalized from the template like `project_name`,
`workflow_status`/`workflow_step` NULL for legacy rows); step history lives
in `workflow_step_records`.

Backfill: live tasks with a recorded `session_id` get a `main` session row;
legacy live tasks (spawn-completed, `created`) are marked
`single-step`/`complete` with one `ok` `work` step record, so the engine
never re-drives them and recovery resumes exactly their `main` session.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0008'
down_revision: Union[str, Sequence[str], None] = '0007'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'task_sessions',
        sa.Column('task_id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('omp_session_id', sa.String(), nullable=True),
        sa.Column('spawned_at', sa.String(), nullable=False),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id']),
        sa.PrimaryKeyConstraint('task_id', 'name'),
    )
    op.create_table(
        'workflow_step_records',
        sa.Column('task_id', sa.Integer(), nullable=False),
        sa.Column('seq', sa.Integer(), nullable=False),
        sa.Column('step', sa.String(), nullable=False),
        sa.Column('kind', sa.String(), nullable=False),
        sa.Column('session', sa.String(), nullable=True),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('outcome_json', sa.Text(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('prompted_at', sa.String(), nullable=True),
        sa.Column('started_at', sa.String(), nullable=False),
        sa.Column('finished_at', sa.String(), nullable=True),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id']),
        sa.PrimaryKeyConstraint('task_id', 'seq'),
    )
    with op.batch_alter_table('tasks') as batch_op:
        batch_op.add_column(
            sa.Column('workflow_name', sa.String(), server_default='single-step', nullable=False)
        )
        batch_op.add_column(sa.Column('workflow_status', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('workflow_step', sa.String(), nullable=True))

    connection = op.get_bind()
    now = connection.execute(sa.text("SELECT datetime('now')")).scalar_one()

    # Per-session identity backfill: every live task's recorded session id
    # becomes session `main` (the only session single-step ever had).
    connection.execute(
        sa.text(
            "INSERT INTO task_sessions (task_id, name, omp_session_id, spawned_at) "
            "SELECT id, 'main', session_id, :now FROM tasks "
            "WHERE session_id IS NOT NULL AND state != 'archived'"
        ),
        {"now": now},
    )

    # Legacy live tasks: their prompt was already delivered pre-workflow, so
    # mark the run complete with one ok step record — the engine never
    # touches them and recovery falls out identical to before (resume `main`,
    # land idle). Failed/archived tasks keep NULL workflow_status.
    connection.execute(
        sa.text(
            "INSERT INTO workflow_step_records (task_id, seq, step, kind, session, status, "
            "outcome_json, error, started_at, finished_at) "
            "SELECT id, 1, 'work', 'agent', 'main', 'ok', NULL, NULL, "
            "spawn_completed_at, spawn_completed_at FROM tasks "
            "WHERE state = 'created' AND spawn_completed_at IS NOT NULL"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE tasks SET workflow_status = 'complete' "
            "WHERE state = 'created' AND spawn_completed_at IS NOT NULL"
        )
    )

    # SQLite can't drop columns; batch mode rebuilds the table.
    with op.batch_alter_table('tasks') as batch_op:
        batch_op.drop_column('session_id')


def downgrade() -> None:
    """Downgrade schema. Restores `tasks.session_id` from each task's `main`
    session row; non-`main` session identities and all step history are
    dropped with their tables — accepted data loss, matching 0007's
    precedent (single operator, pre-release dogfooding)."""
    with op.batch_alter_table('tasks') as batch_op:
        batch_op.add_column(sa.Column('session_id', sa.String(), nullable=True))
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE tasks SET session_id = ("
            "  SELECT omp_session_id FROM task_sessions "
            "  WHERE task_sessions.task_id = tasks.id AND task_sessions.name = 'main'"
            ")"
        )
    )
    with op.batch_alter_table('tasks') as batch_op:
        batch_op.drop_column('workflow_step')
        batch_op.drop_column('workflow_status')
        batch_op.drop_column('workflow_name')
    op.drop_table('workflow_step_records')
    op.drop_table('task_sessions')
