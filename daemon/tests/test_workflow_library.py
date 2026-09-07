"""The workflow library: creating, editing, validating, saving, exporting, and
archiving entries an operator owns (ADR-0031).

The line these tests hold is the one between the mutable library and the
append-only revisions underneath it: a draft is inert text, only an explicit
executable save changes what a name launches, and nothing here can reach a
task that has already accepted a revision.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy import text as sa_text

from ompire_daemon.db import db_path_for, make_engine
from ompire_daemon.migrate import upgrade_head
from ompire_daemon.registry.workflow_definitions import clear_cache, list_revisions
from ompire_daemon.registry.workflow_library import (
    ArchivedWorkflowError,
    BuiltinWorkflowReadOnlyError,
    DuplicateWorkflowNameError,
    InvalidWorkflowNameError,
    UnknownWorkflowNameError,
    WorkflowDraftTooLargeError,
    WorkflowNameMismatchError,
    WorkflowNotLaunchableError,
    WorkflowVersionConflictError,
    create_entry,
    get_detail,
    launchable_descriptors,
    list_entries,
    resolve_current,
    save_draft,
    save_revision,
    set_archived,
    synchronize_builtins,
)
from ompire_daemon.workflow_definitions import MAX_DOCUMENT_BYTES, load_definition
from ompire_daemon.workflows import load_packaged_workflows
from tests.conftest import register_builtin_workflows

MINIMAL = """
format: 1
name: {name}
sessions: [main]
primary: main
steps:
  - name: work
    kind: agent
    session: main
    prompt:
      parts:
        - text: "{body}"
"""


def minimal(name: str = "custom", body: str = "do it") -> str:
    return MINIMAL.format(name=name, body=body)


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = db_path_for(data_dir)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    upgrade_head(db_path)
    engine = make_engine(db_path)
    register_builtin_workflows(engine)
    return engine


def version_of(engine: Engine, name: str) -> int:
    return get_detail(engine, name).entry.version


# --- built-in synchronization -------------------------------------------------


def test_startup_installs_the_packaged_definitions_as_readonly_entries(
    engine: Engine,
) -> None:
    entries = {entry.name: entry for entry in list_entries(engine)}
    assert set(entries) == {"bugfix", "single-step"}
    assert all(entry.origin == "builtin" for entry in entries.values())
    # A built-in carries no draft: its text is in the package, not the database.
    assert all(not entry.has_draft for entry in entries.values())
    assert all(entry.available for entry in entries.values())
    assert [d.name for d in launchable_descriptors(list(entries.values()))] == [
        "bugfix",
        "single-step",
    ]


def test_synchronizing_again_is_idempotent(engine: Engine) -> None:
    """Every start re-installs the packaged set. An unchanged package must not
    invent a revision or bump an entry's edit version."""
    before = {entry.name: entry.version for entry in list_entries(engine)}
    register_builtin_workflows(engine)
    after = {entry.name: entry.version for entry in list_entries(engine)}
    assert before == after


def test_a_package_upgrade_moves_the_builtin_and_keeps_the_old_revision(
    engine: Engine,
) -> None:
    """A later package changes only its own built-in selection.

    The revision the previous package shipped stays retained — an old task
    pins it, and what that task ran has to stay readable.
    """
    original = resolve_current_revision(engine, "single-step")
    upgraded = load_definition(minimal(name="single-step", body="a new instruction"))
    synchronize_builtins(engine, [upgraded])
    assert resolve_current_revision(engine, "single-step") == upgraded.revision
    retained = {r.revision for r in list_revisions(engine, workflow_name="single-step")}
    assert {original, upgraded.revision} <= retained


def test_a_builtin_the_package_dropped_stops_launching_but_keeps_its_history(
    engine: Engine,
) -> None:
    synchronize_builtins(engine, [load_packaged_workflows()["single-step"]])
    entry = next(e for e in list_entries(engine) if e.name == "bugfix")
    assert entry.available is False
    assert entry.unavailable_reason == "not_packaged"
    assert entry.descriptor is None
    # Nothing was deleted: the definition it used to ship is still retained.
    assert list_revisions(engine, workflow_name="bugfix")


def test_builtin_synchronization_refuses_to_overwrite_custom_work(
    engine: Engine,
) -> None:
    """A package introducing a name an operator already used is a collision,
    reported rather than resolved by overwriting — and never a reason to
    refuse to start, which would leave nobody able to rename it."""
    create_entry(engine, name="mine", yaml_text=minimal(name="mine"))
    conflicts = synchronize_builtins(engine, [load_definition(minimal(name="mine"))])
    assert [c.name for c in conflicts] == ["mine"]
    entry = next(e for e in list_entries(engine) if e.name == "mine")
    assert entry.origin == "custom"
    assert entry.current_revision is None


def resolve_current_revision(engine: Engine, name: str) -> str:
    with engine.connect() as conn:
        return resolve_current(conn, name).revision


# --- names and creation -------------------------------------------------------


def test_a_new_entry_is_a_draft_and_cannot_launch(engine: Engine) -> None:
    detail = create_entry(engine, name="custom", yaml_text=minimal())
    assert detail.entry.origin == "custom"
    assert detail.entry.current_revision is None
    assert detail.entry.available is False
    assert detail.entry.unavailable_reason == "draft_only"
    assert detail.draft_yaml == minimal()
    with engine.connect() as conn, pytest.raises(WorkflowNotLaunchableError) as exc:
        resolve_current(conn, "custom")
    assert exc.value.reason == "draft_only"


def test_names_are_validated_reserved_and_never_shadowed(engine: Engine) -> None:
    with pytest.raises(InvalidWorkflowNameError):
        create_entry(engine, name="Not A Slug", yaml_text=minimal())
    with pytest.raises(DuplicateWorkflowNameError) as exc:
        create_entry(engine, name="bugfix", yaml_text=minimal(name="bugfix"))
    assert exc.value.origin == "builtin"

    create_entry(engine, name="custom", yaml_text=minimal())
    with pytest.raises(DuplicateWorkflowNameError):
        create_entry(engine, name="custom", yaml_text=minimal())


def test_an_archived_name_stays_reserved(engine: Engine) -> None:
    """Archiving keeps the drafts, the revisions, and the tasks that ran them.
    Handing the name to a new procedure would make that history read as this
    one's."""
    create_entry(engine, name="custom", yaml_text=minimal())
    set_archived(
        engine, "custom", archived=True, expected_version=version_of(engine, "custom")
    )
    with pytest.raises(DuplicateWorkflowNameError) as exc:
        create_entry(engine, name="custom", yaml_text=minimal())
    assert exc.value.archived is True


# --- drafts are inert ---------------------------------------------------------


def test_a_draft_takes_any_text_and_never_displaces_the_current_revision(
    engine: Engine,
) -> None:
    create_entry(engine, name="custom", yaml_text=minimal())
    revision = load_definition(minimal())
    save_revision(
        engine,
        "custom",
        revision=revision,
        yaml_text=minimal(),
        expected_version=version_of(engine, "custom"),
    )
    for text in ("", "not: [valid", "!!python/object:os.system {}", "# just a comment"):
        detail = save_draft(
            engine,
            "custom",
            yaml_text=text,
            expected_version=version_of(engine, "custom"),
        )
        assert detail.draft_yaml == text
        # The saved procedure is untouched, and the entry still launches.
        assert detail.entry.current_revision == revision.revision
        assert detail.entry.available is True
    assert resolve_current_revision(engine, "custom") == revision.revision


def test_oversized_text_is_refused_without_mutating_anything(engine: Engine) -> None:
    create_entry(engine, name="custom", yaml_text=minimal())
    before = get_detail(engine, "custom")
    with pytest.raises(WorkflowDraftTooLargeError):
        save_draft(
            engine,
            "custom",
            yaml_text="x" * (MAX_DOCUMENT_BYTES + 1),
            expected_version=before.entry.version,
        )
    assert get_detail(engine, "custom") == before


def test_a_draft_survives_a_daemon_restart(engine: Engine, tmp_path: Path) -> None:
    create_entry(engine, name="custom", yaml_text="half a thought")
    reopened = make_engine(db_path_for(tmp_path / "data"))
    assert get_detail(reopened, "custom").draft_yaml == "half a thought"


# --- executable saves ---------------------------------------------------------


def test_an_executable_save_retains_selects_and_saves_its_text_together(
    engine: Engine,
) -> None:
    create_entry(engine, name="custom", yaml_text="draft")
    revision = load_definition(minimal())
    detail = save_revision(
        engine,
        "custom",
        revision=revision,
        yaml_text=minimal(),
        expected_version=version_of(engine, "custom"),
    )
    assert detail.entry.current_revision == revision.revision
    assert detail.entry.current_format == 1
    assert detail.entry.available is True
    assert detail.draft_yaml == minimal()
    assert [r.revision for r in detail.revisions] == [revision.revision]
    assert resolve_current_revision(engine, "custom") == revision.revision


def test_resaving_identical_semantics_reuses_the_content_revision(
    engine: Engine,
) -> None:
    """A comment is a real edit, and not a new version of the procedure.

    The entry's edit version advances — two tabs must not silently overwrite
    each other — while the content revision, which identifies what would
    execute, does not move.
    """
    create_entry(engine, name="custom", yaml_text=minimal())
    first = save_revision(
        engine,
        "custom",
        revision=load_definition(minimal()),
        yaml_text=minimal(),
        expected_version=version_of(engine, "custom"),
    )
    commented = "# a note for the next reader\n" + minimal()
    second = save_revision(
        engine,
        "custom",
        revision=load_definition(commented),
        yaml_text=commented,
        expected_version=version_of(engine, "custom"),
    )
    assert second.entry.current_revision == first.entry.current_revision
    assert second.entry.version > first.entry.version
    assert len(second.revisions) == 1


def test_a_document_cannot_rename_the_entry_it_is_saved_into(engine: Engine) -> None:
    create_entry(engine, name="custom", yaml_text=minimal())
    with pytest.raises(WorkflowNameMismatchError):
        save_revision(
            engine,
            "custom",
            revision=load_definition(minimal(name="something-else")),
            yaml_text=minimal(name="something-else"),
            expected_version=version_of(engine, "custom"),
        )
    assert get_detail(engine, "custom").entry.current_revision is None


def test_builtins_refuse_every_mutation(engine: Engine) -> None:
    version = version_of(engine, "single-step")
    with pytest.raises(BuiltinWorkflowReadOnlyError):
        save_draft(engine, "single-step", yaml_text="x", expected_version=version)
    with pytest.raises(BuiltinWorkflowReadOnlyError):
        save_revision(
            engine,
            "single-step",
            revision=load_definition(minimal(name="single-step")),
            yaml_text=minimal(name="single-step"),
            expected_version=version,
        )
    with pytest.raises(BuiltinWorkflowReadOnlyError):
        set_archived(
            engine, "single-step", archived=True, expected_version=version
        )


# --- lost updates -------------------------------------------------------------


def test_a_stale_write_changes_nothing_and_reports_the_current_version(
    engine: Engine,
) -> None:
    """Two tabs. The second one's version is the one it loaded, not the one
    the first committed, so its save is refused rather than applied over
    work it never saw."""
    create_entry(engine, name="custom", yaml_text="first")
    stale = version_of(engine, "custom")
    save_draft(engine, "custom", yaml_text="tab one", expected_version=stale)
    before = get_detail(engine, "custom")

    with pytest.raises(WorkflowVersionConflictError) as exc:
        save_draft(engine, "custom", yaml_text="tab two", expected_version=stale)
    assert exc.value.expected == stale
    assert exc.value.actual == before.entry.version
    assert get_detail(engine, "custom") == before

    with pytest.raises(WorkflowVersionConflictError):
        save_revision(
            engine,
            "custom",
            revision=load_definition(minimal()),
            yaml_text=minimal(),
            expected_version=stale,
        )
    with pytest.raises(WorkflowVersionConflictError):
        set_archived(engine, "custom", archived=True, expected_version=stale)
    assert get_detail(engine, "custom") == before


def test_a_comment_only_difference_still_conflicts(engine: Engine) -> None:
    """Comments do not change a content revision, so a library that compared
    revisions would call this an unchanged entry and let one tab silently
    overwrite the other."""
    create_entry(engine, name="custom", yaml_text=minimal())
    stale = version_of(engine, "custom")
    save_draft(engine, "custom", yaml_text="# tab one\n" + minimal(), expected_version=stale)
    with pytest.raises(WorkflowVersionConflictError):
        save_draft(
            engine, "custom", yaml_text="# tab two\n" + minimal(), expected_version=stale
        )


def test_a_failed_executable_save_rolls_back_the_whole_transaction(
    engine: Engine,
) -> None:
    """Retention and selection commit together or not at all: a restart cannot
    expose a retained document nothing selected, or a selection pointing at a
    document that was never written."""
    create_entry(engine, name="custom", yaml_text=minimal())
    revision = load_definition(minimal(body="never saved"))
    with pytest.raises(WorkflowVersionConflictError):
        save_revision(
            engine,
            "custom",
            revision=revision,
            yaml_text=minimal(body="never saved"),
            expected_version=999,
        )
    assert list_revisions(engine, workflow_name="custom") == []
    assert get_detail(engine, "custom").entry.current_revision is None


# --- archive and restore ------------------------------------------------------


def test_archiving_hides_an_entry_without_deleting_anything(engine: Engine) -> None:
    create_entry(engine, name="custom", yaml_text=minimal())
    revision = load_definition(minimal())
    save_revision(
        engine,
        "custom",
        revision=revision,
        yaml_text=minimal(),
        expected_version=version_of(engine, "custom"),
    )
    archived = set_archived(
        engine, "custom", archived=True, expected_version=version_of(engine, "custom")
    )
    assert archived.entry.archived is True
    assert archived.entry.available is False
    assert archived.entry.unavailable_reason == "archived"
    assert archived.entry.descriptor is None
    # Nothing is gone: the draft, the revision, and the pointer are all intact.
    assert archived.draft_yaml == minimal()
    assert archived.entry.current_revision == revision.revision
    assert [r.revision for r in archived.revisions] == [revision.revision]
    assert "custom" not in [
        d.name for d in launchable_descriptors(list_entries(engine))
    ]

    with pytest.raises(ArchivedWorkflowError):
        save_draft(
            engine, "custom", yaml_text="x", expected_version=version_of(engine, "custom")
        )

    restored = set_archived(
        engine, "custom", archived=False, expected_version=version_of(engine, "custom")
    )
    assert restored.entry.available is True
    assert resolve_current_revision(engine, "custom") == revision.revision


def test_restoring_a_draft_only_entry_leaves_it_draft_only(engine: Engine) -> None:
    create_entry(engine, name="custom", yaml_text=minimal())
    set_archived(
        engine, "custom", archived=True, expected_version=version_of(engine, "custom")
    )
    restored = set_archived(
        engine, "custom", archived=False, expected_version=version_of(engine, "custom")
    )
    assert restored.entry.available is False
    assert restored.entry.unavailable_reason == "draft_only"


# --- one damaged entry costs one entry ----------------------------------------


def test_a_damaged_current_revision_is_an_unavailable_entry_not_a_broken_library(
    engine: Engine,
) -> None:
    create_entry(engine, name="custom", yaml_text=minimal())
    revision = load_definition(minimal())
    save_revision(
        engine,
        "custom",
        revision=revision,
        yaml_text=minimal(),
        expected_version=version_of(engine, "custom"),
    )
    with engine.begin() as conn:
        conn.execute(
            sa_text("DELETE FROM workflow_revisions WHERE revision = :rev"),
            {"rev": revision.revision},
        )
    clear_cache()

    entries = {entry.name: entry for entry in list_entries(engine)}
    assert entries["custom"].available is False
    assert entries["custom"].unavailable_reason == "missing"
    # The built-ins are untouched, and so is the entry's recoverable text.
    assert entries["single-step"].available is True
    assert get_detail(engine, "custom").draft_yaml == minimal()

    with engine.connect() as conn, pytest.raises(WorkflowNotLaunchableError) as exc:
        resolve_current(conn, "custom")
    assert exc.value.reason == "missing"

    # A corrected executable save is the repair path.
    repaired = save_revision(
        engine,
        "custom",
        revision=load_definition(minimal(body="repaired")),
        yaml_text=minimal(body="repaired"),
        expected_version=version_of(engine, "custom"),
    )
    assert repaired.entry.available is True


def test_an_unknown_name_is_not_a_launchable_workflow(engine: Engine) -> None:
    with engine.connect() as conn, pytest.raises(UnknownWorkflowNameError):
        resolve_current(conn, "never-existed")
