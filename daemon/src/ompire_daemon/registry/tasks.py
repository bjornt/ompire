"""Task registry: lifecycle queries against the `tasks` table. No ORM — Core only.

States are limited to created/failed/archived in this chunk; the D4 session
state machine arrives with add-session-states.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Connection, Engine
from sqlalchemy.exc import IntegrityError

from ompire_daemon.db import (
    review_iterations,
    reviews,
    task_sessions,
    tasks,
    workflow_step_records,
)
from ompire_daemon.execution_inputs import (
    TaskExecutionInputs,
    decode_execution_inputs,
    encode_execution_inputs,
    execution_inputs_payload,
)

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


class TaskInputsAlreadyPinnedError(Exception):
    """Refusal for a second write of the accepted inputs. They are decided
    once: a task that already has them is never re-resolved, and a legacy
    confirmation cannot edit a task that was accepted normally."""

    def __init__(self, task_id: int) -> None:
        super().__init__(f"task {task_id} already has pinned execution inputs")
        self.task_id = task_id


@dataclass(frozen=True)
class Task:
    id: int
    project_name: str
    # The launch decision this task runs under, or None for a task created
    # before pinned inputs existed (ADR-0026). None is a real state, not a
    # missing value to fill in: it blocks model- and branch-dependent work
    # until the operator confirms a continuation configuration.
    execution_inputs: TaskExecutionInputs | None
    slug: str
    branch: str
    clone_path: str
    state: str
    prompt: str
    error: str | None
    workshop_id: str | None
    workflow_name: str
    workflow_status: str | None
    workflow_step: str | None
    # The declared ending a format-2 run reached; None while it runs, and
    # for every format-1 run (ADR-0029).
    workflow_result: str | None
    pr_url: str | None
    pr_state: str | None
    pr_merged_at: str | None
    spawn_completed_at: str | None
    created_at: str
    updated_at: str


class TaskConfigurationRequiredError(Exception):
    """The single readiness guard for everything that needs a task's launch
    inputs (ADR-0026).

    A task created before pinned inputs existed has no accepted model, base
    branch, or preamble, and today's project and profile settings are not
    evidence of what it used. Rather than guess — or fall back to `main` and
    the host's model — every path that would need those values refuses here,
    and the operator confirms a continuation configuration once. Reading,
    inspecting, stopping, and cleaning up such a task stay available.
    """

    def __init__(self, task_id: int) -> None:
        super().__init__(
            f"task {task_id} has no confirmed launch configuration; confirm one "
            "on the task before continuing, reviewing, or shipping it"
        )
        self.task_id = task_id


def task_payload(task: Task, *, engine: Engine) -> dict:
    """The one wire shape of a task row, used by both REST responses and
    `task_updated` events so a client cannot see two different shapes for the
    same row.

    `execution_inputs` is the accepted decision itself — the same document
    execution reads — and `null` means the task predates pinned inputs, which
    `needs_configuration` states outright so a client does not have to infer
    a blocker from an absent field.

    The workflow fields describe the definition *this task* pinned, resolved
    through `taskdefinition` (ADR-0028). `workflow_primary_session` is null
    rather than a guess whenever the definition cannot be resolved: a client
    that substituted a plausible default would point review and shipping at a
    session this task may never have declared. A task whose revision is
    damaged or unsupported still serializes — it reports why, and stays
    readable, stoppable, and cleanable.
    """
    from ompire_daemon.taskdefinition import workflow_readiness

    payload = asdict(task)
    payload["execution_inputs"] = (
        execution_inputs_payload(task.execution_inputs)
        if task.execution_inputs is not None
        else None
    )
    payload["needs_configuration"] = task.execution_inputs is None
    binding = (
        task.execution_inputs.workflow_binding
        if task.execution_inputs is not None
        else None
    )
    readiness = workflow_readiness(engine, task)
    payload["workflow_revision"] = binding.revision if binding else None
    payload["workflow_revision_source"] = binding.source if binding else None
    payload["workflow_ready"] = readiness.ready
    payload["workflow_readiness_reason"] = readiness.reason
    payload["workflow_readiness_detail"] = readiness.detail
    payload["workflow_primary_session"] = None
    payload["workflow_sessions"] = None
    if readiness.ready:
        from ompire_daemon.taskdefinition import resolve_task_definition

        definition = resolve_task_definition(engine, task).definition
        payload["workflow_primary_session"] = definition.primary
        payload["workflow_sessions"] = list(definition.sessions)
    return payload


def require_task_inputs(task: Task) -> TaskExecutionInputs:
    if task.execution_inputs is None:
        raise TaskConfigurationRequiredError(task.id)
    return task.execution_inputs


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


def _row_to_task(row) -> Task:
    return Task(
        id=row.id,
        project_name=row.project_name,
        execution_inputs=(
            decode_execution_inputs(row.execution_inputs_json)
            if row.execution_inputs_json is not None
            else None
        ),
        slug=row.slug,
        branch=row.branch,
        clone_path=row.clone_path,
        state=row.state,
        prompt=row.prompt,
        error=row.error,
        workshop_id=row.workshop_id,
        workflow_name=row.workflow_name,
        workflow_status=row.workflow_status,
        workflow_step=row.workflow_step,
        workflow_result=row.workflow_result,
        pr_url=row.pr_url,
        pr_state=row.pr_state,
        pr_merged_at=row.pr_merged_at,
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
    execution_inputs: TaskExecutionInputs,
    workflow_name: str = "single-step",
    conn: Connection | None = None,
) -> Task:
    """Insert one accepted task together with the inputs it was accepted
    under (ADR-0026).

    `conn` lets the caller run this inside an already-open write reservation,
    so the re-resolution the acceptance checked and this insert cannot be
    separated by another writer. Without it the function opens its own
    transaction, which is what the simpler callers want.
    """
    validate_task_slug(slug)
    now = _now_iso()
    values = {
        "project_name": project_name,
        "execution_inputs_json": encode_execution_inputs(execution_inputs),
        "slug": slug,
        "branch": branch,
        "clone_path": clone_path,
        "state": "created",
        "prompt": prompt,
        "error": None,
        "workshop_id": None,
        "workflow_name": workflow_name,
        "workflow_status": None,
        "workflow_step": None,
        "workflow_result": None,
        "pr_url": None,
        "spawn_completed_at": None,
        "created_at": now,
        "updated_at": now,
    }
    if conn is not None:
        try:
            result = conn.execute(tasks.insert().values(**values))
        except IntegrityError as exc:
            raise DuplicateTaskError(project_name, slug) from exc
        inserted = result.inserted_primary_key
        assert inserted is not None
        row = conn.execute(tasks.select().where(tasks.c.id == inserted[0])).one()
        return _row_to_task(row)
    try:
        with engine.begin() as own_conn:
            result = own_conn.execute(tasks.insert().values(**values))
            inserted = result.inserted_primary_key
            assert inserted is not None
            task_id = inserted[0]
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


def list_pr_pollable_tasks(engine: Engine) -> list[Task]:
    """Tasks the PR watcher polls (merge-poll capability, design D-2): they
    have a `pr_url`, are not archived, and are not yet in a terminal PR state.

    A delivery that ended at a local signed commit or a pushed branch has no
    `pr_url` and is therefore never polled — there is no pull request to watch
    and no merge it is waiting for (ADR-0032)."""
    with engine.connect() as conn:
        rows = conn.execute(
            tasks.select()
            .where(tasks.c.pr_url.isnot(None))
            .where(tasks.c.state != "archived")
            .where(tasks.c.pr_state.is_(None) | (tasks.c.pr_state == "open"))
        ).all()
    return [_row_to_task(row) for row in rows]


def _update(engine: Engine, task_id: int, **values) -> Task:
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


def mark_pr_url(engine: Engine, task_id: int, url: str) -> Task:
    return _update(engine, task_id, pr_url=url)


def mark_pr_state(
    engine: Engine, task_id: int, pr_state: str, merged_at: str | None = None
) -> Task:
    return _update(engine, task_id, pr_state=pr_state, pr_merged_at=merged_at)


def mark_failed(engine: Engine, task_id: int, error: str) -> Task:
    return _update(engine, task_id, state="failed", error=error, spawn_completed_at=_now_iso())


def pin_execution_inputs(engine: Engine, task_id: int, inputs: TaskExecutionInputs) -> Task:
    """Write a legacy task's confirmed continuation configuration, once.

    Only a task without pinned inputs can be written: a normally accepted
    task's decision is not editable through this path, and a second
    confirmation of the same task is refused rather than silently applied.
    The read and the write share one reservation so two confirmations racing
    cannot both believe they were first.
    """
    from ompire_daemon.registry.model_profiles import reserved_write

    with reserved_write(engine) as conn:
        row = conn.execute(
            tasks.select()
            .with_only_columns(tasks.c.id, tasks.c.execution_inputs_json)
            .where(tasks.c.id == task_id)
        ).first()
        if row is None:
            raise TaskNotFoundError(task_id)
        if row.execution_inputs_json is not None:
            raise TaskInputsAlreadyPinnedError(task_id)
        conn.execute(
            tasks.update()
            .where(tasks.c.id == task_id)
            .values(
                execution_inputs_json=encode_execution_inputs(inputs),
                updated_at=_now_iso(),
            )
        )
    return get_task(engine, task_id)


def pin_workflow_binding(engine: Engine, task_id: int, binding) -> Task:
    """Fill in a task's *null* workflow binding, once, preserving everything
    else the task already accepted (ADR-0028).

    Deliberately the narrowest possible write. A task accepted after retained
    revisions existed already has a binding and is refused here; every other
    pinned field — the model bindings, the workspace values, the branch, the
    provenance — is carried through untouched, because none of them is being
    decided again. The read and the write share one reservation so two
    confirmations racing cannot both believe they were first.
    """
    from ompire_daemon.execution_inputs import (
        decode_execution_inputs,
        execution_inputs_document,
    )
    from ompire_daemon.registry.model_profiles import reserved_write

    with reserved_write(engine) as conn:
        row = conn.execute(
            tasks.select()
            .with_only_columns(tasks.c.id, tasks.c.execution_inputs_json)
            .where(tasks.c.id == task_id)
        ).first()
        if row is None:
            raise TaskNotFoundError(task_id)
        if row.execution_inputs_json is None:
            raise TaskConfigurationRequiredError(task_id)
        existing = decode_execution_inputs(row.execution_inputs_json)
        if existing.workflow_binding is not None:
            raise TaskInputsAlreadyPinnedError(task_id)
        document = execution_inputs_document(existing)
        from ompire_daemon.execution_inputs import encode_workflow_binding

        document["workflow_binding"] = encode_workflow_binding(binding)
        conn.execute(
            tasks.update()
            .where(tasks.c.id == task_id)
            .values(
                execution_inputs_json=json.dumps(document), updated_at=_now_iso()
            )
        )
    return get_task(engine, task_id)


def list_unconfigured_tasks(engine: Engine) -> list[Task]:
    """Live tasks that predate pinned inputs. Archived rows are excluded:
    they are readable history and are never asked to be confirmed."""
    with engine.connect() as conn:
        rows = conn.execute(
            tasks.select()
            .where(tasks.c.execution_inputs_json.is_(None))
            .where(tasks.c.state != "archived")
            .order_by(tasks.c.id)
        ).all()
    return [_row_to_task(row) for row in rows]


def mark_archived(engine: Engine, task_id: int) -> Task:
    return _update(engine, task_id, state="archived")


def purge_task(engine: Engine, task_id: int) -> list[str]:
    """Delete a task and every durable child row it owns.

    Returns the candidate staging repositories that are now unreferenced, so
    the caller can remove them from disk — foreign-key cascades cannot be
    assumed on this connection, and nothing else knows those paths once the
    rows are gone.

    Purge is the only operation that deletes durable history; cleanup
    deliberately retains the review and the delivery journal (ADR-0016), and
    result bytes are never destroyed here at all — an unpurged complete
    revision refuses the whole purge and names itself (ADR-0034).
    """
    from ompire_daemon.registry.model_profiles import reserved_write
    from ompire_daemon.registry.results import (
        assert_no_retained_results,
        delete_task_results,
    )
    from ompire_daemon.registry.ships import delete_task_deliveries_on

    # Every refusal is decided before any deletion, on one write reservation
    # (ADR-0034). Delivery rows used to be deleted in their own transaction
    # *before* the rest, which meant a task that turned out to be ineligible
    # could lose its publication journal on the way to being refused. A refusal
    # now leaves every child row this task owns exactly where it was.
    with reserved_write(engine) as conn:
        row = conn.execute(
            tasks.select().with_only_columns(tasks.c.id, tasks.c.state).where(
                tasks.c.id == task_id
            )
        ).first()
        if row is None:
            raise TaskNotFoundError(task_id)
        if row.state != "archived":
            raise TaskNotArchivedError(task_id, row.state)
        # Retained result bytes are the operator's, not the task's to take with
        # it. They are purged explicitly or not at all; tombstones travel with
        # the rest of the history.
        assert_no_retained_results(conn, task_id)
        storage_paths = delete_task_deliveries_on(conn, task_id)
        delete_task_results(conn, task_id)
        conn.execute(workflow_step_records.delete().where(workflow_step_records.c.task_id == task_id))
        conn.execute(task_sessions.delete().where(task_sessions.c.task_id == task_id))
        conn.execute(review_iterations.delete().where(review_iterations.c.task_id == task_id))
        conn.execute(reviews.delete().where(reviews.c.task_id == task_id))
        conn.execute(tasks.delete().where(tasks.c.id == task_id))
    return storage_paths


def reconcile_startup(engine: Engine) -> tuple[list[Task], list[Task]]:
    """Classify every `created` task per the startup reconciliation matrix
    (crash-recovery capability, design D-4), as far as the registry alone can
    tell: a spawn that never completed is unresumable and marked `failed`
    here. Every spawn-completed task is a recovery candidate — sessions are
    lazily spawned (workflow-engine capability), so a task may legitimately
    have no recorded session identity (a command-only workflow, or a run
    that failed before its first agent step); "no session id" is no longer a
    failure cause. Candidates still need their container's presence checked
    (async, `workshop_status`) before they can be recovered or failed as
    `fail-missing-container` — the caller finishes classifying.

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
        else:
            candidates.append(task)
    return failed, candidates
