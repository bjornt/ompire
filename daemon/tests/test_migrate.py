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
    assert version == "0008"
    assert "projects" in tables
    assert "tasks" in tables
    assert "templates" in tables
    assert "task_sessions" in tables
    assert "workflow_step_records" in tables
    assert "pr_url" in task_columns
    assert "template_name" in task_columns
    # Workflow run state lives on the task row (workflow-engine capability).
    assert "workflow_name" in task_columns
    assert "workflow_status" in task_columns
    assert "workflow_step" in task_columns
    # Session identity moved to task_sessions (per-session rows).
    assert "session_id" not in task_columns
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
    assert version == "0008"
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
        assert version == "0008"

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


def _land_at_0007_with_tasks(db_path: Path) -> None:
    """Upgrade to 0007, then seed one live spawn-completed task with a session
    id, one live mid-spawn task (never completed), one failed task with a
    session id, and one archived task with a session id."""
    from alembic import command

    command.upgrade(_alembic_cfg(db_path), "0007")
    engine = make_engine(db_path)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO projects (name, title, upstream_url, fork_url, checkout_path) "
                "VALUES ('demo', 'Demo', 'https://example.com/demo', NULL, '/tmp/demo')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO tasks (project_name, template_name, slug, branch, clone_path, "
                "state, prompt, error, workshop_id, session_id, pr_url, spawn_completed_at, "
                "created_at, updated_at) VALUES "
                "('demo', 'demo', 'live-fix', 'ompire/live-fix', '/tmp/tasks/demo/live-fix', "
                "'created', 'fix it', NULL, 'ws-1', 'omp-session-1', NULL, "
                "'2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00', "
                "'2026-08-01T00:00:00+00:00'), "
                "('demo', 'demo', 'mid-spawn', 'ompire/mid-spawn', '/tmp/tasks/demo/mid-spawn', "
                "'created', 'fix it', NULL, NULL, NULL, NULL, NULL, "
                "'2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00'), "
                "('demo', 'demo', 'failed-task', 'ompire/failed-task', '/tmp/tasks/demo/failed-task', "
                "'failed', 'fix it', 'boom', 'ws-2', 'omp-session-2', NULL, "
                "'2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00', "
                "'2026-08-01T00:00:00+00:00'), "
                "('demo', 'demo', 'archived-task', 'ompire/archived-task', "
                "'/tmp/tasks/demo/archived-task', 'archived', 'fix it', NULL, 'ws-3', "
                "'omp-session-3', NULL, '2026-08-01T00:00:00+00:00', "
                "'2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00')"
            )
        )


def test_0008_backfills_sessions_and_legacy_workflow_runs(tmp_path: Path) -> None:
    """Design D-5: live tasks' session ids become session `main` rows; legacy
    live (spawn-completed, created) tasks become `single-step`/`complete` with
    one ok `work` record so the engine never re-drives them."""
    db_path = tmp_path / "ompire.db"
    _land_at_0007_with_tasks(db_path)

    upgrade_head(db_path, alembic_ini=REAL_ALEMBIC_INI)

    engine = make_engine(db_path)
    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert version == "0008"

        sessions = conn.execute(
            text("SELECT task_id, name, omp_session_id FROM task_sessions ORDER BY task_id")
        ).all()
        task_ids = {
            row[0]: row[1]
            for row in conn.execute(text("SELECT slug, id FROM tasks"))
        }
        live_id = task_ids["live-fix"]
        # Live tasks with a session id are backfilled; failed ones too (they
        # are live rows), archived ones are not.
        assert (live_id, "main", "omp-session-1") in sessions
        assert (task_ids["failed-task"], "main", "omp-session-2") in sessions
        assert not any(s[0] == task_ids["archived-task"] for s in sessions)
        assert not any(s[0] == task_ids["mid-spawn"] for s in sessions)

        records = conn.execute(
            text(
                "SELECT task_id, seq, step, kind, session, status FROM workflow_step_records"
            )
        ).all()
        # Only the spawn-completed live task gets the synthetic ok record.
        assert records == [(live_id, 1, "work", "agent", "main", "ok")]

        runs = {
            row[0]: (row[1], row[2])
            for row in conn.execute(
                text("SELECT slug, workflow_name, workflow_status FROM tasks")
            )
        }
        assert runs["live-fix"] == ("single-step", "complete")
        assert runs["mid-spawn"] == ("single-step", None)
        assert runs["failed-task"] == ("single-step", None)
        assert runs["archived-task"] == ("single-step", None)

        task_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(tasks)"))}
        assert "session_id" not in task_columns


def test_0008_downgrade_restores_session_id(tmp_path: Path) -> None:
    """The downgrade re-adds `tasks.session_id` from each task's `main`
    session row (documented data loss for non-`main` sessions and step
    history)."""
    db_path = tmp_path / "ompire.db"
    _land_at_0007_with_tasks(db_path)
    upgrade_head(db_path, alembic_ini=REAL_ALEMBIC_INI)

    from alembic import command

    command.downgrade(_alembic_cfg(db_path), "0007")

    engine = make_engine(db_path)
    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert version == "0007"
        task_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(tasks)"))}
        assert "session_id" in task_columns
        assert "workflow_name" not in task_columns
        rows = {
            row[0]: row[1]
            for row in conn.execute(text("SELECT slug, session_id FROM tasks"))
        }
        assert rows["live-fix"] == "omp-session-1"
        assert rows["failed-task"] == "omp-session-2"
        # Archived tasks were not backfilled into task_sessions: nothing to
        # restore (their clones are deleted anyway).
        assert rows["archived-task"] is None
        tables = {
            row[0]
            for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
        assert "task_sessions" not in tables
        assert "workflow_step_records" not in tables


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
    """0004 added `tasks.session_id`; 0008 moved session identity to
    `task_sessions`. Downgrading to 0003 still drops the old column;
    re-upgrading to 0007 restores it, and 0008 moves it off again."""
    db_path = tmp_path / "ompire.db"
    upgrade_head(db_path, alembic_ini=REAL_ALEMBIC_INI)

    engine = make_engine(db_path)
    with engine.connect() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(tasks)"))}
    # At head, session identity lives on task_sessions, not the task row.
    assert "session_id" not in columns
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

    # 0007 has the column back; head drops it again (into task_sessions).
    command.upgrade(cfg, "0007")
    with engine.connect() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(tasks)"))}
    assert "session_id" in columns
    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(tasks)"))}
        tables = {
            row[0]
            for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
    assert "session_id" not in columns
    assert "task_sessions" in tables


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
