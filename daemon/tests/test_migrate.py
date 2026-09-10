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


def test_upgrade_head_keeps_existing_loggers_enabled(tmp_path: Path) -> None:
    """Migrations run in-process at daemon startup, so alembic's `fileConfig`
    must not disable existing loggers. The default (`True`) silently killed
    every ompire_daemon.* logger, and no daemon log line reached the journal
    (dogfooding: a failed ship was invisible in journald)."""
    import logging

    logger = logging.getLogger("ompire_daemon.ship")
    logger.disabled = False

    upgrade_head(tmp_path / "ompire.db", alembic_ini=REAL_ALEMBIC_INI)

    assert not logger.disabled


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
    assert version == "0023"
    assert "projects" in tables
    assert "tasks" in tables
    # Templates are retired (ADR-0026): the live table is gone and only inert
    # upgrade evidence remains.
    assert "templates" not in tables
    assert "launch_migration_evidence" in tables
    assert "launch_reconciliations" in tables
    assert "task_sessions" in tables
    assert "workflow_step_records" in tables
    assert "settings" in tables
    # Durable review history (review capability; ADR-0016's review slice).
    assert "reviews" in tables
    assert "review_iterations" in tables
    # The operator-owned library over the append-only revisions (ADR-0031).
    assert "workflow_revisions" in tables
    assert "workflow_library" in tables
    assert "pr_url" in task_columns
    assert "template_name" not in task_columns
    # The launch decision a task was accepted under (ADR-0026).
    assert "execution_inputs_json" in task_columns
    # Workflow run state lives on the task row (workflow-engine capability).
    assert "workflow_name" in task_columns
    assert "workflow_status" in task_columns
    assert "workflow_step" in task_columns
    # Session identity moved to task_sessions (per-session rows).
    assert "session_id" not in task_columns
    # Workspace and prompt defaults live on the project again (ADR-0026) —
    # as defaults a launch inherits, not as a saved launch preset.
    assert "base_branch" in project_columns
    assert "branch_pattern" in project_columns
    assert "workshop_additions" in project_columns
    assert "preamble" in project_columns
    assert "launch_config_state" in project_columns
    # Checkout onboarding facts (ADR-0022).
    assert "checkout_mode" in project_columns
    assert "fetch_remote" in project_columns
    assert "setup_state" in project_columns
    assert "setup_error" in project_columns
    # Global model profiles and the optional project reference (ADR-0025).
    assert "model_profiles" in tables
    assert "default_model_profile" in project_columns
    # Durable task results retained outside the workspace (ADR-0034).
    assert "task_results" in tables
    assert "task_result_files" in tables
    assert "results_version" in task_columns


def test_0011_backfills_existing_projects_as_adopted(tmp_path: Path) -> None:
    """A row written before 0011 reads as an adopted, ready, `origin` checkout.

    That is the only honest reading, and it must not depend on the checkout
    still being on disk — the migration never touches the filesystem.
    """
    from alembic import command

    db_path = tmp_path / "ompire.db"
    command.upgrade(_alembic_cfg(db_path), "0010")
    engine = make_engine(db_path)
    with engine.connect() as conn:
        conn.execute(
            text(
                "INSERT INTO projects (name, title, upstream_url, fork_url, checkout_path) "
                "VALUES ('legacy', 'Legacy', 'https://example.com/legacy', NULL, "
                "'/nonexistent/legacy')"
            )
        )
        conn.commit()

    upgrade_head(db_path, alembic_ini=REAL_ALEMBIC_INI)

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT checkout_mode, fetch_remote, setup_state, setup_error "
                "FROM projects WHERE name = 'legacy'"
            )
        ).one()
    assert row.checkout_mode == "adopted"
    assert row.fetch_remote == "origin"
    assert row.setup_state == "ready"
    assert row.setup_error is None
    assert not Path("/nonexistent/legacy").exists()


def test_0012_preserves_data_and_backfills_no_default(tmp_path: Path) -> None:
    """0012 is purely additive: every existing project, template and task row
    survives, and each project reads back with no model profile.

    No default is invented from a template's model, a provider credential, or
    the project's name — nothing before this revision recorded that choice.
    """
    from alembic import command

    db_path = tmp_path / "ompire.db"
    _land_at_0007_with_tasks(db_path)
    command.upgrade(_alembic_cfg(db_path), "0011")
    engine = make_engine(db_path)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO templates (name, project_name, base_branch, branch_pattern, "
                "workflow, workshop_additions, model, thinking, preamble, created_at, "
                "updated_at) VALUES ('demo', 'demo', 'main', 'ompire/<slug>', "
                "'single-step', 'project', 'sonnet', 'high', '', "
                "'2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00')"
            )
        )

    command.upgrade(_alembic_cfg(db_path), "0012")

    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM tasks")).scalar_one() == 4
        assert conn.execute(text("SELECT COUNT(*) FROM model_profiles")).scalar_one() == 0
        assert conn.execute(
            text("SELECT name, default_model_profile FROM projects")
        ).all() == [("demo", None)]
        # The template's own model/thinking are untouched by this revision.
        assert conn.execute(
            text("SELECT model, thinking FROM templates WHERE name = 'demo'")
        ).one() == ("sonnet", "high")

    command.downgrade(_alembic_cfg(db_path), "0011")
    with engine.connect() as conn:
        project_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(projects)"))}
        tables = {
            row[0]
            for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
        assert "default_model_profile" not in project_columns
        assert "model_profiles" not in tables
        assert conn.execute(text("SELECT COUNT(*) FROM tasks")).scalar_one() == 4


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
    assert version == "0023"
    assert row == "demo"


def _land_at_0006(db_path: Path) -> None:
    from alembic.config import Config as AlembicConfig

    from alembic import command

    cfg = AlembicConfig(str(REAL_ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "0006")


def _alembic_cfg(db_path: Path):
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
    """Historical behavior of 0007, checked at 0007. Templates are retired at
    0013, but the revision that created them is unchanged and still has to
    work on the way through."""
    from alembic import command

    db_path = tmp_path / "ompire.db"
    _land_at_0006(db_path)
    _seed_0006_rows(db_path)

    command.upgrade(_alembic_cfg(db_path), "0007")

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
    from alembic import command

    db_path = tmp_path / "ompire.db"
    _land_at_0007_with_tasks(db_path)

    command.upgrade(_alembic_cfg(db_path), "0008")

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
    from alembic import command

    db_path = tmp_path / "ompire.db"
    _land_at_0006(db_path)
    _seed_0006_rows(db_path)
    command.upgrade(_alembic_cfg(db_path), "0007")

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

    # And back to 0007 again: the seed re-derives templates from the restored
    # defaults.
    command.upgrade(_alembic_cfg(db_path), "0007")
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

    from alembic.config import Config as AlembicConfig

    from alembic import command

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
    from alembic.config import Config as AlembicConfig

    from alembic import command

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


def test_0010_review_tables_roundtrip(tmp_path: Path) -> None:
    """0010 adds review history and backfills nothing: tasks that predate it
    legitimately have none, and inventing one would fabricate provenance."""
    db_path = tmp_path / "ompire.db"
    _land_at_0007_with_tasks(db_path)
    upgrade_head(db_path, alembic_ini=REAL_ALEMBIC_INI)

    engine = make_engine(db_path)
    with engine.connect() as conn:
        review_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(reviews)"))}
        iteration_columns = {
            row[1] for row in conn.execute(text("PRAGMA table_info(review_iterations)"))
        }
        # 0018 adds the nullable candidate binding to both (ADR-0032), and
        # 0019 the nullable run link and the retained report. Every one of
        # them is nullable precisely so this backfill-free property survives:
        # a review recorded before content binding keeps a NULL candidate
        # rather than being attributed to a tree nobody reviewed, and one
        # started by hand keeps a NULL step rather than a run that never
        # asked for it.
        assert review_columns == {
            "task_id",
            "status",
            "process_started_at",
            "candidate_id",
            "workflow_seq",
            "created_at",
            "updated_at",
        }
        assert iteration_columns == {
            "task_id",
            "seq",
            "outcome",
            "comment_count",
            "stderr",
            "candidate_id",
            "workflow_seq",
            "findings",
            "findings_state",
            "recorded_at",
        }
        # No backfill for pre-existing tasks.
        assert conn.execute(text("SELECT COUNT(*) FROM reviews")).scalar_one() == 0

    from alembic import command

    command.downgrade(_alembic_cfg(db_path), "0009")
    with engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        tables = {
            row[0]
            for row in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        }
    assert version == "0009"
    assert "reviews" not in tables
    assert "review_iterations" not in tables

    # And forward again: task rows survive the round trip.
    upgrade_head(db_path, alembic_ini=REAL_ALEMBIC_INI)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM tasks")).scalar_one() == 4
        assert conn.execute(text("SELECT COUNT(*) FROM reviews")).scalar_one() == 0


# --- 0013: template retirement (ADR-0026) ------------------------------------


def _land_at_0012(db_path: Path) -> None:
    from alembic import command

    command.upgrade(_alembic_cfg(db_path), "0012")


def _insert_project(conn, name: str, **overrides) -> None:
    values = {
        "name": name,
        "title": name.title(),
        "upstream_url": f"https://example.com/{name}.git",
        "checkout_path": f"/tmp/{name}",
    }
    values.update(overrides)
    columns = ", ".join(values)
    binds = ", ".join(f":{key}" for key in values)
    conn.execute(text(f"INSERT INTO projects ({columns}) VALUES ({binds})"), values)


def _insert_template(conn, name: str, project: str, **overrides) -> None:
    values = {
        "name": name,
        "project_name": project,
        "base_branch": "main",
        "branch_pattern": "ompire/<slug>",
        "workflow": "single-step",
        "workshop_additions": "project",
        "model": None,
        "thinking": None,
        "preamble": "",
        "created_at": "2026-08-01T00:00:00+00:00",
        "updated_at": "2026-08-01T00:00:00+00:00",
    }
    values.update(overrides)
    columns = ", ".join(values)
    binds = ", ".join(f":{key}" for key in values)
    conn.execute(text(f"INSERT INTO templates ({columns}) VALUES ({binds})"), values)


def _insert_task(conn, project: str, slug: str, template: str | None, state: str = "created") -> None:
    conn.execute(
        text(
            "INSERT INTO tasks (project_name, template_name, slug, branch, clone_path, "
            "state, prompt, workflow_name, created_at, updated_at) VALUES "
            "(:project, :template, :slug, :branch, :clone, :state, 'p', 'single-step', "
            "'2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00')"
        ),
        {
            "project": project,
            "template": template,
            "slug": slug,
            "branch": f"ompire/{slug}",
            "clone": f"/tmp/tasks/{project}/{slug}",
            "state": state,
        },
    )


def _evidence(conn, kind: str) -> list:
    import json as _json

    rows = conn.execute(
        text(
            "SELECT scope_kind, scope, source, payload_json FROM "
            "launch_migration_evidence WHERE kind = :kind ORDER BY id"
        ),
        {"kind": kind},
    ).all()
    return [(r.scope_kind, r.scope, r.source, _json.loads(r.payload_json)) for r in rows]


def test_0013_one_template_moves_its_defaults_onto_the_project(tmp_path: Path) -> None:
    """The unambiguous case: one template, so every field it held becomes the
    project's default and nothing needs deciding."""
    db_path = tmp_path / "ompire.db"
    _land_at_0012(db_path)
    engine = make_engine(db_path)
    with engine.begin() as conn:
        _insert_project(conn, "solo")
        _insert_template(
            conn,
            "solo-tpl",
            "solo",
            base_branch="trunk",
            branch_pattern="feat/<slug>",
            workshop_additions="global",
            preamble="house style",
        )

    upgrade_head(db_path, alembic_ini=REAL_ALEMBIC_INI)

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT base_branch, branch_pattern, workshop_additions, preamble, "
                "launch_config_state FROM projects WHERE name = 'solo'"
            )
        ).one()
    assert row.base_branch == "trunk"
    assert row.branch_pattern == "feat/<slug>"
    assert row.workshop_additions == "global"
    assert row.preamble == "house style"
    # No model was ever configured, so there is nothing to reconcile.
    assert row.launch_config_state == "reconciled"


def test_0013_conflicting_templates_leave_the_project_unreconciled(tmp_path: Path) -> None:
    """Two templates disagreeing about a field is not resolved by picking the
    first one: every distinct candidate is preserved and the project is
    blocked until the operator chooses."""
    db_path = tmp_path / "ompire.db"
    _land_at_0012(db_path)
    engine = make_engine(db_path)
    with engine.begin() as conn:
        _insert_project(conn, "multi")
        _insert_template(conn, "a", "multi", base_branch="main", preamble="")
        _insert_template(conn, "b", "multi", base_branch="release", preamble="be careful")

    upgrade_head(db_path, alembic_ini=REAL_ALEMBIC_INI)

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT base_branch, branch_pattern, preamble, launch_config_state "
                "FROM projects WHERE name = 'multi'"
            )
        ).one()
        conflicts = _evidence(conn, "workspace-conflict")
        templates = _evidence(conn, "template")
    assert row.launch_config_state == "needs-reconciliation"
    # The field they agree on is still copied across.
    assert row.branch_pattern == "ompire/<slug>"
    # The ones they disagree about keep their column defaults and are listed
    # as candidates — including the empty preamble, which is a real value.
    assert conflicts[0][3]["base_branch"] == ["main", "release"]
    assert conflicts[0][3]["preamble"] == ["", "be careful"]
    # Both template rows survive verbatim as inert evidence, with their source.
    assert {source for _, _, source, _ in templates} == {"a", "b"}


def test_0013_legacy_model_choices_become_candidates_never_a_profile(
    tmp_path: Path,
) -> None:
    """One old concrete model/thinking pair cannot answer for four roles, and
    an omp fuzzy name is not a provider-qualified identifier. It is recorded
    as a candidate and the project is blocked until a real profile is chosen."""
    db_path = tmp_path / "ompire.db"
    _land_at_0012(db_path)
    engine = make_engine(db_path)
    with engine.begin() as conn:
        _insert_project(conn, "modelled")
        _insert_template(conn, "m", "modelled", model="sonnet", thinking="high")

    upgrade_head(db_path, alembic_ini=REAL_ALEMBIC_INI)

    with engine.connect() as conn:
        state = conn.execute(
            text("SELECT launch_config_state, default_model_profile FROM projects")
        ).one()
        profiles = conn.execute(text("SELECT COUNT(*) FROM model_profiles")).scalar_one()
        candidates = _evidence(conn, "model-candidates")
    assert state.launch_config_state == "needs-reconciliation"
    assert state.default_model_profile is None
    assert profiles == 0
    assert candidates[0][3] == [{"source": "m", "model": "sonnet", "thinking": "high"}]


def test_0013_a_null_model_is_not_a_model_choice(tmp_path: Path) -> None:
    """A template that never set a model recorded no choice at all, so there
    is nothing to reconcile — the operator picks a profile at launch."""
    db_path = tmp_path / "ompire.db"
    _land_at_0012(db_path)
    engine = make_engine(db_path)
    with engine.begin() as conn:
        _insert_project(conn, "unset")
        _insert_template(conn, "u", "unset", model=None, thinking=None)

    upgrade_head(db_path, alembic_ini=REAL_ALEMBIC_INI)

    with engine.connect() as conn:
        state = conn.execute(
            text("SELECT launch_config_state FROM projects WHERE name = 'unset'")
        ).scalar_one()
        assert _evidence(conn, "model-candidates") == []
    assert state == "reconciled"


def test_0013_an_already_assigned_profile_is_not_overwritten(tmp_path: Path) -> None:
    db_path = tmp_path / "ompire.db"
    _land_at_0012(db_path)
    engine = make_engine(db_path)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO model_profiles (name, roles_json, created_at, updated_at) "
                "VALUES ('chosen', '{}', '2026-08-01', '2026-08-01')"
            )
        )
        _insert_project(conn, "assigned", default_model_profile="chosen")
        _insert_template(conn, "t", "assigned", model="sonnet")

    upgrade_head(db_path, alembic_ini=REAL_ALEMBIC_INI)

    with engine.connect() as conn:
        assert (
            conn.execute(
                text("SELECT default_model_profile FROM projects WHERE name = 'assigned'")
            ).scalar_one()
            == "chosen"
        )


def test_0013_zero_template_projects_get_new_defaults_marked_as_new(
    tmp_path: Path,
) -> None:
    """Nothing to recover, so ordinary defaults apply — recorded as *new*
    values rather than restored history."""
    db_path = tmp_path / "ompire.db"
    _land_at_0012(db_path)
    engine = make_engine(db_path)
    with engine.begin() as conn:
        _insert_project(conn, "bare")

    upgrade_head(db_path, alembic_ini=REAL_ALEMBIC_INI)

    with engine.connect() as conn:
        state = conn.execute(
            text("SELECT launch_config_state FROM projects WHERE name = 'bare'")
        ).scalar_one()
        new_defaults = _evidence(conn, "new-defaults")
    assert state == "reconciled"
    assert new_defaults[0][1] == "bare"


def test_0013_tasks_keep_their_history_and_gain_no_invented_inputs(
    tmp_path: Path,
) -> None:
    """A task created before pinned inputs has none; its template attribution
    survives as evidence, and archived rows stay archived and readable."""
    db_path = tmp_path / "ompire.db"
    _land_at_0012(db_path)
    engine = make_engine(db_path)
    with engine.begin() as conn:
        _insert_project(conn, "hist")
        _insert_template(conn, "h", "hist", base_branch="trunk")
        _insert_task(conn, "hist", "live", "h")
        _insert_task(conn, "hist", "old", None, state="archived")

    upgrade_head(db_path, alembic_ini=REAL_ALEMBIC_INI)

    with engine.connect() as conn:
        rows = {
            row.slug: row
            for row in conn.execute(
                text("SELECT slug, state, branch, execution_inputs_json FROM tasks")
            )
        }
        attribution = {scope: payload for _, scope, _, payload in _evidence(conn, "task-template")}
    # Neither task was given the template's current contents as history.
    assert rows["live"].execution_inputs_json is None
    assert rows["old"].execution_inputs_json is None
    assert rows["old"].state == "archived"
    assert rows["live"].branch == "ompire/live"
    # The attribution is preserved, including its absence for the older row.
    assert sorted(
        (v["template_name"] or "") for v in attribution.values()
    ) == ["", "h"]


def test_0013_is_reentrant_across_a_restart_during_reconciliation(
    tmp_path: Path,
) -> None:
    """Re-running the whole upgrade path must not re-block a project the
    operator has already reconciled, nor duplicate its evidence."""
    db_path = tmp_path / "ompire.db"
    _land_at_0012(db_path)
    engine = make_engine(db_path)
    with engine.begin() as conn:
        _insert_project(conn, "twice")
        _insert_template(conn, "a", "twice", base_branch="main")
        _insert_template(conn, "b", "twice", base_branch="release")

    upgrade_head(db_path, alembic_ini=REAL_ALEMBIC_INI)
    with engine.begin() as conn:
        # The operator decides.
        conn.execute(
            text(
                "UPDATE projects SET base_branch = 'release', "
                "launch_config_state = 'reconciled' WHERE name = 'twice'"
            )
        )

    upgrade_head(db_path, alembic_ini=REAL_ALEMBIC_INI)

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT base_branch, launch_config_state FROM projects WHERE name = 'twice'"
            )
        ).one()
        assert len(_evidence(conn, "template")) == 2
    assert row.base_branch == "release"
    assert row.launch_config_state == "reconciled"


# --- 0014: per-consumer bindings and applied session policy (ADR-0027) -------


def _land_at_0013(db_path: Path) -> None:
    from alembic import command

    command.upgrade(_alembic_cfg(db_path), "0013")


def _upgrade_to_0014(db_path: Path) -> None:
    """Stop at 0014 deliberately: these tests are about *that* conversion, and
    running past it would be testing two revisions at once."""
    from alembic import command

    command.upgrade(_alembic_cfg(db_path), "0014")


_V1_ROLES = {
    "default": {"model": "vendor/main", "thinking": "medium"},
    "smol": {"model": "vendor/small", "thinking": "off"},
    "slow": {"model": "vendor/big", "thinking": "high"},
    "plan": {"model": "vendor/planner", "thinking": "xhigh"},
}


def _v1_document(**overrides) -> str:
    import json as _json

    document = {
        "version": 1,
        "provenance": "accepted",
        "accepted_at": "2026-09-01T00:00:00+00:00",
        "project_name": "legacy",
        "workflow_name": "bugfix",
        "model_profile_name": "retired-profile",
        "model_profile_source": "task",
        "roles": _V1_ROLES,
        "step_roles": {"reproduce": "default", "fix": "default"},
        "judge_role": "slow",
        "workspace": {
            "base_branch": "trunk",
            "branch_pattern": "ompire/<slug>",
            "workshop_additions": "project",
            "preamble": "house style",
        },
        "workspace_overrides": ["base_branch"],
        "branch": "ompire/legacy-task",
        "checkout_path": "/tmp/legacy",
        "fetch_remote": "origin",
        "upstream_url": "https://example.com/legacy.git",
        "fork_url": None,
        "unknown_inputs": [],
    }
    document.update(overrides)
    return _json.dumps(document)


def _insert_pinned_task(conn, slug: str, document: str | None) -> None:
    conn.execute(
        text(
            "INSERT INTO tasks (project_name, slug, branch, clone_path, state, prompt, "
            "workflow_name, execution_inputs_json, created_at, updated_at) VALUES "
            "('legacy', :slug, :branch, :clone, 'created', 'p', 'bugfix', :doc, "
            "'2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00')"
        ),
        {
            "slug": slug,
            "branch": f"ompire/{slug}",
            "clone": f"/tmp/tasks/legacy/{slug}",
            "doc": document,
        },
    )


def _insert_session(conn, task_id: int, name: str, omp_session_id: str | None) -> None:
    conn.execute(
        text(
            "INSERT INTO task_sessions (task_id, name, omp_session_id, spawned_at) "
            "VALUES (:task, :name, :sid, '2026-09-01T00:00:00+00:00')"
        ),
        {"task": task_id, "name": name, "sid": omp_session_id},
    )


def _pinned(conn, slug: str) -> dict:
    import json as _json

    raw = conn.execute(
        text("SELECT execution_inputs_json FROM tasks WHERE slug = :slug"), {"slug": slug}
    ).scalar_one()
    return _json.loads(raw)


def test_0014_converts_a_pinned_task_to_per_consumer_bindings(tmp_path: Path) -> None:
    """A version-1 task keeps the execution policy it was accepted with, now
    expressed per consumer — re-expressed from what the document already
    stored, not re-resolved from anything current."""
    db_path = tmp_path / "ompire.db"
    _land_at_0013(db_path)
    engine = make_engine(db_path)
    with engine.begin() as conn:
        _insert_project(conn, "legacy")
        _insert_pinned_task(conn, "legacy-task", _v1_document())

    _upgrade_to_0014(db_path)

    with engine.connect() as conn:
        document = _pinned(conn, "legacy-task")

    assert document["version"] == 2
    # The retired shape is gone, not left beside the new one.
    for retired in ("roles", "step_roles", "judge_role"):
        assert retired not in document

    for step in ("reproduce", "fix"):
        binding = document["step_bindings"][step]
        assert binding["profile_name"] == "retired-profile"
        assert binding["profile_source"] == "task"
        assert binding["role"] == "default"
        # Version 1 could not express a per-step role choice, so calling one
        # an operator override would invent a decision that never happened.
        assert binding["role_source"] == "workflow"
        assert binding["roles"] == _V1_ROLES

    judge = document["auxiliary_bindings"]["judge"]
    assert judge["role"] == "slow"
    assert judge["roles"] == _V1_ROLES

    # Everything the document already said stays exactly as it was.
    assert document["workspace"]["base_branch"] == "trunk"
    assert document["workspace_overrides"] == ["base_branch"]
    assert document["branch"] == "ompire/legacy-task"
    assert document["accepted_at"] == "2026-09-01T00:00:00+00:00"


def test_0014_does_not_invent_a_binding_for_a_step_the_task_never_accepted(
    tmp_path: Path,
) -> None:
    """`bugfix` declares more agent steps than this document recorded. The
    migration converts what was accepted and adds nothing: a step introduced
    since acceptance was never reviewed for this task."""
    db_path = tmp_path / "ompire.db"
    _land_at_0013(db_path)
    engine = make_engine(db_path)
    with engine.begin() as conn:
        _insert_project(conn, "legacy")
        _insert_pinned_task(
            conn, "partial", _v1_document(step_roles={"reproduce": "default"})
        )

    _upgrade_to_0014(db_path)

    with engine.connect() as conn:
        document = _pinned(conn, "partial")

    assert set(document["step_bindings"]) == {"reproduce"}
    assert "validate-agent" not in document["step_bindings"]


def test_0014_reads_the_task_document_not_the_live_profile(tmp_path: Path) -> None:
    """A profile row that shares the pinned name must not supply values. The
    task's own snapshot is what governs it, which is exactly what lets a
    profile be edited or deleted without touching an accepted task."""
    db_path = tmp_path / "ompire.db"
    _land_at_0013(db_path)
    engine = make_engine(db_path)
    with engine.begin() as conn:
        _insert_project(conn, "legacy")
        conn.execute(
            text(
                "INSERT INTO model_profiles (name, roles_json, created_at, updated_at) "
                "VALUES ('retired-profile', :roles, '2026-09-01T00:00:00+00:00', "
                "'2026-09-01T00:00:00+00:00')"
            ),
            {
                "roles": (
                    '{"default": {"model": "vendor/EDITED", "thinking": "off"}, '
                    '"smol": {"model": "vendor/EDITED", "thinking": "off"}, '
                    '"slow": {"model": "vendor/EDITED", "thinking": "off"}, '
                    '"plan": {"model": "vendor/EDITED", "thinking": "off"}}'
                )
            },
        )
        _insert_pinned_task(conn, "snapshot", _v1_document())

    _upgrade_to_0014(db_path)

    with engine.connect() as conn:
        document = _pinned(conn, "snapshot")

    assert "EDITED" not in str(document)
    assert document["step_bindings"]["fix"]["roles"]["default"]["model"] == "vendor/main"


def test_0014_gives_resumable_sessions_a_continuation_policy(tmp_path: Path) -> None:
    """A resume needs a complete policy, and nothing recorded what these
    sessions ran under. The task's own pinned map supplies one — the stored
    judge role for `judge`, `default` for the rest — labelled as derived
    rather than as evidence about turns already taken."""
    db_path = tmp_path / "ompire.db"
    _land_at_0013(db_path)
    engine = make_engine(db_path)
    with engine.begin() as conn:
        _insert_project(conn, "legacy")
        _insert_pinned_task(conn, "resumable", _v1_document())
        task_id = conn.execute(
            text("SELECT id FROM tasks WHERE slug = 'resumable'")
        ).scalar_one()
        _insert_session(conn, task_id, "coder", "sess-coder")
        _insert_session(conn, task_id, "judge", "sess-judge")
        # Never captured an identity, so `--resume` cannot bring it back and
        # there is nothing for a continuation policy to continue.
        _insert_session(conn, task_id, "reproducer", None)

    _upgrade_to_0014(db_path)

    from ompire_daemon.registry.sessions import list_sessions

    sessions = {s.name: s for s in list_sessions(engine, task_id)}

    coder = sessions["coder"].applied_policy
    assert coder is not None
    assert coder.origin == "migrated"
    assert not coder.verified
    # No declared consumer applied this; the upgrade derived it.
    assert (coder.consumer_kind, coder.consumer_name) == (None, None)
    assert coder.policy.active.model == "vendor/main"
    assert coder.policy.slow.model == "vendor/big"

    judge = sessions["judge"].applied_policy
    assert judge is not None
    # The judge's own stored role, not `default` — the bug a single task-wide
    # policy could not see.
    assert judge.role == "slow"
    assert judge.policy.active.model == "vendor/big"

    assert sessions["reproducer"].applied_policy is None


def test_0014_leaves_an_unpinned_task_unpinned(tmp_path: Path) -> None:
    """A task with no recorded inputs gains none. It keeps its existing
    explicit-reconciliation requirement rather than being handed a policy."""
    db_path = tmp_path / "ompire.db"
    _land_at_0013(db_path)
    engine = make_engine(db_path)
    with engine.begin() as conn:
        _insert_project(conn, "legacy")
        _insert_pinned_task(conn, "unconfirmed", None)
        task_id = conn.execute(
            text("SELECT id FROM tasks WHERE slug = 'unconfirmed'")
        ).scalar_one()
        _insert_session(conn, task_id, "main", "sess-main")

    _upgrade_to_0014(db_path)

    with engine.connect() as conn:
        raw = conn.execute(
            text("SELECT execution_inputs_json FROM tasks WHERE slug = 'unconfirmed'")
        ).scalar_one()
        applied = conn.execute(
            text(
                "SELECT applied_policy_json FROM task_sessions WHERE task_id = :id"
            ),
            {"id": task_id},
        ).scalar_one()
    assert raw is None
    assert applied is None


def test_a_migrated_task_decodes_and_executes_without_its_source_profile(
    tmp_path: Path,
) -> None:
    """The point of the whole conversion chain: a task written by a much older
    daemon reads, at head, with a complete policy per consumer and no profile
    row in the registry at all."""
    db_path = tmp_path / "ompire.db"
    _land_at_0013(db_path)
    engine = make_engine(db_path)
    with engine.begin() as conn:
        _insert_project(conn, "legacy")
        _insert_pinned_task(conn, "decodable", _v1_document())

    upgrade_head(db_path, alembic_ini=REAL_ALEMBIC_INI)

    from ompire_daemon.execution_inputs import ModelPolicy
    from ompire_daemon.registry.tasks import get_task

    with engine.connect() as conn:
        task_id = conn.execute(
            text("SELECT id FROM tasks WHERE slug = 'decodable'")
        ).scalar_one()
        profiles = conn.execute(text("SELECT count(*) FROM model_profiles")).scalar_one()
    assert profiles == 0

    inputs = get_task(engine, task_id).execution_inputs
    assert inputs is not None
    policy = ModelPolicy.for_step(inputs, "fix")
    assert policy.active.model == "vendor/main"
    assert policy.plan.model == "vendor/planner"
    # No workflow was ever accepted for this task, and the upgrade did not
    # invent one.
    assert inputs.workflow_binding is None


def test_0014_downgrade_restores_the_version_1_shape(tmp_path: Path) -> None:
    """A task whose consumers all share one profile round-trips. One whose
    consumers disagree cannot: version 1 has nowhere to put the difference, so
    it is left alone rather than silently flattened to one of them."""
    from alembic import command

    db_path = tmp_path / "ompire.db"
    _land_at_0013(db_path)
    engine = make_engine(db_path)
    with engine.begin() as conn:
        _insert_project(conn, "legacy")
        _insert_pinned_task(conn, "uniform", _v1_document())

    _upgrade_to_0014(db_path)

    # Give a second task genuinely divergent per-consumer profiles.
    import json as _json

    with engine.begin() as conn:
        document = _pinned(conn, "uniform")
        divergent = _json.loads(_json.dumps(document))
        divergent["step_bindings"]["fix"]["profile_name"] = "other"
        divergent["step_bindings"]["fix"]["roles"] = {
            role: {"model": "other/model", "thinking": "low"}
            for role in ("default", "smol", "slow", "plan")
        }
        conn.execute(
            text(
                "INSERT INTO tasks (project_name, slug, branch, clone_path, state, "
                "prompt, workflow_name, execution_inputs_json, created_at, updated_at) "
                "VALUES ('legacy', 'divergent', 'ompire/divergent', '/tmp/d', 'created', "
                "'p', 'bugfix', :doc, '2026-09-01T00:00:00+00:00', "
                "'2026-09-01T00:00:00+00:00')"
            ),
            {"doc": _json.dumps(divergent)},
        )

    command.downgrade(_alembic_cfg(db_path), "0013")

    with engine.connect() as conn:
        uniform = _pinned(conn, "uniform")
        divergent_after = _pinned(conn, "divergent")
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(task_sessions)"))}

    assert "applied_policy_json" not in columns
    assert uniform["version"] == 1
    assert uniform["roles"] == _V1_ROLES
    assert uniform["step_roles"] == {"fix": "default", "reproduce": "default"}
    assert uniform["judge_role"] == "slow"
    # Refused rather than flattened; a version-1 daemon refuses it explicitly.
    assert divergent_after["version"] == 2


# --- 0015: retained workflow revisions and honest continuation (ADR-0028) ----


def _land_at_0014(db_path: Path) -> None:
    from alembic import command

    command.upgrade(_alembic_cfg(db_path), "0014")


def _v2_document(**overrides) -> str:
    """What a task accepted by the previous daemon actually stored: per-consumer
    bindings including the engine's auxiliary judge, and no definition."""
    import json as _json

    def binding(role: str) -> dict:
        return {
            "profile_name": "retired-profile",
            "profile_source": "task",
            "role": role,
            "role_source": "workflow",
            "roles": _V1_ROLES,
        }

    document = {
        "version": 2,
        "provenance": "accepted",
        "accepted_at": "2026-09-01T00:00:00+00:00",
        "project_name": "legacy",
        "workflow_name": "bugfix",
        "model_profile_name": "retired-profile",
        "model_profile_source": "task",
        "step_bindings": {
            name: binding("default")
            for name in ("reproduce", "fix", "validate-agent")
        },
        "auxiliary_bindings": {"judge": binding("slow")},
        "workspace": {
            "base_branch": "trunk",
            "branch_pattern": "ompire/<slug>",
            "workshop_additions": "project",
            "preamble": "house style",
        },
        "workspace_overrides": ["base_branch"],
        "branch": "ompire/legacy-task",
        "checkout_path": "/tmp/legacy",
        "fetch_remote": "origin",
        "upstream_url": "https://example.com/legacy.git",
        "fork_url": None,
        "unknown_inputs": [],
    }
    document.update(overrides)
    return _json.dumps(document)


def test_0015_leaves_the_workflow_binding_null_rather_than_inventing_one(
    tmp_path: Path,
) -> None:
    """The load-bearing refusal.

    Version 2 recorded a workflow *name*. What that name's prompts and routes
    said when the task ran is gone, so filling the binding in from whatever
    ships today would claim the task accepted a document it never saw. NULL is
    the honest value, and the operator confirms a continuation.
    """
    db_path = tmp_path / "ompire.db"
    _land_at_0014(db_path)
    engine = make_engine(db_path)
    with engine.begin() as conn:
        _insert_project(conn, "legacy")
        _insert_pinned_task(conn, "upgraded", _v2_document())

    upgrade_head(db_path, alembic_ini=REAL_ALEMBIC_INI)

    with engine.connect() as conn:
        document = _pinned(conn, "upgraded")
        retained = conn.execute(
            text("SELECT count(*) FROM workflow_revisions")
        ).scalar_one()

    # 0021 carries it forward to version 4 without inventing anything either.
    assert document["version"] == 4
    assert document["workflow_binding"] is None
    assert document["workflow_name"] == "bugfix"
    # A migration cannot know what the old definition said, so it retains none.
    assert retained == 0


def test_0015_preserves_the_retired_judge_binding_as_inert_evidence(
    tmp_path: Path,
) -> None:
    """The judge is removed from the live document and kept as history.

    The old model choice stays inspectable — it is the only record of what the
    task's judge would have run — while nothing can read it back into
    execution: version 3 has no auxiliary consumers at all.
    """
    db_path = tmp_path / "ompire.db"
    _land_at_0014(db_path)
    engine = make_engine(db_path)
    with engine.begin() as conn:
        _insert_project(conn, "legacy")
        _insert_pinned_task(conn, "judged", _v2_document())

    upgrade_head(db_path, alembic_ini=REAL_ALEMBIC_INI)

    import json as _json

    with engine.connect() as conn:
        document = _pinned(conn, "judged")
        task_id = conn.execute(
            text("SELECT id FROM tasks WHERE slug = 'judged'")
        ).scalar_one()
        rows = conn.execute(
            text(
                "SELECT kind, source, payload_json FROM launch_migration_evidence "
                "WHERE scope_kind = 'task' AND scope = :scope ORDER BY id"
            ),
            {"scope": str(task_id)},
        ).all()

    assert "auxiliary_bindings" not in document
    kinds = {kind for kind, _source, _payload in rows}
    assert kinds == {"legacy-execution-inputs", "retired-auxiliary-binding"}

    original = next(
        _json.loads(payload)
        for kind, _source, payload in rows
        if kind == "legacy-execution-inputs"
    )
    assert original["version"] == 2
    assert original["auxiliary_bindings"]["judge"]["role"] == "slow"

    judge = next(
        _json.loads(payload)
        for kind, _source, payload in rows
        if kind == "retired-auxiliary-binding"
    )
    assert judge["consumer"] == "judge"
    assert judge["binding"]["roles"]["slow"]["model"] == "vendor/big"


def test_0015_leaves_step_and_session_history_untouched(tmp_path: Path) -> None:
    """Everything that actually happened stays exactly as recorded, the
    retired judge session included."""
    db_path = tmp_path / "ompire.db"
    _land_at_0014(db_path)
    engine = make_engine(db_path)
    with engine.begin() as conn:
        _insert_project(conn, "legacy")
        _insert_pinned_task(conn, "historic", _v2_document())
        task_id = conn.execute(
            text("SELECT id FROM tasks WHERE slug = 'historic'")
        ).scalar_one()
        _insert_session(conn, task_id, "reproducer", "sess-repro")
        _insert_session(conn, task_id, "judge", "sess-judge")
        conn.execute(
            text(
                "INSERT INTO workflow_step_records "
                "(task_id, seq, step, kind, session, status, outcome_json, "
                "error, prompted_at, started_at, finished_at) VALUES "
                "(:task, 1, 'reproduce', 'agent', 'reproducer', 'ok', "
                "'{\"status\": \"success\"}', NULL, '2026-09-01T00:00:00+00:00', "
                "'2026-09-01T00:00:00+00:00', '2026-09-01T00:01:00+00:00')"
            ),
            {"task": task_id},
        )

    upgrade_head(db_path, alembic_ini=REAL_ALEMBIC_INI)

    with engine.connect() as conn:
        records = conn.execute(
            text(
                "SELECT seq, step, status, outcome_json, pause_json FROM "
                "workflow_step_records WHERE task_id = :task"
            ),
            {"task": task_id},
        ).all()
        sessions = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM task_sessions WHERE task_id = :task"),
                {"task": task_id},
            )
        }

    assert sessions == {"reproducer", "judge"}
    assert records == [(1, "reproduce", "ok", '{"status": "success"}', None)]


def test_0015_downgrade_restores_the_judge_binding_from_its_evidence(
    tmp_path: Path,
) -> None:
    """Down is an escape hatch, and it says so by what it cannot do.

    A task upgraded from version 2 round-trips, because its judge binding was
    kept. A task accepted *after* the upgrade never had one, so there is no
    honest version-2 shape for it — it is left at version 3 for an older
    daemon to refuse explicitly rather than run with a fabricated judge.
    """
    from alembic import command

    db_path = tmp_path / "ompire.db"
    _land_at_0014(db_path)
    engine = make_engine(db_path)
    with engine.begin() as conn:
        _insert_project(conn, "legacy")
        _insert_pinned_task(conn, "upgraded", _v2_document())

    upgrade_head(db_path, alembic_ini=REAL_ALEMBIC_INI)

    import json as _json

    with engine.begin() as conn:
        native = _json.loads(_pinned_raw(conn, "upgraded"))
        native.pop("auxiliary_bindings", None)
        native["workflow_binding"] = {
            "revision": "sha256:" + "0" * 64,
            "source": "accepted",
            "bound_at": "2026-09-06T00:00:00+00:00",
            "legacy_through_seq": 0,
            "interrupted_legacy_seq": None,
        }
        conn.execute(
            text(
                "INSERT INTO tasks (project_name, slug, branch, clone_path, state, "
                "prompt, workflow_name, execution_inputs_json, created_at, updated_at) "
                "VALUES ('legacy', 'native', 'ompire/native', '/tmp/n', 'created', "
                "'p', 'bugfix', :doc, '2026-09-06T00:00:00+00:00', "
                "'2026-09-06T00:00:00+00:00')"
            ),
            {"doc": _json.dumps(native)},
        )

    command.downgrade(_alembic_cfg(db_path), "0014")

    with engine.connect() as conn:
        upgraded = _pinned(conn, "upgraded")
        native_after = _pinned(conn, "native")
        columns = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(workflow_step_records)"))
        }
        tables = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'table'")
            )
        }

    assert "pause_json" not in columns
    assert "workflow_revisions" not in tables
    assert upgraded["version"] == 2
    assert upgraded["auxiliary_bindings"]["judge"]["role"] == "slow"
    assert "workflow_binding" not in upgraded
    assert native_after["version"] == 3


def _pinned_raw(conn, slug: str) -> str:
    return conn.execute(
        text("SELECT execution_inputs_json FROM tasks WHERE slug = :slug"),
        {"slug": slug},
    ).scalar_one()


# --- 0016: recorded evidence and named endings (ADR-0029, ADR-0030) ----------


def _land_at_0015(db_path: Path) -> None:
    from alembic import command

    command.upgrade(_alembic_cfg(db_path), "0015")


def test_0016_leaves_existing_history_unrecorded_rather_than_backfilled(
    tmp_path: Path,
) -> None:
    """The load-bearing refusal, again.

    A pre-upgrade attempt froze no evidence and a pre-upgrade run declared no
    ending. NULL says exactly that. An empty binding map would claim the
    attempt looked at nothing, and a terminal result inferred from `complete`
    would claim a run reported an outcome it never had a vocabulary for.
    """
    db_path = tmp_path / "ompire.db"
    _land_at_0015(db_path)
    engine = make_engine(db_path)
    with engine.begin() as conn:
        _insert_project(conn, "legacy")
        _insert_pinned_task(conn, "historic", _v2_document())
        task_id = conn.execute(
            text("SELECT id FROM tasks WHERE slug = 'historic'")
        ).scalar_one()
        conn.execute(
            text(
                "UPDATE tasks SET workflow_status = 'complete', "
                "workflow_step = NULL WHERE id = :task"
            ),
            {"task": task_id},
        )
        conn.execute(
            text(
                "INSERT INTO workflow_step_records "
                "(task_id, seq, step, kind, session, status, outcome_json, "
                "error, pause_json, prompted_at, started_at, finished_at) VALUES "
                "(:task, 1, 'reproduce', 'agent', 'reproducer', 'ok', "
                "'{\"status\": \"success\"}', NULL, NULL, "
                "'2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00', "
                "'2026-09-01T00:01:00+00:00')"
            ),
            {"task": task_id},
        )

    upgrade_head(db_path, alembic_ini=REAL_ALEMBIC_INI)

    with engine.connect() as conn:
        step_columns = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(workflow_step_records)"))
        }
        task_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(tasks)"))}
        record = conn.execute(
            text(
                "SELECT outcome_json, evidence_json FROM workflow_step_records "
                "WHERE task_id = :task AND seq = 1"
            ),
            {"task": task_id},
        ).one()
        result, status = conn.execute(
            text("SELECT workflow_result, workflow_status FROM tasks WHERE id = :task"),
            {"task": task_id},
        ).one()

    assert "evidence_json" in step_columns
    assert "workflow_result" in task_columns
    # The attempt's own evidence is untouched, and it gained no bindings.
    assert record == ('{"status": "success"}', None)
    # The run is still complete, and still says nothing about what that meant.
    assert (status, result) == ("complete", None)


def test_0016_downgrade_drops_only_the_two_new_columns(tmp_path: Path) -> None:
    from alembic import command

    db_path = tmp_path / "ompire.db"
    _land_at_0015(db_path)
    engine = make_engine(db_path)
    with engine.begin() as conn:
        _insert_project(conn, "legacy")
        _insert_pinned_task(conn, "historic", _v2_document())

    upgrade_head(db_path, alembic_ini=REAL_ALEMBIC_INI)
    command.downgrade(_alembic_cfg(db_path), "0015")

    with engine.connect() as conn:
        version = conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()
        step_columns = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(workflow_step_records)"))
        }
        task_columns = {row[1] for row in conn.execute(text("PRAGMA table_info(tasks)"))}
        slug = conn.execute(text("SELECT slug FROM tasks")).scalar_one()

    assert version == "0015"
    assert "evidence_json" not in step_columns
    assert "workflow_result" not in task_columns
    # Everything 0015 owns survives the round trip.
    assert "pause_json" in step_columns
    assert slug == "historic"


def test_0018_adds_the_delivery_journal_without_inventing_history(tmp_path: Path) -> None:
    """0018 creates the delivery tables empty and binds reviews to nothing.

    A task that shipped before content binding keeps its `pr_url` and its review
    iterations, and gains no delivery record: the projection then says "this was
    published, and there is no journal behind it", which is exactly true. Making
    one up would be fabricated provenance.
    """
    db_path = tmp_path / "ompire.db"
    _land_at_0007_with_tasks(db_path)
    engine = make_engine(db_path)

    from alembic import command

    command.upgrade(_alembic_cfg(db_path), "0017")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO reviews (task_id, status, process_started_at, "
                "created_at, updated_at) VALUES (1, 'approved', NULL, 't', 't')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO review_iterations (task_id, seq, outcome, "
                "comment_count, stderr, recorded_at) "
                "VALUES (1, 1, 'approved', 0, NULL, 't')"
            )
        )
        conn.execute(text("UPDATE tasks SET pr_url = 'https://x/pull/1' WHERE id = 1"))

    upgrade_head(db_path, alembic_ini=REAL_ALEMBIC_INI)

    with engine.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
        assert {
            "delivery_candidates",
            "deliveries",
            "delivery_actions",
            "delivery_decisions",
        } <= tables
        for table in ("delivery_candidates", "deliveries", "delivery_actions"):
            assert conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one() == 0
        # Existing history is preserved and left unbound.
        assert (
            conn.execute(
                text("SELECT candidate_id FROM reviews WHERE task_id = 1")
            ).scalar_one()
            is None
        )
        assert (
            conn.execute(
                text("SELECT candidate_id FROM review_iterations WHERE task_id = 1")
            ).scalar_one()
            is None
        )
        assert (
            conn.execute(text("SELECT pr_url FROM tasks WHERE id = 1")).scalar_one()
            == "https://x/pull/1"
        )

    command.downgrade(_alembic_cfg(db_path), "0017")
    with engine.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
        assert "deliveries" not in tables
        # The publication fact and the review history outlive the journal.
        assert (
            conn.execute(text("SELECT pr_url FROM tasks WHERE id = 1")).scalar_one()
            == "https://x/pull/1"
        )
        assert (
            conn.execute(text("SELECT COUNT(*) FROM review_iterations")).scalar_one()
            == 1
        )


def test_0019_marks_pre_upgrade_grants_without_inventing_run_links(
    tmp_path: Path,
) -> None:
    """0019 records where history ends and adds nothing to it.

    A delivery authorized through the old Ship page really was not granted by
    a workflow decision, so its new run links stay NULL. What the migration
    does add is the *boundary*: that delivery's id is at or below it, so it
    can still be continued under the grant it genuinely has, while any row
    created afterwards has to carry its own authority no matter what its
    links say.
    """
    db_path = tmp_path / "ompire.db"
    _land_at_0007_with_tasks(db_path)
    engine = make_engine(db_path)

    from alembic import command

    command.upgrade(_alembic_cfg(db_path), "0018")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO deliveries (id, task_id, version, ending, mode, "
                "authorized_at, disposition, created_at, updated_at) VALUES "
                "(7, 1, 1, 'push', 'squash', 't', 'authorized', 't', 't')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO delivery_actions (id, delivery_id, seq, kind, "
                "attempt, request_key, input_fingerprint, phase, created_at, "
                "updated_at) VALUES "
                "(3, 7, 1, 'commit', 1, 'r', 'f', 'succeeded', 't', 't')"
            )
        )

    upgrade_head(db_path, alembic_ini=REAL_ALEMBIC_INI)

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT workflow_gate_seq, workflow_choice_id, review_seq "
                "FROM deliveries WHERE id = 7"
            )
        ).one()
        assert row == (None, None, None)
        assert (
            conn.execute(
                text("SELECT workflow_seq FROM delivery_actions WHERE id = 3")
            ).scalar_one()
            is None
        )
        boundary = conn.execute(
            text(
                "SELECT id, max_delivery_id, max_action_id "
                "FROM delivery_authority_boundary"
            )
        ).all()
        assert boundary == [(1, 7, 3)]

    # A delivery created after the upgrade sits above the boundary, so an
    # unset link can never read as a pre-upgrade grant.
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO deliveries (task_id, version, disposition, "
                "created_at, updated_at) VALUES (1, 2, 'open', 't', 't')"
            )
        )
        assert (
            conn.execute(text("SELECT MAX(id) FROM deliveries")).scalar_one() > 7
        )

    command.downgrade(_alembic_cfg(db_path), "0018")
    with engine.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
        assert "delivery_authority_boundary" not in tables
        # The authorization itself outlives the boundary that classified it.
        assert (
            conn.execute(
                text("SELECT ending FROM deliveries WHERE id = 7")
            ).scalar_one()
            == "push"
        )


def test_0020_adds_results_without_inventing_any(tmp_path: Path) -> None:
    """An existing task keeps its history and gains *no* result.

    A task that ran before durable results existed genuinely produced none.
    Reconstructing one from its outcome text, its clone, or its last workflow
    step would manufacture exactly the provenance ADR-0034 exists to keep
    honest — so the upgrade adds storage and nothing else.
    """
    from alembic import command

    db_path = tmp_path / "ompire.db"
    command.upgrade(_alembic_cfg(db_path), "0019")
    engine = make_engine(db_path)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO projects (name, title, upstream_url, fork_url, "
                "checkout_path) VALUES ('demo', 'Demo', "
                "'https://example.com/demo', NULL, '/tmp/demo')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO tasks (id, project_name, slug, branch, clone_path, "
                "state, prompt, created_at, updated_at) VALUES (1, 'demo', "
                "'legacy', 'ompire/legacy', '/tmp/clone', 'archived', 'explore', "
                "'t', 't')"
            )
        )

    upgrade_head(db_path, alembic_ini=REAL_ALEMBIC_INI)

    with engine.connect() as conn:
        assert (
            conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            == "0023"
        )
        assert conn.execute(text("SELECT COUNT(*) FROM task_results")).scalar_one() == 0
        row = conn.execute(
            text("SELECT slug, state, results_version FROM tasks WHERE id = 1")
        ).one()
    assert row.slug == "legacy"
    assert row.state == "archived"
    # A task with no results is at version 0, not at some inherited counter.
    assert row.results_version == 0


def test_0021_carries_pinned_inputs_forward_without_inventing_a_source_commit(
    tmp_path: Path,
) -> None:
    """The load-bearing refusal of this migration.

    A task accepted before attachments existed was built from *some* commit,
    and nobody recorded which. Filling `source_commit` in from today's branch
    head would claim the task was reviewed against a base that may have moved
    many times since. Null is the honest value, and no attachment reference is
    invented for a task that never had one.
    """
    db_path = tmp_path / "ompire.db"
    _land_at_0014(db_path)
    engine = make_engine(db_path)
    with engine.begin() as conn:
        _insert_project(conn, "legacy")
        _insert_pinned_task(conn, "upgraded", _v2_document())

    upgrade_head(db_path, alembic_ini=REAL_ALEMBIC_INI)

    with engine.connect() as conn:
        document = _pinned(conn, "upgraded")
        references = conn.execute(
            text("SELECT count(*) FROM task_result_references")
        ).scalar_one()

    assert document["version"] == 4
    assert document["result_attachments"] == []
    assert document["source_commit"] is None
    assert document["base_comparisons"] == []
    assert document["acknowledged_base_difference"] is False
    assert references == 0


def test_0021_leaves_a_task_with_no_pinned_inputs_null(tmp_path: Path) -> None:
    """NULL means "no launch was ever accepted here". The upgrade must not
    turn that into an empty accepted document, which would read as a task that
    was reviewed and simply had no attachments."""
    db_path = tmp_path / "ompire.db"
    _land_at_0014(db_path)
    engine = make_engine(db_path)
    with engine.begin() as conn:
        _insert_project(conn, "legacy")
        _insert_pinned_task(conn, "unpinned", None)

    upgrade_head(db_path, alembic_ini=REAL_ALEMBIC_INI)

    with engine.connect() as conn:
        raw = conn.execute(
            text("SELECT execution_inputs_json FROM tasks WHERE slug = 'unpinned'")
        ).scalar_one()
    assert raw is None


def test_0022_adds_the_export_journal_without_inventing_history(
    tmp_path: Path,
) -> None:
    """A result retained before checkout export existed has no export history.

    That is the fact, not an unknown outcome to reconcile: nothing was ever
    written into the operator's checkout on its behalf, and a backfilled row
    would be a claim about files this daemon never touched.
    """
    db_path = tmp_path / "ompire.db"
    _land_at_0014(db_path)
    engine = make_engine(db_path)
    with engine.begin() as conn:
        _insert_project(conn, "legacy")
        _insert_pinned_task(conn, "legacy", _v2_document())

    upgrade_head(db_path, alembic_ini=REAL_ALEMBIC_INI)

    with engine.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
        }
        indexes = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND tbl_name = 'result_exports'"
                )
            )
        }
        exports = conn.execute(
            text("SELECT count(*) FROM result_exports")
        ).scalar_one()
        files = conn.execute(
            text("SELECT count(*) FROM result_export_files")
        ).scalar_one()

    assert {"result_exports", "result_export_files"} <= tables
    # The durable root reservation, which is what stops two project
    # registrations aliasing one directory from installing into it at once.
    assert "uq_result_exports_active_root" in indexes
    assert "uq_result_exports_request" in indexes
    assert exports == 0
    assert files == 0
