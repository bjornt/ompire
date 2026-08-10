"""Task registry: lifecycle queries against the `tasks` table. No ORM — Core only.

States are limited to created/failed/archived in this chunk; the D4 session
state machine arrives with add-session-states.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

from ompire_daemon.db import tasks

_SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_SLUG_LENGTH = 64

TASK_STATES = ("created", "failed", "archived")


class InvalidTaskSlugError(ValueError):
    def __init__(self, slug: str) -> None:
        super().__init__(
            f"invalid task slug {slug!r}: must be lowercase alphanumerics and hyphens, "
            f"max {MAX_SLUG_LENGTH} chars"
        )
        self.slug = slug


class DuplicateTaskError(Exception):
    def __init__(self, project_name: str, slug: str) -> None:
        super().__init__(f"a live task {project_name}/{slug} already exists")
        self.project_name = project_name
        self.slug = slug


class TaskNotFoundError(Exception):
    def __init__(self, task_id: int) -> None:
        super().__init__(f"task {task_id} not found")
        self.task_id = task_id


class TaskNotArchivedError(Exception):
    def __init__(self, task_id: int, state: str) -> None:
        super().__init__(f"task {task_id} is {state!r}, not archived; only archived tasks can be purged")
        self.task_id = task_id
        self.state = state


class ClonePathOutsideRootError(ValueError):
    def __init__(self, path: Path, root: Path) -> None:
        super().__init__(f"clone path {path} resolves outside task root {root}")
        self.path = path
        self.root = root


@dataclass(frozen=True)
class Task:
    id: int
    project_name: str
    slug: str
    branch: str
    clone_path: str
    state: str
    prompt: str
    error: str | None
    workshop_id: str | None
    session_id: str | None
    pr_url: str | None
    spawn_completed_at: str | None
    created_at: str
    updated_at: str


def validate_task_slug(slug: str) -> None:
    if len(slug) > MAX_SLUG_LENGTH or not _SLUG_RE.match(slug):
        raise InvalidTaskSlugError(slug)


def clone_path_for(task_root: Path, project_name: str, slug: str) -> Path:
    """Build `<task_root>/<project>/<slug>`, refusing anything that escapes the root.

    Both components are slug-validated before this runs, so escape should be
    impossible — the resolve check is defense in depth on the security-critical
    path (SPEC Decision 3 posture).
    """
    root = task_root.expanduser().resolve()
    path = (root / project_name / slug).resolve()
    if root not in path.parents:
        raise ClonePathOutsideRootError(path, root)
    return path


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_task(row) -> Task:  # noqa: ANN001
    return Task(
        id=row.id,
        project_name=row.project_name,
        slug=row.slug,
        branch=row.branch,
        clone_path=row.clone_path,
        state=row.state,
        prompt=row.prompt,
        error=row.error,
        workshop_id=row.workshop_id,
        session_id=row.session_id,
        pr_url=row.pr_url,
        spawn_completed_at=row.spawn_completed_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def create_task(
    engine: Engine,
    *,
    project_name: str,
    slug: str,
    branch: str,
    clone_path: str,
    prompt: str,
) -> Task:
    validate_task_slug(slug)
    now = _now_iso()
    try:
        with engine.begin() as conn:
            result = conn.execute(
                tasks.insert().values(
                    project_name=project_name,
                    slug=slug,
                    branch=branch,
                    clone_path=clone_path,
                    state="created",
                    prompt=prompt,
                    error=None,
                    workshop_id=None,
                    session_id=None,
                    pr_url=None,
                    spawn_completed_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            task_id = result.inserted_primary_key[0]
    except IntegrityError as exc:
        raise DuplicateTaskError(project_name, slug) from exc
    return get_task(engine, task_id)


def get_task(engine: Engine, task_id: int) -> Task:
    with engine.connect() as conn:
        row = conn.execute(tasks.select().where(tasks.c.id == task_id)).first()
    if row is None:
        raise TaskNotFoundError(task_id)
    return _row_to_task(row)


def list_tasks(engine: Engine) -> list[Task]:
    with engine.connect() as conn:
        rows = conn.execute(tasks.select().order_by(tasks.c.created_at.desc(), tasks.c.id.desc())).all()
    return [_row_to_task(row) for row in rows]


def _update(engine: Engine, task_id: int, **values) -> Task:  # noqa: ANN003
    with engine.begin() as conn:
        result = conn.execute(
            tasks.update().where(tasks.c.id == task_id).values(updated_at=_now_iso(), **values)
        )
        if result.rowcount == 0:
            raise TaskNotFoundError(task_id)
    return get_task(engine, task_id)


def mark_spawn_completed(engine: Engine, task_id: int) -> Task:
    return _update(engine, task_id, spawn_completed_at=_now_iso())


def mark_workshop_launched(engine: Engine, task_id: int, workshop_id: str) -> Task:
    return _update(engine, task_id, workshop_id=workshop_id)


def mark_session_id(engine: Engine, task_id: int, session_id: str) -> Task:
    return _update(engine, task_id, session_id=session_id)


def mark_pr_url(engine: Engine, task_id: int, url: str) -> Task:
    return _update(engine, task_id, pr_url=url)


def mark_failed(engine: Engine, task_id: int, error: str) -> Task:
    return _update(engine, task_id, state="failed", error=error, spawn_completed_at=_now_iso())


def mark_archived(engine: Engine, task_id: int) -> Task:
    return _update(engine, task_id, state="archived")


def purge_task(engine: Engine, task_id: int) -> None:
    task = get_task(engine, task_id)
    if task.state != "archived":
        raise TaskNotArchivedError(task_id, task.state)
    with engine.begin() as conn:
        conn.execute(tasks.delete().where(tasks.c.id == task_id))


def reconcile_startup(engine: Engine) -> tuple[list[Task], list[Task]]:
    """Classify every `created` task per the startup reconciliation matrix
    (crash-recovery capability, design D-4), as far as the registry alone can
    tell: a spawn that never completed, or one that completed with no
    recorded session id, is unresumable and marked `failed` here. A
    spawn-completed task with a session id still needs its container's
    presence checked (async, `workshop_status`) before it can be recovered or
    failed as `fail-missing-container` — those are returned as candidates for
    the caller to finish classifying.

    Returns `(failed, candidates)`.
    """
    with engine.connect() as conn:
        rows = conn.execute(tasks.select().where(tasks.c.state == "created")).all()
    failed: list[Task] = []
    candidates: list[Task] = []
    for row in rows:
        task = _row_to_task(row)
        if task.spawn_completed_at is None:
            failed.append(
                mark_failed(engine, task.id, "daemon restarted during spawn; pipeline did not complete")
            )
        elif task.session_id is None:
            failed.append(mark_failed(engine, task.id, "no session id recorded; cannot resume"))
        else:
            candidates.append(task)
    return failed, candidates
