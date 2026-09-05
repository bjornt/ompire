"""SQLite engine and Core table metadata. No ORM: queries are built against
`Table` objects directly. This `metadata` is the schema source of truth;
Alembic migrations under `daemon/alembic/` are generated from it.

Architecture: ADR-0005
(docs/adr/0005-persist-local-state-with-sqlite-core-and-alembic.md)
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import (
    Column,
    Engine,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    event,
    text,
)

metadata = MetaData()

# `checkout_mode`/`setup_state` carry the onboarding facts a bare row could
# not: whether Ompire created the base checkout or adopted the operator's, and
# whether it is usable yet (ADR-0022). `fetch_remote` is the remote spawn
# fetches in *that* checkout — the per-task clone's own `origin` is unrelated
# and unchanged.
projects = Table(
    "projects",
    metadata,
    Column("name", String, primary_key=True),
    Column("title", String, nullable=False),
    Column("upstream_url", String, nullable=False),
    Column("fork_url", String, nullable=True),
    Column("checkout_path", String, nullable=False),
    Column("checkout_mode", String, nullable=False, server_default="adopted"),
    Column("fetch_remote", String, nullable=False, server_default="origin"),
    Column("setup_state", String, nullable=False, server_default="ready"),
    Column("setup_error", Text, nullable=True),
    # Optional global model profile (ADR-0025). NULL means no default; the
    # named, non-cascading FK is schema metadata — the runtime guarantee is the
    # write reservation in `registry/model_profiles.reserved_write`, because
    # this connection does not enable `PRAGMA foreign_keys`.
    Column(
        "default_model_profile",
        String,
        ForeignKey("model_profiles.name", name="fk_projects_default_model_profile"),
        nullable=True,
    ),
    Index("ix_projects_default_model_profile", "default_model_profile"),
)

# Global model profiles (ADR-0025): a reusable name for the four model-role
# bindings. The role map is one small complete document — there is no
# role-level update API and nothing queries profiles by a nested model — so it
# follows the existing JSON-text convention rather than eight fixed columns.
model_profiles = Table(
    "model_profiles",
    metadata,
    Column("name", String, primary_key=True),
    Column("roles_json", Text, nullable=False),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
)

# SPEC Decision 6/9: spawn configuration lives on templates; checkout path and
# remotes come from the referenced project. model/thinking NULL = omp default.
templates = Table(
    "templates",
    metadata,
    Column("name", String, primary_key=True),
    Column("project_name", String, ForeignKey("projects.name"), nullable=False),
    Column("base_branch", String, nullable=False, server_default="main"),
    Column("branch_pattern", String, nullable=False),
    Column("workflow", String, nullable=False, server_default="single-step"),
    Column("workshop_additions", String, nullable=False, server_default="project"),
    Column("model", String, nullable=True),
    Column("thinking", String, nullable=True),
    Column("preamble", Text, nullable=False, server_default=""),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
)

tasks = Table(
    "tasks",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("project_name", String, ForeignKey("projects.name"), nullable=False),
    # Plain column, no FK: pre-existing rows stay NULL, and archived rows keep
    # the name as history even after the template is deleted.
    Column("template_name", String, nullable=True),
    Column("slug", String, nullable=False),
    Column("branch", String, nullable=False),
    Column("clone_path", String, nullable=False),
    Column("state", String, nullable=False),
    Column("prompt", Text, nullable=False),
    Column("error", Text, nullable=True),
    Column("workshop_id", String, nullable=True),
    # Workflow run state (workflow-engine capability): name denormalized from
    # the template at creation; status/step NULL for legacy rows and whenever
    # no run is active.
    Column("workflow_name", String, nullable=False, server_default="single-step"),
    Column("workflow_status", String, nullable=True),
    Column("workflow_step", String, nullable=True),
    Column("pr_url", String, nullable=True),
    Column("pr_state", String, nullable=True),
    Column("pr_merged_at", String, nullable=True),
    Column("spawn_completed_at", String, nullable=True),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
    # A slug is reusable after archive; uniqueness applies to live rows only.
    Index(
        "uq_tasks_live_project_slug",
        "project_name",
        "slug",
        unique=True,
        sqlite_where=text("state != 'archived'"),
    ),
)


# Named omp sessions per task (workflow-engine capability): identity for
# `omp --resume` is per (task, session), not per task.
task_sessions = Table(
    "task_sessions",
    metadata,
    Column("task_id", Integer, ForeignKey("tasks.id"), primary_key=True),
    Column("name", String, primary_key=True),
    Column("omp_session_id", String, nullable=True),
    Column("spawned_at", String, nullable=False),
)

# One row per executed workflow step; identity is (task_id, seq) because loops
# revisit step names. `prompted_at` marks an agent step's prompt as sent, so
# restart recovery can tell "never prompted" (send fresh) from "turn lost"
# (resume-nudge) — see the workflow-engine design's recovery rules.
workflow_step_records = Table(
    "workflow_step_records",
    metadata,
    Column("task_id", Integer, ForeignKey("tasks.id"), primary_key=True),
    Column("seq", Integer, primary_key=True),
    Column("step", String, nullable=False),
    Column("kind", String, nullable=False),
    Column("session", String, nullable=True),
    Column("status", String, nullable=False),
    Column("outcome_json", Text, nullable=True),
    Column("error", Text, nullable=True),
    Column("prompted_at", String, nullable=True),
    Column("started_at", String, nullable=False),
    Column("finished_at", String, nullable=True),
)

# Durable review history (ADR-0016's review slice). One row per task; the
# reviewer process itself is not durable, so its URL and port stay in
# `ReviewManager` memory. `process_started_at` is the write-ahead marker: it
# is stamped before llmvet is launched and cleared when the process is
# observed exiting, so startup can tell an interrupted reviewer from a review
# left `open` because its comments went back to the agent.
reviews = Table(
    "reviews",
    metadata,
    Column("task_id", Integer, ForeignKey("tasks.id"), primary_key=True),
    Column("status", String, nullable=False),
    Column("process_started_at", String, nullable=True),
    Column("created_at", String, nullable=False),
    Column("updated_at", String, nullable=False),
)

# Ordered review iterations; identity is (task_id, seq) because re-review
# after comments appends to the same review's history, mirroring
# `workflow_step_records`.
review_iterations = Table(
    "review_iterations",
    metadata,
    Column("task_id", Integer, ForeignKey("tasks.id"), primary_key=True),
    Column("seq", Integer, primary_key=True),
    Column("outcome", String, nullable=False),
    Column("comment_count", Integer, nullable=True),
    Column("stderr", Text, nullable=True),
    Column("recorded_at", String, nullable=False),
)

# ADR-0013: UI-editable overrides are persisted as JSON-encoded scalar
# values and layered over operator-owned config.toml.
settings = Table(
    "settings",
    metadata,
    Column("key", String, primary_key=True),
    Column("value", Text, nullable=False),
)


def db_path_for(data_dir: Path) -> Path:
    return data_dir / "db" / "ompire.db"


def ensure_db_dir(db_path: Path) -> None:
    """Create the parent directory for the SQLite database, owner-only."""
    db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)


def make_engine(db_path: Path) -> Engine:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}", future=True)

    @event.listens_for(engine, "connect")
    def _set_wal_mode(dbapi_connection, connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    return engine
