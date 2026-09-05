"""add model profiles and the optional project default reference

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-05

Global model profiles (workflow-first-model-profiles / ADR-0025): a named,
reusable map of the four model roles to concrete model + thinking pairs, plus
an optional per-project reference to one.

Purely additive. Existing projects backfill to NULL — no default — which is
the only honest reading of a row written before profiles existed: no template,
provider credential, host omp setting, or project name says which profile its
operator would have picked. Nothing about templates, tasks, or settings is
transformed, and task execution is unaffected by this revision.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: Union[str, Sequence[str], None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Profiles first: the project column references them.
    op.create_table(
        "model_profiles",
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("roles_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("name"),
    )
    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(
            sa.Column("default_model_profile", sa.String(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_projects_default_model_profile",
            "model_profiles",
            ["default_model_profile"],
            ["name"],
        )
    op.create_index(
        "ix_projects_default_model_profile", "projects", ["default_model_profile"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Reverse order: drop the reference before what it points at.
    op.drop_index("ix_projects_default_model_profile", table_name="projects")
    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_constraint(
            "fk_projects_default_model_profile", type_="foreignkey"
        )
        batch_op.drop_column("default_model_profile")
    op.drop_table("model_profiles")
