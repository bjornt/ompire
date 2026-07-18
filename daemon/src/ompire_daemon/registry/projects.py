"""Project registry: CRUD against the `projects` table. No ORM — Core queries only."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

from ompire_daemon.db import projects

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
    def __init__(self, name: str) -> None:
        super().__init__(f"project {name!r} has tasks referencing it")
        self.name = name


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


def _row_to_project(row) -> Project:  # noqa: ANN001
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
) -> Project:
    with engine.begin() as conn:
        result = conn.execute(
            projects.update()
            .where(projects.c.name == name)
            .values(
                title=title,
                upstream_url=upstream_url,
                fork_url=fork_url,
                checkout_path=checkout_path,
            )
        )
        if result.rowcount == 0:
            raise ProjectNotFoundError(name)
    return get_project(engine, name)


def _has_referencing_tasks(engine: Engine, name: str) -> bool:
    # Removal-guard hook: the tasks table doesn't exist yet, so nothing can
    # reference this project. add-task-spawn-clone replaces this body with a
    # real query against the tasks table.
    return False


def delete_project(engine: Engine, name: str) -> None:
    if _has_referencing_tasks(engine, name):
        raise ProjectHasReferencingTasksError(name)
    with engine.begin() as conn:
        result = conn.execute(projects.delete().where(projects.c.name == name))
        if result.rowcount == 0:
            raise ProjectNotFoundError(name)
