"""retire templates in favour of project defaults and pinned task inputs

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-05

Template retirement (workflow-first-model-profiles / ADR-0026). Launch policy
stops living on a saved preset: workspace and prompt defaults become project
columns, and the decision one task was accepted under becomes an immutable
JSON document on that task.

The rule this revision follows everywhere is that current configuration is not
historical evidence. A template's contents describe what the *next* launch
would have done, not what an already-running task actually used, so:

- every template row and every task's template attribution is copied verbatim
  into `launch_migration_evidence` — nulls, empty strings and timestamps
  included — before the live storage goes away;
- a workspace default is copied onto the project only when every template of
  that project agrees on it. A disagreement is left unresolved and the project
  is marked `needs-reconciliation`; the operator picks, nothing is guessed
  from "the first template";
- no `model_profiles` row is invented. One old `model`/`thinking` pair cannot
  answer for four roles, an omp fuzzy model name is not a provider-qualified
  identifier, and a project name says nothing about which profile its operator
  would have chosen. Legacy model choices exist here only as evidence, and any
  project that has one needs an explicit decision before it can launch;
- an already-assigned `default_model_profile` (0012 backfilled NULL, so this
  only exists if the operator set it) is left exactly as it is;
- every existing task keeps `execution_inputs_json` NULL. That is the honest
  reading: the original spawn's overrides were never persisted. Those tasks
  stay readable and archivable, and the daemon asks for a confirmed
  continuation configuration before anything that would need one.

`judge_model` is a `config.toml` key, not a database row, so its capture
happens at daemon startup rather than here.
"""
import json
from datetime import UTC, datetime
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: Union[str, Sequence[str], None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# The workspace/prompt fields that move from a template to its project.
_WORKSPACE_FIELDS = ("base_branch", "branch_pattern", "workshop_additions", "preamble")


def upgrade() -> None:
    """Upgrade schema."""
    now = datetime.now(UTC).isoformat()
    conn = op.get_bind()

    op.create_table(
        "launch_migration_evidence",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("scope_kind", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_launch_migration_evidence_scope",
        "launch_migration_evidence",
        ["scope_kind", "scope"],
    )
    op.create_table(
        "launch_reconciliations",
        sa.Column("scope_kind", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("acknowledged_value", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("scope_kind", "scope", "kind"),
    )

    with op.batch_alter_table("projects") as batch_op:
        batch_op.add_column(
            sa.Column(
                "base_branch", sa.String(), nullable=False, server_default="main"
            )
        )
        batch_op.add_column(
            sa.Column(
                "branch_pattern",
                sa.String(),
                nullable=False,
                server_default="ompire/<slug>",
            )
        )
        batch_op.add_column(
            sa.Column(
                "workshop_additions",
                sa.String(),
                nullable=False,
                server_default="project",
            )
        )
        batch_op.add_column(
            sa.Column("preamble", sa.Text(), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column(
                "launch_config_state",
                sa.String(),
                nullable=False,
                server_default="reconciled",
            )
        )
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(sa.Column("execution_inputs_json", sa.Text(), nullable=True))

    def record(kind, scope_kind, scope, source, payload):
        conn.execute(
            sa.text(
                "INSERT INTO launch_migration_evidence "
                "(kind, scope_kind, scope, source, payload_json, recorded_at) "
                "VALUES (:kind, :scope_kind, :scope, :source, :payload, :at)"
            ),
            {
                "kind": kind,
                "scope_kind": scope_kind,
                "scope": str(scope),
                "source": source,
                "payload": json.dumps(payload),
                "at": now,
            },
        )

    # --- 1. copy every template row into evidence, verbatim ------------------
    template_rows = conn.execute(
        sa.text(
            "SELECT name, project_name, base_branch, branch_pattern, workflow, "
            "workshop_additions, model, thinking, preamble, created_at, updated_at "
            "FROM templates ORDER BY name"
        )
    ).mappings().all()
    for row in template_rows:
        record("template", "project", row["project_name"], row["name"], dict(row))

    # --- 2. copy each task's template attribution ---------------------------
    task_rows = conn.execute(
        sa.text("SELECT id, template_name, state FROM tasks ORDER BY id")
    ).mappings().all()
    for row in task_rows:
        record(
            "task-template",
            "task",
            row["id"],
            row["template_name"] or "",
            {"template_name": row["template_name"], "state": row["state"]},
        )

    # --- 3. per project: unambiguous defaults, conflicts, model candidates ---
    by_project: dict[str, list] = {}
    for row in template_rows:
        by_project.setdefault(row["project_name"], []).append(row)

    project_names = [
        r[0]
        for r in conn.execute(sa.text("SELECT name FROM projects ORDER BY name")).all()
    ]
    for project_name in project_names:
        templates = by_project.get(project_name, [])
        if not templates:
            # Nothing to recover. Ordinary defaults apply; startup records
            # that these are *new* values rather than restored history, and
            # substitutes the daemon's configured branch pattern, which is not
            # available inside a migration.
            record("new-defaults", "project", project_name, "", {"fields": list(_WORKSPACE_FIELDS)})
            continue

        conflicts: dict[str, list] = {}
        resolved: dict[str, object] = {}
        for field in _WORKSPACE_FIELDS:
            distinct = []
            for template in templates:
                if template[field] not in distinct:
                    distinct.append(template[field])
            if len(distinct) == 1:
                resolved[field] = distinct[0]
            else:
                conflicts[field] = distinct
        if resolved:
            assignments = ", ".join(f"{field} = :{field}" for field in resolved)
            conn.execute(
                sa.text(f"UPDATE projects SET {assignments} WHERE name = :name"),
                {**resolved, "name": project_name},
            )

        # Legacy model/thinking choices: candidates only, never a profile.
        model_candidates = []
        for template in templates:
            if template["model"] is None and template["thinking"] is None:
                continue
            candidate = {
                "source": template["name"],
                "model": template["model"],
                "thinking": template["thinking"],
            }
            if candidate not in model_candidates:
                model_candidates.append(candidate)

        needs = False
        if conflicts:
            record("workspace-conflict", "project", project_name, "", conflicts)
            needs = True
        if model_candidates:
            record("model-candidates", "project", project_name, "", model_candidates)
            needs = True
        if needs:
            conn.execute(
                sa.text(
                    "UPDATE projects SET launch_config_state = 'needs-reconciliation' "
                    "WHERE name = :name"
                ),
                {"name": project_name},
            )

    # --- 4. remove the live template storage and reference ------------------
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_column("template_name")
    op.drop_table("templates")


def downgrade() -> None:
    """Downgrade schema.

    Recreates the template table and column so the schema matches 0012 again.
    It deliberately does *not* rebuild template rows from evidence: a
    downgrade cannot know which of the recorded candidates the operator has
    since chosen, and inventing rows would be exactly the fabricated history
    this revision exists to avoid.
    """
    op.create_table(
        "templates",
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("project_name", sa.String(), nullable=False),
        sa.Column("base_branch", sa.String(), nullable=False, server_default="main"),
        sa.Column("branch_pattern", sa.String(), nullable=False),
        sa.Column("workflow", sa.String(), nullable=False, server_default="single-step"),
        sa.Column(
            "workshop_additions", sa.String(), nullable=False, server_default="project"
        ),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("thinking", sa.String(), nullable=True),
        sa.Column("preamble", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.ForeignKeyConstraint(["project_name"], ["projects.name"]),
        sa.PrimaryKeyConstraint("name"),
    )
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.add_column(sa.Column("template_name", sa.String(), nullable=True))
        batch_op.drop_column("execution_inputs_json")
    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_column("launch_config_state")
        batch_op.drop_column("preamble")
        batch_op.drop_column("workshop_additions")
        batch_op.drop_column("branch_pattern")
        batch_op.drop_column("base_branch")
    op.drop_index(
        "ix_launch_migration_evidence_scope", table_name="launch_migration_evidence"
    )
    op.drop_table("launch_migration_evidence")
    op.drop_table("launch_reconciliations")
