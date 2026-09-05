"""Project registry: CRUD against the `projects` table. No ORM — Core queries only."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from sqlalchemy import Engine

from ompire_daemon.db import projects, tasks, templates
from ompire_daemon.registry.model_profiles import (
    require_profile_exists,
    reserved_write,
)

_SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class InvalidSlugError(ValueError):
    def __init__(self, name: str) -> None:
        super().__init__(f"invalid project name {name!r}: must be lowercase alphanumerics and hyphens")
        self.name = name


class DuplicateProjectError(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(f"project {name!r} already exists")
        self.name = name


class ProjectNotFoundError(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(f"project {name!r} not found")
        self.name = name


class ProjectHasReferencingTasksError(Exception):
    """409 detail for delete/rename guards: names referencing task rows
    (any state) and referencing templates (SPEC Decision 6 — no cascade)."""

    def __init__(
        self,
        name: str,
        task_labels: list[str] | None = None,
        template_names: list[str] | None = None,
    ) -> None:
        task_labels = task_labels or []
        template_names = template_names or []
        parts: list[str] = []
        if task_labels:
            parts.append(f"tasks: {', '.join(task_labels)}")
        if template_names:
            parts.append(f"templates: {', '.join(template_names)}")
        detail = f" ({'; '.join(parts)})" if parts else ""
        super().__init__(f"project {name!r} has tasks or templates referencing it{detail}")
        self.name = name
        self.task_labels = task_labels
        self.template_names = template_names


CHECKOUT_MODES = ("adopted", "cloned")
SETUP_STATES = ("ready", "cloning", "failed")

DEFAULT_FETCH_REMOTE = "origin"


class _Unsupplied(Enum):
    """Distinguishes "the caller said nothing about this field" from an
    explicit `None`. A project update that omits `default_model_profile` — as
    every API caller written before profiles existed does — must preserve the
    stored reference, while an explicit null clears it."""

    token = 0


UNSUPPLIED = _Unsupplied.token


class ProjectSetupBusyError(Exception):
    """Refusal for an operation that cannot run while a clone is in flight."""

    def __init__(self, name: str) -> None:
        super().__init__(f"project {name!r} is still being set up")
        self.name = name


class ProjectNotReadyError(Exception):
    """409 detail for spawn/template guards: the checkout is not usable yet."""

    def __init__(self, name: str, setup_state: str) -> None:
        super().__init__(
            f"project {name!r} is not ready (setup {setup_state}); "
            "finish or retry its checkout setup first"
        )
        self.name = name
        self.setup_state = setup_state


@dataclass(frozen=True)
class Project:
    name: str
    title: str
    upstream_url: str
    fork_url: str | None
    checkout_path: str
    # Onboarding facts (ADR-0022). `cloned` means Ompire created the checkout
    # and may remove a *staging* tree it owns; it never deletes either kind.
    checkout_mode: str = "adopted"
    fetch_remote: str = DEFAULT_FETCH_REMOTE
    setup_state: str = "ready"
    setup_error: str | None = None
    # The global model profile this project selects as its default, or None
    # (ADR-0025). A reference to policy, not a copy of it — and, in this
    # change, not yet consumed by task execution.
    default_model_profile: str | None = None


def validate_slug(name: str) -> None:
    if not _SLUG_RE.match(name):
        raise InvalidSlugError(name)


def _row_to_project(row) -> Project:
    return Project(
        name=row.name,
        title=row.title,
        upstream_url=row.upstream_url,
        fork_url=row.fork_url,
        checkout_path=row.checkout_path,
        checkout_mode=row.checkout_mode,
        fetch_remote=row.fetch_remote,
        setup_state=row.setup_state,
        setup_error=row.setup_error,
        default_model_profile=row.default_model_profile,
    )


def list_projects(engine: Engine) -> list[Project]:
    with engine.connect() as conn:
        rows = conn.execute(projects.select().order_by(projects.c.name)).all()
    return [_row_to_project(row) for row in rows]


def get_project(engine: Engine, name: str) -> Project:
    with engine.connect() as conn:
        row = conn.execute(projects.select().where(projects.c.name == name)).first()
    if row is None:
        raise ProjectNotFoundError(name)
    return _row_to_project(row)


def create_project(
    engine: Engine,
    *,
    name: str,
    title: str,
    upstream_url: str,
    fork_url: str | None = None,
    checkout_path: str | None = None,
    default_checkout_root: Path,
    checkout_mode: str = "adopted",
    fetch_remote: str = DEFAULT_FETCH_REMOTE,
    setup_state: str = "ready",
    default_model_profile: str | None = None,
) -> Project:
    validate_slug(name)
    resolved_checkout_path = checkout_path or str(default_checkout_root / name)
    # The duplicate check, the profile reference check, and the insert share
    # one write reservation, so a profile cannot be deleted between being
    # validated here and being pinned on the committed row.
    with reserved_write(engine) as conn:
        clash = conn.execute(
            projects.select()
            .with_only_columns(projects.c.name)
            .where(projects.c.name == name)
        ).first()
        if clash is not None:
            raise DuplicateProjectError(name)
        if default_model_profile is not None:
            require_profile_exists(conn, default_model_profile)
        conn.execute(
            projects.insert().values(
                name=name,
                title=title,
                upstream_url=upstream_url,
                fork_url=fork_url,
                checkout_path=resolved_checkout_path,
                checkout_mode=checkout_mode,
                fetch_remote=fetch_remote,
                setup_state=setup_state,
                setup_error=None,
                default_model_profile=default_model_profile,
            )
        )
        # Read the committed row back inside the reservation: the caller's
        # response is this mutation's own outcome, not whatever a later write
        # leaves behind.
        row = conn.execute(projects.select().where(projects.c.name == name)).one()
    return _row_to_project(row)


def update_project(
    engine: Engine,
    name: str,
    *,
    title: str,
    upstream_url: str,
    fork_url: str | None,
    checkout_path: str,
    fetch_remote: str = DEFAULT_FETCH_REMOTE,
    new_name: str | None = None,
    default_model_profile: str | None | _Unsupplied = UNSUPPLIED,
) -> Project:
    """Update a project's editable fields.

    `default_model_profile` is three-valued: omitted preserves the stored
    reference, `None` clears it, and a name selects that profile. The stored
    value is read inside the write reservation rather than from any earlier
    read, so an omission preserves what is actually committed.
    """
    rename = new_name is not None and new_name != name
    if rename:
        assert new_name is not None  # rename implies it differs from name
        validate_slug(new_name)
        # Same guard as delete: renaming the PK under referencing rows would
        # orphan them — refused, no cascade.
        _raise_if_referenced(engine, name)
    values: dict[str, str | None] = {
        "title": title,
        "upstream_url": upstream_url,
        "fork_url": fork_url,
        "checkout_path": checkout_path,
        "fetch_remote": fetch_remote,
    }
    if rename:
        values["name"] = new_name
    with reserved_write(engine) as conn:
        current = conn.execute(projects.select().where(projects.c.name == name)).first()
        if current is None:
            raise ProjectNotFoundError(name)
        if rename:
            assert new_name is not None
            clash = conn.execute(
                projects.select()
                .with_only_columns(projects.c.name)
                .where(projects.c.name == new_name)
            ).first()
            if clash is not None:
                raise DuplicateProjectError(new_name)
        if isinstance(default_model_profile, _Unsupplied):
            values["default_model_profile"] = current.default_model_profile
        else:
            if default_model_profile is not None:
                require_profile_exists(conn, default_model_profile)
            values["default_model_profile"] = default_model_profile
        conn.execute(projects.update().where(projects.c.name == name).values(**values))
        row = conn.execute(
            projects.select().where(
                projects.c.name == (new_name if rename else name)
            )
        ).one()
    return _row_to_project(row)


def _referencing_task_labels(engine: Engine, name: str) -> list[str]:
    # Any referencing row blocks removal, archived included: purging archived
    # tasks (DELETE /api/tasks/{id}) is the way to unblock. Keeps the FK honest.
    with engine.connect() as conn:
        rows = conn.execute(
            tasks.select()
            .with_only_columns(tasks.c.slug, tasks.c.state)
            .where(tasks.c.project_name == name)
            .order_by(tasks.c.id)
        ).all()
    return [f"{name}/{row.slug} ({row.state})" for row in rows]


def _referencing_template_names(engine: Engine, name: str) -> list[str]:
    # Templates referencing this project block delete/rename too (SPEC
    # Decision 6); deleting or repointing them unblocks. No cascade.
    with engine.connect() as conn:
        rows = conn.execute(
            templates.select()
            .with_only_columns(templates.c.name)
            .where(templates.c.project_name == name)
            .order_by(templates.c.name)
        ).all()
    return [row.name for row in rows]


def _raise_if_referenced(engine: Engine, name: str) -> None:
    labels = _referencing_task_labels(engine, name)
    template_names = _referencing_template_names(engine, name)
    if labels or template_names:
        raise ProjectHasReferencingTasksError(name, labels, template_names)


def list_setup_pending(engine: Engine) -> list[Project]:
    """Projects whose clone was still running when the daemon stopped."""
    with engine.connect() as conn:
        rows = conn.execute(
            projects.select()
            .where(projects.c.setup_state == "cloning")
            .order_by(projects.c.name)
        ).all()
    return [_row_to_project(row) for row in rows]


def _set_setup(
    engine: Engine, name: str, *, state: str, error: str | None
) -> Project:
    assert state in SETUP_STATES
    with engine.begin() as conn:
        result = conn.execute(
            projects.update()
            .where(projects.c.name == name)
            .values(setup_state=state, setup_error=error)
        )
        if result.rowcount == 0:
            raise ProjectNotFoundError(name)
    return get_project(engine, name)


def mark_setup_cloning(engine: Engine, name: str) -> Project:
    """Arm a (re)try: clear the previous error so a retry cannot show a stale one."""
    return _set_setup(engine, name, state="cloning", error=None)


def mark_setup_ready(engine: Engine, name: str) -> Project:
    return _set_setup(engine, name, state="ready", error=None)


def mark_setup_failed(engine: Engine, name: str, error: str) -> Project:
    return _set_setup(engine, name, state="failed", error=error)


def delete_project(engine: Engine, name: str) -> None:
    # A clone job holds a staging tree and will write this row when it ends;
    # deleting the row underneath it would orphan both (ADR-0022).
    if get_project(engine, name).setup_state == "cloning":
        raise ProjectSetupBusyError(name)
    _raise_if_referenced(engine, name)
    with engine.begin() as conn:
        result = conn.execute(projects.delete().where(projects.c.name == name))
        if result.rowcount == 0:
            raise ProjectNotFoundError(name)
