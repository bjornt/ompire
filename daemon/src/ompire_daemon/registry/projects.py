"""Project registry: CRUD against the `projects` table. No ORM — Core queries only."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

from ompire_daemon.db import projects, tasks

_SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
# Everything but the <slug> placeholder must be safe in a git ref name.
_BRANCH_PATTERN_SAFE_RE = re.compile(r"^[A-Za-z0-9._/-]*$")


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
    def __init__(self, name: str, task_labels: list[str] | None = None) -> None:
        detail = f": {', '.join(task_labels)}" if task_labels else ""
        super().__init__(f"project {name!r} has tasks referencing it{detail}")
        self.name = name
        self.task_labels = task_labels or []


class InvalidBranchPatternError(ValueError):
    def __init__(self, pattern: str) -> None:
        super().__init__(
            f"invalid branch pattern {pattern!r}: must contain exactly one <slug> "
            "placeholder and otherwise only git-ref-safe characters (A-Za-z0-9._/-)"
        )
        self.pattern = pattern


@dataclass(frozen=True)
class Project:
    name: str
    title: str
    upstream_url: str
    fork_url: str | None
    checkout_path: str
    base_branch: str
    branch_pattern: str


def validate_slug(name: str) -> None:
    if not _SLUG_RE.match(name):
        raise InvalidSlugError(name)


def validate_branch_pattern(pattern: str) -> None:
    if pattern.count("<slug>") != 1:
        raise InvalidBranchPatternError(pattern)
    if not _BRANCH_PATTERN_SAFE_RE.match(pattern.replace("<slug>", "")):
        raise InvalidBranchPatternError(pattern)


def _row_to_project(row) -> Project:  # noqa: ANN001
    return Project(
        name=row.name,
        title=row.title,
        upstream_url=row.upstream_url,
        fork_url=row.fork_url,
        checkout_path=row.checkout_path,
        base_branch=row.base_branch,
        branch_pattern=row.branch_pattern,
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
    base_branch: str = "main",
    branch_pattern: str | None = None,
    default_branch_pattern: str,
    default_checkout_root: Path,
) -> Project:
    validate_slug(name)
    resolved_checkout_path = checkout_path or str(default_checkout_root / name)
    resolved_branch_pattern = branch_pattern or default_branch_pattern
    validate_branch_pattern(resolved_branch_pattern)
    try:
        with engine.begin() as conn:
            conn.execute(
                projects.insert().values(
                    name=name,
                    title=title,
                    upstream_url=upstream_url,
                    fork_url=fork_url,
                    checkout_path=resolved_checkout_path,
                    base_branch=base_branch,
                    branch_pattern=resolved_branch_pattern,
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
    base_branch: str,
    branch_pattern: str,
) -> Project:
    validate_branch_pattern(branch_pattern)
    with engine.begin() as conn:
        result = conn.execute(
            projects.update()
            .where(projects.c.name == name)
            .values(
                title=title,
                upstream_url=upstream_url,
                fork_url=fork_url,
                checkout_path=checkout_path,
                base_branch=base_branch,
                branch_pattern=branch_pattern,
            )
        )
        if result.rowcount == 0:
            raise ProjectNotFoundError(name)
    return get_project(engine, name)


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


def delete_project(engine: Engine, name: str) -> None:
    labels = _referencing_task_labels(engine, name)
    if labels:
        raise ProjectHasReferencingTasksError(name, labels)
    with engine.begin() as conn:
        result = conn.execute(projects.delete().where(projects.c.name == name))
        if result.rowcount == 0:
            raise ProjectNotFoundError(name)
