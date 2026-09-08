"""bind trusted delivery to retained candidates and write-ahead action intent

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-07

Publishing stops being one transient in-memory operation (ADR-0032). Four new
tables hold what a delivery has to survive a restart with: the protected
candidate a review graded and a signature covers, the operator's authorization
and its selected ending, one write-ahead row per privileged action attempt, and
the ordered authorization/reconciliation decisions made about them.

`reviews` and `review_iterations` gain a nullable `candidate_id`. Nullable is
the whole migration story for existing history: a review recorded before content
binding stays exactly as it was and stays readable, and nothing here guesses
which tree it graded. A new delivery for such a task needs a fresh review, which
is a visible refusal rather than an invented binding.

Nothing existing is rewritten. `tasks.pr_url` keeps every historical publication
fact, and no successful delivery attempt is fabricated for it: a task that
shipped before this migration has a PR and no delivery rows, and the projection
says exactly that.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0018"
down_revision: Union[str, Sequence[str], None] = "0017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "delivery_candidates",
        sa.Column("candidate_id", sa.String(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("base_branch", sa.String(), nullable=False),
        sa.Column("base_commit", sa.String(), nullable=False),
        sa.Column("original_head", sa.String(), nullable=False),
        sa.Column("tree_id", sa.String(), nullable=False),
        sa.Column("source_commits_json", sa.Text(), nullable=False),
        sa.Column("dirty", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("storage_path", sa.String(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("candidate_id"),
    )
    op.create_index(
        "ix_delivery_candidates_task", "delivery_candidates", ["task_id"]
    )

    op.create_table(
        "deliveries",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("workflow_revision", sa.String(), nullable=True),
        sa.Column("candidate_id", sa.String(), nullable=True),
        sa.Column("review_candidate_id", sa.String(), nullable=True),
        sa.Column("mode", sa.String(), nullable=True),
        sa.Column("ending", sa.String(), nullable=True),
        sa.Column("commit_message", sa.Text(), nullable=True),
        sa.Column("pr_title", sa.Text(), nullable=True),
        sa.Column("pr_body", sa.Text(), nullable=True),
        sa.Column("routing_json", sa.Text(), nullable=True),
        sa.Column("identity_json", sa.Text(), nullable=True),
        sa.Column("authorized_at", sa.String(), nullable=True),
        sa.Column("authorized_by", sa.String(), nullable=True),
        sa.Column("request_key", sa.String(), nullable=True),
        sa.Column("input_fingerprint", sa.String(), nullable=True),
        sa.Column("draft_json", sa.Text(), nullable=True),
        sa.Column("disposition", sa.String(), nullable=False),
        sa.Column("blocked_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_deliveries_task", "deliveries", ["task_id"])
    # Partial unique index: an exact replay of a request key returns the same
    # delivery, while the many rows that never carried one do not collide.
    op.create_index(
        "uq_deliveries_request_key",
        "deliveries",
        ["task_id", "request_key"],
        unique=True,
        sqlite_where=sa.text("request_key IS NOT NULL"),
    )

    op.create_table(
        "delivery_actions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("delivery_id", sa.Integer(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("request_key", sa.String(), nullable=False),
        sa.Column("input_fingerprint", sa.String(), nullable=False),
        sa.Column("phase", sa.String(), nullable=False),
        sa.Column("expected_json", sa.Text(), nullable=True),
        sa.Column("progress_json", sa.Text(), nullable=True),
        sa.Column("identity_json", sa.Text(), nullable=True),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["delivery_id"], ["deliveries.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_delivery_actions_delivery", "delivery_actions", ["delivery_id", "seq"]
    )

    op.create_table(
        "delivery_decisions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("delivery_id", sa.Integer(), nullable=False),
        sa.Column("action_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("detail_json", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["delivery_id"], ["deliveries.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_delivery_decisions_delivery", "delivery_decisions", ["delivery_id", "id"]
    )

    op.add_column("reviews", sa.Column("candidate_id", sa.String(), nullable=True))
    op.add_column(
        "review_iterations", sa.Column("candidate_id", sa.String(), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema.

    Dropping the delivery tables loses every authorization record, action
    attempt, and reconciliation decision. Completed publications are unaffected
    — the PR URL and review history are elsewhere — but an *unresolved* effect
    loses the evidence that said it was unresolved, and a downgraded daemon
    would offer publishing again with no journal to check first. The candidate
    staging repositories under the data directory are left on disk rather than
    deleted here; a migration must not remove the only remaining copy of what a
    signature covered.
    """
    op.drop_column("review_iterations", "candidate_id")
    op.drop_column("reviews", "candidate_id")
    op.drop_index(
        "ix_delivery_decisions_delivery", table_name="delivery_decisions"
    )
    op.drop_table("delivery_decisions")
    op.drop_index("ix_delivery_actions_delivery", table_name="delivery_actions")
    op.drop_table("delivery_actions")
    op.drop_index("uq_deliveries_request_key", table_name="deliveries")
    op.drop_index("ix_deliveries_task", table_name="deliveries")
    op.drop_table("deliveries")
    op.drop_index("ix_delivery_candidates_task", table_name="delivery_candidates")
    op.drop_table("delivery_candidates")
