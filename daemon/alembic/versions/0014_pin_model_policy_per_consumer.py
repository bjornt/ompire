"""pin model policy per consumer and record what each session applied

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-06

Per-step model overrides (workflow-first-model-profiles / ADR-0027). A task
stops having "a model" and starts having one complete binding per model
consumer — every declared agent step, plus the engine-reserved judge — and a
session starts recording the policy its child last ran under.

Two conversions, both bounded by what the old rows actually said:

- `tasks.execution_inputs_json` version 1 → version 2. The old document holds
  one four-role map, a step→role map, and a judge role, so every consumer's
  version-2 binding is that same profile snapshot with the step's own role.
  That is a re-expression of the stored decision, not a new one: the source
  profile is *not* re-read, the current workflow is *not* consulted (a step
  added since acceptance was never accepted for this task), and a document
  that is NULL stays NULL. Tasks whose inputs were never pinned keep their
  existing explicit-reconciliation requirement.

- `task_sessions.applied_policy_json` is added and backfilled for resumable
  sessions of version-1 tasks. The value comes from the task's own pinned
  map: the stored judge role for the `judge` session, `default` for every
  other one. It is labelled `migrated`, because it says what the session
  continues under — not that any past turn was configured that way. Nobody
  recorded that, and a version-1 daemon in fact resumed every session on
  `default`, judge included; this revision fixes the *future* behavior
  without claiming to know the past.
"""
import json
from datetime import UTC, datetime
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0014"
down_revision: Union[str, Sequence[str], None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_ROLES = ("default", "smol", "slow", "plan")
_JUDGE_SESSION = "judge"


def _policy(roles: dict, active_role: str) -> dict:
    return {
        "active": roles[active_role],
        "smol": roles["smol"],
        "slow": roles["slow"],
        "plan": roles["plan"],
    }


def _binding(profile_name, roles: dict, role: str, *, profile_source: str, role_source: str) -> dict:
    return {
        "profile_name": profile_name,
        "profile_source": profile_source,
        "role": role,
        "role_source": role_source,
        "roles": {name: roles[name] for name in _ROLES},
    }


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()
    now = datetime.now(UTC).isoformat()

    with op.batch_alter_table("task_sessions") as batch_op:
        batch_op.add_column(sa.Column("applied_policy_json", sa.Text(), nullable=True))

    rows = conn.execute(
        sa.text(
            "SELECT id, execution_inputs_json FROM tasks "
            "WHERE execution_inputs_json IS NOT NULL"
        )
    ).all()
    for task_id, raw in rows:
        document = json.loads(raw)
        if document.get("version") != 1:
            # Already version 2 (or something this revision does not
            # understand): leave it alone rather than re-encode a shape whose
            # meaning it cannot verify.
            continue
        roles = document["roles"]
        step_roles = document.get("step_roles") or {}
        judge_role = document.get("judge_role", "slow")
        profile_name = document.get("model_profile_name")
        profile_source = document.get("model_profile_source", "task")

        # A version-1 task pinned exactly one profile, so every consumer
        # inherits it. `role_source` is `workflow` throughout: version 1 had
        # no way to express a per-step role choice, and labelling these as
        # operator overrides would invent a decision.
        document["step_bindings"] = {
            step: _binding(
                profile_name,
                roles,
                role,
                profile_source=profile_source,
                role_source="workflow",
            )
            for step, role in sorted(step_roles.items())
        }
        document["auxiliary_bindings"] = {
            _JUDGE_SESSION: _binding(
                profile_name,
                roles,
                judge_role,
                profile_source=profile_source,
                role_source="workflow",
            )
        }
        for retired in ("roles", "step_roles", "judge_role"):
            document.pop(retired, None)
        document["version"] = 2
        conn.execute(
            sa.text("UPDATE tasks SET execution_inputs_json = :doc WHERE id = :id"),
            {"doc": json.dumps(document), "id": task_id},
        )

        sessions = conn.execute(
            sa.text(
                "SELECT name FROM task_sessions "
                "WHERE task_id = :id AND omp_session_id IS NOT NULL"
            ),
            {"id": task_id},
        ).all()
        for (name,) in sessions:
            role = judge_role if name == _JUDGE_SESSION else "default"
            applied = {
                "version": 1,
                "policy": _policy(roles, role),
                "profile_name": profile_name,
                "role": role,
                # No declared consumer applied this; the upgrade derived it.
                "consumer_kind": None,
                "consumer_name": None,
                "origin": "migrated",
                "applied_at": now,
            }
            conn.execute(
                sa.text(
                    "UPDATE task_sessions SET applied_policy_json = :doc "
                    "WHERE task_id = :id AND name = :name"
                ),
                {"doc": json.dumps(applied), "id": task_id, "name": name},
            )


def downgrade() -> None:
    """Downgrade schema.

    Converts version-2 documents back to the version-1 shape and drops the
    applied-policy column. A per-consumer override cannot survive the round
    trip — version 1 has nowhere to put it — so the task-wide profile's own
    snapshot is what is written back, and a document whose consumers disagree
    on their profile is left at version 2 rather than silently flattened to
    one of them.
    """
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT id, execution_inputs_json FROM tasks "
            "WHERE execution_inputs_json IS NOT NULL"
        )
    ).all()
    for task_id, raw in rows:
        document = json.loads(raw)
        if document.get("version") != 2:
            continue
        step_bindings = document.get("step_bindings") or {}
        auxiliary = document.get("auxiliary_bindings") or {}
        bindings = list(step_bindings.values()) + list(auxiliary.values())
        if not bindings:
            continue
        snapshots = {json.dumps(b["roles"], sort_keys=True) for b in bindings}
        if len(snapshots) > 1:
            # Distinct profiles per consumer: there is no honest version-1
            # value. Leave the document as it is; a version-1 daemon refuses
            # it explicitly rather than running the wrong model.
            continue
        judge = auxiliary.get(_JUDGE_SESSION)
        document["roles"] = bindings[0]["roles"]
        document["step_roles"] = {
            step: binding["role"] for step, binding in sorted(step_bindings.items())
        }
        document["judge_role"] = judge["role"] if judge else "slow"
        document.pop("step_bindings", None)
        document.pop("auxiliary_bindings", None)
        document["version"] = 1
        conn.execute(
            sa.text("UPDATE tasks SET execution_inputs_json = :doc WHERE id = :id"),
            {"doc": json.dumps(document), "id": task_id},
        )

    with op.batch_alter_table("task_sessions") as batch_op:
        batch_op.drop_column("applied_policy_json")
