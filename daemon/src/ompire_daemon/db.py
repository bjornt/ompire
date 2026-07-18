"""SQLite engine and Core table metadata. No ORM: queries are built against
`Table` objects directly. This `metadata` is the schema source of truth;
Alembic migrations under `daemon/alembic/` are generated from it.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Column, Engine, MetaData, String, Table, create_engine, event

metadata = MetaData()

projects = Table(
    "projects",
    metadata,
    Column("name", String, primary_key=True),
    Column("title", String, nullable=False),
    Column("upstream_url", String, nullable=False),
    Column("fork_url", String, nullable=True),
    Column("checkout_path", String, nullable=False),
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
