"""Project registry: CRUD against the `projects` table. No ORM — Core queries only.

Since the template retirement (ADR-0026) a project also owns the workspace and
prompt *defaults* a launch inherits: base branch, branch pattern, Workshop
additions source, and standing preamble. They are defaults, not policy — a
task pins its own effective values at acceptance and stops reading these.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from sqlalchemy import Engine

from ompire_daemon.db import projects, tasks
from ompire_daemon.platform.transactions import reserved_write
from ompire_daemon.work.profiles import (
    require_profile_exists,
)

_SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# Everything but the <slug> placeholder must be safe in a git ref name.
_BRANCH_PATTERN_SAFE_RE = re.compile(r"^[A-Za-z0-9._/-]*$")

# Which Workshop additions file a task's clone gets. The choice is exclusive:
# `project` uses the repository's own additions, `global` uses the operator's,
# and neither silently falls back to the other (ADR-0026).
WORKSHOP_ADDITIONS_SOURCES = ("project", "global")

DEFAULT_BASE_BRANCH = "main"
DEFAULT_WORKSHOP_ADDITIONS = "project"
# Only a dataclass field default. Real project registration seeds the branch
# pattern from the daemon's `default_branch_pattern` setting; nothing reads
# this constant to run a task.
DEFAULT_BRANCH_PATTERN_PLACEHOLDER = "ompire/<slug>"

LAUNCH_CONFIG_STATES = ("reconciled", "needs-reconciliation")


class InvalidBranchPatternError(ValueError):
    def __init__(self, pattern: str) -> None:
        super().__init__(
            f"invalid branch pattern {pattern!r}: must contain exactly one <slug> "
            "placeholder and otherwise only git-ref-safe characters (A-Za-z0-9._/-)"
        )
        self.pattern = pattern


class InvalidWorkshopAdditionsError(ValueError):
    def __init__(self, workshop_additions: str) -> None:
        super().__init__(
            f"invalid workshop additions source {workshop_additions!r}: "
            f"must be one of {', '.join(WORKSHOP_ADDITIONS_SOURCES)}"
        )
        self.workshop_additions = workshop_additions


def validate_branch_pattern(pattern: str) -> None:
    if pattern.count("<slug>") != 1:
        raise InvalidBranchPatternError(pattern)
    if not _BRANCH_PATTERN_SAFE_RE.match(pattern.replace("<slug>", "")):
        raise InvalidBranchPatternError(pattern)


def validate_workshop_additions(workshop_additions: str) -> None:
    if workshop_additions not in WORKSHOP_ADDITIONS_SOURCES:
        raise InvalidWorkshopAdditionsError(workshop_additions)


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
    """409 detail for delete/rename guards: task rows in any state still name
    this project. No cascade — purging the archived tasks is what unblocks it.
    Templates are gone (ADR-0026); task history is the only remaining guard."""

    def __init__(self, name: str, task_labels: list[str] | None = None) -> None:
        task_labels = task_labels or []
        detail = f" ({', '.join(task_labels)})" if task_labels else ""
        super().__init__(f"project {name!r} has tasks referencing it{detail}")
        self.name = name
        self.task_labels = task_labels


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
    """409 detail for the spawn guard: the checkout is not usable yet."""

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
    # (ADR-0025). A reference to policy, not a copy of it: a launch inherits
    # it unless the operator selects a task profile, and what the task then
    # runs is the snapshot pinned at acceptance, not this pointer.
    default_model_profile: str | None = None
    # Workspace and prompt defaults for a launch (ADR-0026). A task inherits
    # each independently and may override each independently.
    base_branch: str = DEFAULT_BASE_BRANCH
    branch_pattern: str = DEFAULT_BRANCH_PATTERN_PLACEHOLDER
    workshop_additions: str = DEFAULT_WORKSHOP_ADDITIONS
    preamble: str = ""
    # `reconciled` or `needs-reconciliation` (ADR-0026): whether the operator
    # still owes a decision about launch configuration carried over from
    # templates. Independent of `setup_state` — a ready checkout can still be
    # unlaunchable, and a cloning one can have perfectly clear defaults.
    launch_config_state: str = "reconciled"


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
        base_branch=row.base_branch,
        branch_pattern=row.branch_pattern,
        workshop_additions=row.workshop_additions,
        preamble=row.preamble,
        launch_config_state=row.launch_config_state,
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
    base_branch: str = DEFAULT_BASE_BRANCH,
    branch_pattern: str = DEFAULT_BRANCH_PATTERN_PLACEHOLDER,
    workshop_additions: str = DEFAULT_WORKSHOP_ADDITIONS,
    preamble: str = "",
) -> Project:
    """Register a project with its launch defaults.

    `branch_pattern`'s default here is only a last resort for direct registry
    callers; real registration passes the daemon's `default_branch_pattern`
    setting, which is a seed at this moment and never re-read afterwards.
    """
    validate_slug(name)
    validate_branch_pattern(branch_pattern)
    validate_workshop_additions(workshop_additions)
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
                base_branch=base_branch,
                branch_pattern=branch_pattern,
                workshop_additions=workshop_additions,
                preamble=preamble,
                launch_config_state="reconciled",
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
    base_branch: str | _Unsupplied = UNSUPPLIED,
    branch_pattern: str | _Unsupplied = UNSUPPLIED,
    workshop_additions: str | _Unsupplied = UNSUPPLIED,
    preamble: str | _Unsupplied = UNSUPPLIED,
) -> Project:
    """Update a project's editable fields.

    `default_model_profile` is three-valued: omitted preserves the stored
    reference, `None` clears it, and a name selects that profile. The stored
    value is read inside the write reservation rather than from any earlier
    read, so an omission preserves what is actually committed.

    The workspace defaults follow the same omission rule but are never null:
    an empty `preamble` string is a value ("no preamble"), not a clear.

    Repointing `checkout_path` is refused while a checkout export for this
    project is running or unresolved (ADR-0036). That is not a claim the
    directory cannot move on disk — nothing in SQLite can promise that — only
    that Ompire's own registration will not be changed out from under an
    operation that is mid-flight or unexplained. Every other field stays
    editable throughout.
    """
    from ompire_daemon.registry.result_exports import (
        assert_project_exports_settled_on,
    )

    if not isinstance(branch_pattern, _Unsupplied):
        validate_branch_pattern(branch_pattern)
    if not isinstance(workshop_additions, _Unsupplied):
        validate_workshop_additions(workshop_additions)
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
        if current.checkout_path != checkout_path:
            assert_project_exports_settled_on(conn, name)
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
        values["base_branch"] = (
            current.base_branch
            if isinstance(base_branch, _Unsupplied)
            else base_branch
        )
        values["branch_pattern"] = (
            current.branch_pattern
            if isinstance(branch_pattern, _Unsupplied)
            else branch_pattern
        )
        values["workshop_additions"] = (
            current.workshop_additions
            if isinstance(workshop_additions, _Unsupplied)
            else workshop_additions
        )
        values["preamble"] = (
            current.preamble if isinstance(preamble, _Unsupplied) else preamble
        )
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


def _raise_if_referenced(engine: Engine, name: str) -> None:
    labels = _referencing_task_labels(engine, name)
    if labels:
        raise ProjectHasReferencingTasksError(name, labels)


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


def set_launch_config_state(engine: Engine, name: str, state: str) -> Project:
    """Mark whether this project's launch configuration still needs the
    operator's decision (ADR-0026). Separate from `setup_state` on purpose."""
    assert state in LAUNCH_CONFIG_STATES
    with engine.begin() as conn:
        result = conn.execute(
            projects.update()
            .where(projects.c.name == name)
            .values(launch_config_state=state)
        )
        if result.rowcount == 0:
            raise ProjectNotFoundError(name)
    return get_project(engine, name)


class ProjectLaunchConfigUnreconciledError(Exception):
    """409 detail for the launch guard: the operator has not yet decided what
    this project's carried-over template configuration should become."""

    def __init__(self, name: str) -> None:
        super().__init__(
            f"project {name!r} still needs launch-configuration reconciliation; "
            "resolve it in the project editor before launching a task"
        )
        self.name = name
