"""Project registry: CRUD against the `projects` table. No ORM — Core queries only."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

from ompire_daemon.db import projects, tasks, templates

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


@dataclass(frozen=True)
class Project:
    name: str
    title: str
    upstream_url: str
    fork_url: str | None
    checkout_path: str


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
) -> Project:
    validate_slug(name)
    resolved_checkout_path = checkout_path or str(default_checkout_root / name)
    try:
        with engine.begin() as conn:
            conn.execute(
                projects.insert().values(
                    name=name,
                    title=title,
                    upstream_url=upstream_url,
                    fork_url=fork_url,
                    checkout_path=resolved_checkout_path,
                )
            )
    except IntegrityError as exc:
        raise DuplicateProjectError(name) from exc
    return get_project(engine, name)


def update_project(
    engine: Engine,
    name: str,
    *,
    title: str,
    upstream_url: str,
    fork_url: str | None,
    checkout_path: str,
    new_name: str | None = None,
) -> Project:
    rename = new_name is not None and new_name != name
    if rename:
        assert new_name is not None  # rename implies it differs from name
        validate_slug(new_name)
        # Same guard as delete: renaming the PK under referencing rows would
        # orphan them — refused, no cascade.
        _raise_if_referenced(engine, name)
        with engine.connect() as conn:
            clash = conn.execute(projects.select().where(projects.c.name == new_name)).first()
        if clash is not None:
            raise DuplicateProjectError(new_name)
    values: dict[str, str | None] = {
        "title": title,
        "upstream_url": upstream_url,
        "fork_url": fork_url,
        "checkout_path": checkout_path,
    }
    if rename:
        values["name"] = new_name
    with engine.begin() as conn:
        result = conn.execute(projects.update().where(projects.c.name == name).values(**values))
        if result.rowcount == 0:
            raise ProjectNotFoundError(name)
    return get_project(engine, new_name if rename else name)


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


def delete_project(engine: Engine, name: str) -> None:
    _raise_if_referenced(engine, name)
    with engine.begin() as conn:
        result = conn.execute(projects.delete().where(projects.c.name == name))
        if result.rowcount == 0:
            raise ProjectNotFoundError(name)
