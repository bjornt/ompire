"""record attempt evidence bindings and a run's terminal work result

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-06

Format 2 needs two facts the schema has nowhere to put (ADR-0029, ADR-0030):

- `workflow_step_records.evidence_json` — which prior attempts this attempt
  bound when it opened. Without it, "the evidence this fix was given" is
  recomputed on every read, so a restart can answer with a record that did not
  exist when the attempt started.

- `tasks.workflow_result` — which declared ending a finished run reached.
  `workflow_status` says the run stopped; it cannot say whether the bug was
  validated, validated under an explicit no-reproduction exception, or
  abandoned without a fix.

Both are additive and nullable, and every existing row keeps NULL. NULL means
*not recorded*, and that is the truthful value here: no pre-upgrade attempt
froze a binding, and no pre-upgrade run declared a named ending. Filling
either in — an empty binding map, or a terminal result inferred from a
`complete` status — would manufacture history. A format-1 run that completed
is a format-1 run that completed; it never claimed more than that, and this
migration does not claim it on its behalf.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0016"
down_revision: Union[str, Sequence[str], None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("workflow_step_records") as batch_op:
        batch_op.add_column(sa.Column("evidence_json", sa.Text(), nullable=True))
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(sa.Column("workflow_result", sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema.

    Both columns drop cleanly: nothing outside format 2 reads them, and a
    format-1 run never wrote them. Any format-2 run's frozen bindings and
    named ending are genuinely lost, which is why this direction is an escape
    hatch rather than a supported round trip.
    """
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_column("workflow_result")
    with op.batch_alter_table("workflow_step_records") as batch_op:
        batch_op.drop_column("evidence_json")
