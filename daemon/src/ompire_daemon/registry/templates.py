"""Template registry: CRUD against the `templates` table. No ORM — Core only.

SPEC Decision 6/9: a template carries everything spawn needs and references
a project by name (checkout path and remotes stay with the project).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

from ompire_daemon.db import projects, tasks, templates
from ompire_daemon.registry.projects import ProjectNotFoundError, validate_slug
# The engine's registry is the source of truth for valid workflow names
# (workflow-engine design D-2); no import cycle — workflows.py references
# templates only under TYPE_CHECKING.
from ompire_daemon.workflows import registered_workflows

# Everything but the <slug> placeholder must be safe in a git ref name.
_BRANCH_PATTERN_SAFE_RE = re.compile(r"^[A-Za-z0-9._/-]*$")

WORKSHOP_ADDITIONS_SOURCES = ("project", "global")

# omp's `--thinking` vocabulary, verified against omp v17.2.12 (`omp --help`).
THINKING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max", "auto")


class DuplicateTemplateError(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(f"template {name!r} already exists")
        self.name = name


class TemplateNotFoundError(Exception):
    def __init__(self, name: str) -> None:
        super().__init__(f"template {name!r} not found")
        self.name = name


class TemplateHasReferencingTasksError(Exception):
    def __init__(self, name: str, task_labels: list[str]) -> None:
        detail = f": {', '.join(task_labels)}" if task_labels else ""
        super().__init__(f"template {name!r} has tasks referencing it{detail}")
        self.name = name
        self.task_labels = task_labels


class UnknownTemplateProjectError(ValueError):
    def __init__(self, project_name: str) -> None:
        super().__init__(f"project {project_name!r} not found")
        self.project_name = project_name


class InvalidBranchPatternError(ValueError):
    def __init__(self, pattern: str) -> None:
        super().__init__(
            f"invalid branch pattern {pattern!r}: must contain exactly one <slug> "
            "placeholder and otherwise only git-ref-safe characters (A-Za-z0-9._/-)"
        )
        self.pattern = pattern


class UnknownWorkflowError(ValueError):
    def __init__(self, workflow: str) -> None:
        super().__init__(
            f"unknown workflow {workflow!r}: registered workflows are "
            f"{', '.join(registered_workflows())}"
        )
        self.workflow = workflow


class InvalidThinkingLevelError(ValueError):
    def __init__(self, thinking: str) -> None:
        super().__init__(
            f"invalid thinking level {thinking!r}: must be one of {', '.join(THINKING_LEVELS)}"
        )
        self.thinking = thinking


class InvalidWorkshopAdditionsError(ValueError):
    def __init__(self, workshop_additions: str) -> None:
        super().__init__(
            f"invalid workshop additions source {workshop_additions!r}: "
            f"must be one of {', '.join(WORKSHOP_ADDITIONS_SOURCES)}"
        )
        self.workshop_additions = workshop_additions


@dataclass(frozen=True)
class Template:
    name: str
    project_name: str
    base_branch: str
    branch_pattern: str
    workflow: str
    workshop_additions: str
    model: str | None
    thinking: str | None
    preamble: str
    created_at: str
    updated_at: str


def validate_branch_pattern(pattern: str) -> None:
    if pattern.count("<slug>") != 1:
        raise InvalidBranchPatternError(pattern)
    if not _BRANCH_PATTERN_SAFE_RE.match(pattern.replace("<slug>", "")):
        raise InvalidBranchPatternError(pattern)


def validate_workflow(workflow: str) -> None:
    if workflow not in registered_workflows():
        raise UnknownWorkflowError(workflow)


def validate_thinking(thinking: str) -> None:
    if thinking not in THINKING_LEVELS:
        raise InvalidThinkingLevelError(thinking)


def validate_workshop_additions(workshop_additions: str) -> None:
    if workshop_additions not in WORKSHOP_ADDITIONS_SOURCES:
        raise InvalidWorkshopAdditionsError(workshop_additions)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_template(row) -> Template:  # noqa: ANN001
    return Template(
        name=row.name,
        project_name=row.project_name,
        base_branch=row.base_branch,
        branch_pattern=row.branch_pattern,
        workflow=row.workflow,
        workshop_additions=row.workshop_additions,
        model=row.model,
        thinking=row.thinking,
        preamble=row.preamble,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _require_project(engine: Engine, project_name: str) -> None:
    with engine.connect() as conn:
        row = conn.execute(projects.select().where(projects.c.name == project_name)).first()
    if row is None:
        raise UnknownTemplateProjectError(project_name)


def list_templates(engine: Engine) -> list[Template]:
    with engine.connect() as conn:
        rows = conn.execute(templates.select().order_by(templates.c.name)).all()
    return [_row_to_template(row) for row in rows]


def get_template(engine: Engine, name: str) -> Template:
    with engine.connect() as conn:
        row = conn.execute(templates.select().where(templates.c.name == name)).first()
    if row is None:
        raise TemplateNotFoundError(name)
    return _row_to_template(row)


def create_template(
    engine: Engine,
    *,
    name: str,
    project_name: str,
    base_branch: str = "main",
    branch_pattern: str,
    workflow: str = "single-step",
    workshop_additions: str = "project",
    model: str | None = None,
    thinking: str | None = None,
    preamble: str = "",
) -> Template:
    validate_slug(name)
    _require_project(engine, project_name)
    validate_branch_pattern(branch_pattern)
    validate_workflow(workflow)
    validate_workshop_additions(workshop_additions)
    if thinking is not None:
        validate_thinking(thinking)
    now = _now_iso()
    try:
        with engine.begin() as conn:
            conn.execute(
                templates.insert().values(
                    name=name,
                    project_name=project_name,
                    base_branch=base_branch,
                    branch_pattern=branch_pattern,
                    workflow=workflow,
                    workshop_additions=workshop_additions,
                    model=model,
                    thinking=thinking,
                    preamble=preamble,
                    created_at=now,
                    updated_at=now,
                )
            )
    except IntegrityError as exc:
        raise DuplicateTemplateError(name) from exc
    return get_template(engine, name)


def update_template(
    engine: Engine,
    name: str,
    *,
    project_name: str,
    base_branch: str,
    branch_pattern: str,
    workflow: str,
    workshop_additions: str,
    model: str | None,
    thinking: str | None,
    preamble: str,
) -> Template:
    # Existence first: a 404 on an unknown name beats any body validation.
    get_template(engine, name)
    _require_project(engine, project_name)
    validate_branch_pattern(branch_pattern)
    validate_workflow(workflow)
    validate_workshop_additions(workshop_additions)
    if thinking is not None:
        validate_thinking(thinking)
    with engine.begin() as conn:
        conn.execute(
            templates.update()
            .where(templates.c.name == name)
            .values(
                project_name=project_name,
                base_branch=base_branch,
                branch_pattern=branch_pattern,
                workflow=workflow,
                workshop_additions=workshop_additions,
                model=model,
                thinking=thinking,
                preamble=preamble,
                updated_at=_now_iso(),
            )
        )
    return get_template(engine, name)


def _referencing_task_labels(engine: Engine, name: str) -> list[str]:
    # Only live rows block removal: archived rows keep the name as history
    # (differs from the project guard — templates don't own the clone path).
    with engine.connect() as conn:
        rows = conn.execute(
            tasks.select()
            .with_only_columns(tasks.c.project_name, tasks.c.slug, tasks.c.state)
            .where(tasks.c.template_name == name)
            .where(tasks.c.state != "archived")
            .order_by(tasks.c.id)
        ).all()
    return [f"{row.project_name}/{row.slug} ({row.state})" for row in rows]


def delete_template(engine: Engine, name: str) -> None:
    labels = _referencing_task_labels(engine, name)
    if labels:
        raise TemplateHasReferencingTasksError(name, labels)
    with engine.begin() as conn:
        result = conn.execute(templates.delete().where(templates.c.name == name))
        if result.rowcount == 0:
            raise TemplateNotFoundError(name)
