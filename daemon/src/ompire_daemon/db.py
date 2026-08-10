"""SQLite engine and Core table metadata. No ORM: queries are built against
`Table` objects directly. This `metadata` is the schema source of truth;
Alembic migrations under `daemon/alembic/` are generated from it.
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

projects = Table(
    "projects",
    metadata,
    Column("name", String, primary_key=True),
    Column("title", String, nullable=False),
    Column("upstream_url", String, nullable=False),
    Column("fork_url", String, nullable=True),
    Column("checkout_path", String, nullable=False),
    Column("base_branch", String, nullable=False, server_default="main"),
    Column("branch_pattern", String, nullable=False, server_default="ompire/<slug>"),
)

tasks = Table(
    "tasks",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("project_name", String, ForeignKey("projects.name"), nullable=False),
    Column("slug", String, nullable=False),
    Column("branch", String, nullable=False),
    Column("clone_path", String, nullable=False),
    Column("state", String, nullable=False),
    Column("prompt", Text, nullable=False),
    Column("error", Text, nullable=True),
    Column("workshop_id", String, nullable=True),
    Column("session_id", String, nullable=True),
    Column("pr_url", String, nullable=True),
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


def db_path_for(data_dir: Path) -> Path:
    return data_dir / "ompire.db"


def make_engine(db_path: Path) -> Engine:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}", future=True)

    @event.listens_for(engine, "connect")
    def _set_wal_mode(dbapi_connection, connection_record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    return engine
