"""Migration tests.

`test_fresh_db_*` and `test_reopen_at_head_*` exercise the real app migration
chain. `test_upgrade_from_older_revision_*` builds a synthetic two-revision
alembic project in a temp dir and drives it through the same `upgrade_head`
entry point. The `test_0007_*` tests land a DB at 0006 with seeded rows and
verify the templates seed + project column drops + downgrade round-trip.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
from sqlalchemy import text

from ompire_daemon.db import make_engine
from ompire_daemon.migrate import upgrade_head

REAL_ALEMBIC_INI = Path(__file__).resolve().parent.parent / "alembic.ini"


def test_fresh_db_upgrades_to_head(tmp_path: Path) -> None:
    db_path = tmp_path / "ompire.db"

    upgrade_head(db_path, alembic_ini=REAL_ALEMBIC_INI)

    engine = make_engine(db_path)
    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        tables = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
        task_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(tasks)"))}
        project_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(projects)"))}
    assert version == "0007"
    assert "projects" in tables
    assert "tasks" in tables
    assert "templates" in tables
    assert "pr_url" in task_columns
    assert "template_name" in task_columns
    # The per-project spawn defaults moved to templates (SPEC Decision 6).
    assert "base_branch" not in project_columns
    assert "branch_pattern" not in project_columns


def test_reopen_at_head_is_noop(tmp_path: Path) -> None:
    db_path = tmp_path / "ompire.db"
    upgrade_head(db_path, alembic_ini=REAL_ALEMBIC_INI)

    engine = make_engine(db_path)
    with engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO projects (name, title, upstream_url, fork_url, checkout_path) "
                "VALUES ('demo', 'Demo', 'https://example.com/demo', NULL, '/tmp/demo')"
            )
        )
        conn.commit()

    upgrade_head(db_path, alembic_ini=REAL_ALEMBIC_INI)

    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        row = conn.execute(text("SELECT name FROM projects")).scalar_one()
    assert version == "0007"
    assert row == "demo"


def _land_at_0006(db_path: Path) -> None:
    from alembic import command
    from alembic.config import Config as AlembicConfig

    cfg = AlembicConfig(str(REAL_ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "0006")


def _alembic_cfg(db_path: Path):  # noqa: ANN201
    from alembic.config import Config as AlembicConfig

    cfg = AlembicConfig(str(REAL_ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _seed_0006_rows(db_path: Path) -> None:
    """One project with non-default spawn defaults and one history task row."""
    engine = make_engine(db_path)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO projects (name, title, upstream_url, fork_url, checkout_path, "
                "base_branch, branch_pattern) VALUES "
                "('demo', 'Demo', 'https://example.com/demo', NULL, '/tmp/demo', "
                "'trunk', 'feat/<slug>')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO tasks (project_name, slug, branch, clone_path, state, prompt, "
                "error, workshop_id, session_id, pr_url, spawn_completed_at, created_at, "
                "updated_at) VALUES "
                "('demo', 'old-fix', 'feat/old-fix', '/tmp/tasks/demo/old-fix', 'archived', "
                "'fix it', NULL, NULL, NULL, NULL, '2026-08-01T00:00:00+00:00', "
                "'2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00')"
            )
        )


def test_0007_seeds_templates_and_drops_project_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "ompire.db"
    _land_at_0006(db_path)
    _seed_0006_rows(db_path)

    upgrade_head(db_path, alembic_ini=REAL_ALEMBIC_INI)

    engine = make_engine(db_path)
    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert version == "0007"

        templates = conn.execute(
            text(
                "SELECT name, project_name, base_branch, branch_pattern, workflow, "
                "workshop_additions, model, thinking, preamble FROM templates"
            )
        ).all()
        assert templates == [
            ("demo", "demo", "trunk", "feat/<slug>", "single-step", "project", None, None, "")
        ]

        project_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(projects)"))}
        assert "base_branch" not in project_columns
        assert "branch_pattern" not in project_columns
        # Project rows themselves survive the column drop.
        assert conn.execute(text("SELECT name FROM projects")).all() == [("demo",)]

        # History rows predate templates: no backfill.
        task = conn.execute(
            text("SELECT project_name, slug, template_name FROM tasks")
        ).one()
        assert task == ("demo", "old-fix", None)


def test_0007_downgrade_restores_project_columns(tmp_path: Path) -> None:
    db_path = tmp_path / "ompire.db"
    _land_at_0006(db_path)
    _seed_0006_rows(db_path)
    upgrade_head(db_path, alembic_ini=REAL_ALEMBIC_INI)

    from alembic import command

    command.downgrade(_alembic_cfg(db_path), "0006")

    engine = make_engine(db_path)
    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert version == "0006"
        project_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(projects)"))}
        assert "base_branch" in project_columns
        assert "branch_pattern" in project_columns
        # The column data is gone (accepted per design D-2): defaults fill in.
        row = conn.execute(
            text("SELECT name, base_branch, branch_pattern FROM projects")
        ).one()
        assert row == ("demo", "main", "ompire/<slug>")
        task_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(tasks)"))}
        assert "template_name" not in task_columns

    # And back to head again: the seed re-derives templates from the restored
    # defaults.
    upgrade_head(db_path, alembic_ini=REAL_ALEMBIC_INI)
    with engine.connect() as conn:
        templates = conn.execute(
            text("SELECT name, base_branch, branch_pattern FROM templates")
        ).all()
        assert templates == [("demo", "main", "ompire/<slug>")]


def test_migration_0004_session_id_upgrade_downgrade_roundtrip(tmp_path: Path) -> None:
    db_path = tmp_path / "ompire.db"
    upgrade_head(db_path, alembic_ini=REAL_ALEMBIC_INI)

    engine = make_engine(db_path)
    with engine.connect() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(tasks)"))}
    assert "session_id" in columns
    assert "pr_url" in columns

    from alembic import command
    from alembic.config import Config as AlembicConfig

    cfg = AlembicConfig(str(REAL_ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.downgrade(cfg, "0003")

    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(tasks)"))}
    assert version == "0003"
    assert "session_id" not in columns

    # Back to head: the column returns and existing rows survive the round trip.
    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(tasks)"))}
    assert "session_id" in columns
    assert "pr_url" in columns


@pytest.fixture
def synthetic_two_revision_project(tmp_path: Path) -> Path:
    """A standalone alembic project (unrelated to the real app schema) with
    two revisions, so upgrading from the first to the second is a real
    forward migration we can assert preserves existing rows.
    """
    project_dir = tmp_path / "synthetic_alembic"
    versions_dir = project_dir / "versions"
    versions_dir.mkdir(parents=True)

    (project_dir / "env.py").write_text(
        dedent(
            """
            from sqlalchemy import engine_from_config, pool
            from alembic import context

            config = context.config
            connectable = engine_from_config(
                config.get_section(config.config_ini_section, {}),
                prefix="sqlalchemy.",
                poolclass=pool.NullPool,
            )
            with connectable.connect() as connection:
                context.configure(
                    connection=connection, target_metadata=None, render_as_batch=True
                )
                with context.begin_transaction():
                    context.run_migrations()
            """
        )
    )

    (versions_dir / "0001_widgets.py").write_text(
        dedent(
            """
            revision = "0001"
            down_revision = None
            branch_labels = None
            depends_on = None

            from alembic import op
            import sqlalchemy as sa

            def upgrade() -> None:
                op.create_table(
                    "widgets",
                    sa.Column("id", sa.Integer, primary_key=True),
                    sa.Column("name", sa.String, nullable=False),
                )

            def downgrade() -> None:
                op.drop_table("widgets")
            """
        )
    )

    (versions_dir / "0002_add_note.py").write_text(
        dedent(
            """
            revision = "0002"
            down_revision = "0001"
            branch_labels = None
            depends_on = None

            from alembic import op
            import sqlalchemy as sa

            def upgrade() -> None:
                with op.batch_alter_table("widgets") as batch_op:
                    batch_op.add_column(sa.Column("note", sa.String, nullable=True))

            def downgrade() -> None:
                with op.batch_alter_table("widgets") as batch_op:
                    batch_op.drop_column("note")
            """
        )
    )

    alembic_ini = tmp_path / "synthetic_alembic.ini"
    alembic_ini.write_text(
        dedent(
            f"""
            [alembic]
            script_location = {project_dir}
            sqlalchemy.url =

            [loggers]
            keys = root

            [handlers]
            keys = console

            [formatters]
            keys = generic

            [logger_root]
            level = WARN
            handlers = console
            qualname =

            [handler_console]
            class = StreamHandler
            args = (sys.stderr,)
            level = NOTSET
            formatter = generic

            [formatter_generic]
            format = %(levelname)-5.5s [%(name)s] %(message)s
            datefmt = %H:%M:%S
            """
        )
    )
    return alembic_ini


def test_upgrade_from_older_revision_preserves_rows(
    synthetic_two_revision_project: Path, tmp_path: Path
) -> None:
    db_path = tmp_path / "synthetic.db"

    # Land the db at revision 0001 (older revision) and insert a row.
    upgrade_head_to_revision = synthetic_two_revision_project
    from alembic import command
    from alembic.config import Config as AlembicConfig

    cfg = AlembicConfig(str(upgrade_head_to_revision))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "0001")

    engine = make_engine(db_path)
    with engine.connect() as conn:
        conn.execute(text("INSERT INTO widgets (id, name) VALUES (1, 'gear')"))
        conn.commit()

    # Now upgrade to head (0002) via the real entry point under test.
    upgrade_head(db_path, alembic_ini=synthetic_two_revision_project)

    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        row = conn.execute(text("SELECT id, name, note FROM widgets WHERE id = 1")).one()
    assert version == "0002"
    assert row == (1, "gear", None)
