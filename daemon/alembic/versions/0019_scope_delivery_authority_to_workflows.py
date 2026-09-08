"""scope trusted delivery authority to the workflow run that granted it

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-08

Format 3 makes review and publication *steps*, so the durable records they
produce have to say which run step they belong to. Every link added here is
nullable, and every existing row keeps its NULL: a review an operator started
by hand and a delivery they authorized through the old Ship page really were
not produced by a workflow decision, and backfilling a plausible attempt link
would manufacture exactly the provenance this change exists to establish.

`review_iterations` also gains the reviewer's actual report. Until now an
iteration retained a *count* of comment blocks, which is enough to display and
nowhere near enough to hand to a correcting agent. `findings_state` says what
was retained — the whole report, an empty approval, a truncated capture, or
nothing at all — so an automatic correction can refuse to run against a partial
report instead of treating it as the reviewer's complete opinion.

`delivery_authority_boundary` records, once, the highest delivery and action id
that existed before this upgrade. A NULL workflow link below the boundary is a
genuine pre-upgrade authorization that may still be *continued* under its
original grant; a NULL above it is a row mid-write, and grants nothing. Without
the boundary the two are indistinguishable, and a new delivery could pass
itself off as historical simply by leaving its links unset.
"""
from datetime import UTC, datetime
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0019"
down_revision: Union[str, Sequence[str], None] = "0018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("reviews", sa.Column("workflow_seq", sa.Integer(), nullable=True))
    op.add_column(
        "review_iterations", sa.Column("workflow_seq", sa.Integer(), nullable=True)
    )
    op.add_column("review_iterations", sa.Column("findings", sa.Text(), nullable=True))
    op.add_column(
        "review_iterations", sa.Column("findings_state", sa.String(), nullable=True)
    )

    op.add_column(
        "deliveries", sa.Column("workflow_gate_seq", sa.Integer(), nullable=True)
    )
    op.add_column(
        "deliveries", sa.Column("workflow_choice_id", sa.String(), nullable=True)
    )
    op.add_column("deliveries", sa.Column("review_seq", sa.Integer(), nullable=True))

    op.add_column(
        "delivery_actions", sa.Column("workflow_seq", sa.Integer(), nullable=True)
    )
    op.create_index(
        "uq_delivery_actions_workflow_seq",
        "delivery_actions",
        ["delivery_id", "workflow_seq"],
        unique=True,
        sqlite_where=sa.text("workflow_seq IS NOT NULL AND phase != 'failed'"),
    )

    boundary = op.create_table(
        "delivery_authority_boundary",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("max_delivery_id", sa.Integer(), nullable=False),
        sa.Column("max_action_id", sa.Integer(), nullable=False),
        sa.Column("recorded_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    connection = op.get_bind()
    max_delivery = (
        connection.execute(sa.text("SELECT MAX(id) FROM deliveries")).scalar() or 0
    )
    max_action = (
        connection.execute(sa.text("SELECT MAX(id) FROM delivery_actions")).scalar()
        or 0
    )
    op.bulk_insert(
        boundary,
        [
            {
                "id": 1,
                "max_delivery_id": int(max_delivery),
                "max_action_id": int(max_action),
                "recorded_at": datetime.now(UTC).isoformat(),
            }
        ],
    )


def downgrade() -> None:
    """Downgrade schema.

    Dropping the boundary is the consequential part: a downgraded daemon can no
    longer tell a genuine pre-upgrade authorization from a row whose links were
    simply never written, so it treats them alike. The review reports and the
    run/action links go with it; the deliveries, actions, and decisions
    themselves are untouched, and no publication fact is lost.
    """
    op.drop_table("delivery_authority_boundary")
    op.drop_index("uq_delivery_actions_workflow_seq", table_name="delivery_actions")
    op.drop_column("delivery_actions", "workflow_seq")
    op.drop_column("deliveries", "review_seq")
    op.drop_column("deliveries", "workflow_choice_id")
    op.drop_column("deliveries", "workflow_gate_seq")
    op.drop_column("review_iterations", "findings_state")
    op.drop_column("review_iterations", "findings")
    op.drop_column("review_iterations", "workflow_seq")
    op.drop_column("reviews", "workflow_seq")
