"""export retained results into project checkouts

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-09

Adds the durable journal behind explicit checkout export (ADR-0036): one
`result_exports` row per approved operation, and one `result_export_files` row
per approved destination.

Purely additive, and deliberately empty of invention: no historical export is
backfilled, because none happened. A result retained before this migration has
no export history, which is the fact — not an unknown outcome to reconcile.

The partial unique index on `(root_device, root_inode)` is the durable root
reservation. It is what stops two project registrations that alias the same
directory from installing into it at once, and what keeps an unresolved export
holding its root until an operator reconciles or acknowledges it.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0022"
down_revision: Union[str, Sequence[str], None] = "0021"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "result_exports",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("result_id", sa.String(), nullable=False),
        sa.Column("manifest_id", sa.String(), nullable=False),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("selection_json", sa.Text(), nullable=False),
        sa.Column("selection_fingerprint", sa.String(), nullable=False),
        sa.Column("prefix", sa.String(), nullable=False),
        sa.Column("preview_token", sa.String(), nullable=False),
        sa.Column("preview_json", sa.Text(), nullable=False),
        sa.Column("project_name", sa.String(), nullable=False),
        sa.Column("checkout_path", sa.String(), nullable=False),
        sa.Column("root_device", sa.Integer(), nullable=False),
        sa.Column("root_inode", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("staging_name", sa.String(), nullable=False),
        sa.Column("staging_device", sa.Integer(), nullable=True),
        sa.Column("staging_inode", sa.Integer(), nullable=True),
        sa.Column("staging_error", sa.Text(), nullable=True),
        sa.Column("created_directories_json", sa.Text(), nullable=True),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("confirmed_at", sa.String(), nullable=False),
        sa.Column("started_at", sa.String(), nullable=True),
        sa.Column("finished_at", sa.String(), nullable=True),
        sa.Column("acknowledged_at", sa.String(), nullable=True),
        sa.Column("acknowledged_by", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.ForeignKeyConstraint(["result_id"], ["task_results.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_result_exports_task", "result_exports", ["task_id", "confirmed_at"]
    )
    op.create_index("ix_result_exports_result", "result_exports", ["result_id"])
    op.create_index(
        "uq_result_exports_request",
        "result_exports",
        ["task_id", "request_id"],
        unique=True,
    )
    op.create_index(
        "uq_result_exports_active_root",
        "result_exports",
        ["root_device", "root_inode"],
        unique=True,
        sqlite_where=sa.text("state IN ('running', 'unresolved')"),
    )

    op.create_table(
        "result_export_files",
        sa.Column("export_id", sa.String(), nullable=False),
        sa.Column("manifest_path", sa.String(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("destination", sa.String(), nullable=False),
        sa.Column("classification", sa.String(), nullable=False),
        sa.Column("expected_length", sa.Integer(), nullable=False),
        sa.Column("expected_sha256", sa.String(), nullable=False),
        sa.Column("before_json", sa.Text(), nullable=True),
        sa.Column("staged_device", sa.Integer(), nullable=True),
        sa.Column("staged_inode", sa.Integer(), nullable=True),
        sa.Column("outcome", sa.String(), nullable=False),
        sa.Column("observed_json", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["export_id"], ["result_exports.id"]),
        sa.PrimaryKeyConstraint("export_id", "manifest_path"),
    )
    op.create_index(
        "ix_result_export_files_export",
        "result_export_files",
        ["export_id", "seq"],
    )


def downgrade() -> None:
    """Downgrade schema.

    Drops the export journal. The files an export installed are ordinary
    checkout files and are left exactly where they are — a downgrade removes
    Ompire's record of having delivered them, not the delivery.
    """
    op.drop_index(
        "ix_result_export_files_export", table_name="result_export_files"
    )
    op.drop_table("result_export_files")
    op.drop_index("uq_result_exports_active_root", table_name="result_exports")
    op.drop_index("uq_result_exports_request", table_name="result_exports")
    op.drop_index("ix_result_exports_result", table_name="result_exports")
    op.drop_index("ix_result_exports_task", table_name="result_exports")
    op.drop_table("result_exports")
