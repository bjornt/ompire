"""Spawn pipeline: fetch → hardlink clone → branch → workshop launch
(SPEC Decision 5, step 1; workflow-engine design D-4).

Runs as an asyncio background job after POST /api/tasks returns 202. The
steps are subprocesses exec'd with argument lists (no shell), bounded by
per-step timeouts, with stderr captured. Agent start and prompt delivery are
NOT pipeline steps anymore: once the workshop step completes, the task is
handed to the workflow engine, whose `workflow_step` events continue the
Spawn view's inline progress. Pipeline progress stays ephemeral — broadcast,
never persisted; the registry records only the outcome (and, for the
workshop step, the launched workshop's lock id).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy import Engine

from ompire_daemon.config import Config
from ompire_daemon.events import EventHub
from ompire_daemon.registry.projects import Project, get_project
from ompire_daemon.registry.tasks import (
    Task,
    get_task,
    mark_failed,
    mark_spawn_completed,
    mark_workshop_launched,
)
from ompire_daemon.registry.templates import (
    Template,
    TemplateNotFoundError,
    get_template,
)
from ompire_daemon.workflows import UnknownWorkflowNameError, WorkflowRunner

_STDERR_LIMIT = 64 * 1024

WORKSHOP_LOCK_FILENAME = ".workshop.lock"

# The daemon-owned outcome directory (workflow-engine design D-3): excluded
# from git per clone so outcome files never appear in status/diffs/reviews.
GIT_EXCLUDE_OMPIRE_ENTRY = ".ompire/"


class StepFailedError(Exception):
    def __init__(self, step: str, stderr: str) -> None:
        super().__init__(f"spawn step {step!r} failed")
        self.step = step
        self.stderr = stderr


@dataclass(frozen=True)
class Step:
    name: str
    argv: list[str]
    timeout: int
    cwd: str | None = None


async def _run_step(step: Step) -> str:
    """Run the step's command; return stderr text. Raises on failure/timeout."""
    try:
        process = await asyncio.create_subprocess_exec(
            *step.argv,
            cwd=step.cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        # e.g. my-workshop not installed on this host: fail the task loudly
        # instead of letting the background job die with the task stuck.
        raise StepFailedError(step.name, f"cannot exec {step.argv[0]!r}: {exc}") from exc
    try:
        _, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=step.timeout)
    except asyncio.CancelledError:
        if process.returncode is None:
            process.kill()
        import contextlib

        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.wait(), timeout=5)
        raise
    except TimeoutError:
        process.kill()
        # `wait()` also waits for pipe EOF; a grandchild still holding the
        # pipe after the kill must not hang the pipeline — bound the wait.
        import contextlib

        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.wait(), timeout=5)
        raise StepFailedError(step.name, f"timed out after {step.timeout}s") from None
    stderr = stderr_bytes[-_STDERR_LIMIT:].decode("utf-8", errors="replace")
    if process.returncode != 0:
        raise StepFailedError(step.name, stderr)
    return stderr


def _git_steps(config: Config, project: Project, template: Template, task: Task) -> list[Step]:
    clone_path = task.clone_path
    git_timeout = config.spawn_step_timeout
    return [
        Step("fetch", ["git", "-C", project.checkout_path, "fetch", "origin"], git_timeout),
        # Local source path => hardlink clone, near-instant.
        Step("clone", ["git", "clone", project.checkout_path, clone_path], git_timeout),
        Step(
            "branch",
            ["git", "-C", clone_path, "checkout", "-b", task.branch, f"origin/{template.base_branch}"],
            git_timeout,
        ),
    ]


def _exclude_outcome_dir(clone_path: str) -> None:
    """Append `.ompire/` to the clone's `.git/info/exclude` (idempotent) so
    outcome files are invisible to git status, llmvet, and PRs (SPEC D8)."""
    exclude_path = Path(clone_path) / ".git" / "info" / "exclude"
    try:
        existing = exclude_path.read_text(encoding="utf-8")
    except OSError:
        existing = ""
    if GIT_EXCLUDE_OMPIRE_ENTRY in existing.splitlines():
        return
    separator = "" if existing.endswith("\n") or not existing else "\n"
    try:
        with exclude_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{separator}{GIT_EXCLUDE_OMPIRE_ENTRY}\n")
    except OSError as exc:
        raise StepFailedError("clone", f"cannot write {exclude_path}: {exc}") from exc


def _read_workshop_lock(clone_path: str) -> str:
    """Return the non-empty lock id from `.workshop.lock`, or raise StepFailedError.

    The file's format belongs to my-workshop; the daemon stores its content
    verbatim (stripped), interpreting nothing beyond non-emptiness (design D-2).
    """
    lock_path = Path(clone_path) / WORKSHOP_LOCK_FILENAME
    try:
        lock_id = lock_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise StepFailedError(
            "workshop", f"launch succeeded but {lock_path} is unreadable: {exc}"
        ) from exc
    if not lock_id:
        raise StepFailedError("workshop", f"launch succeeded but {lock_path} is missing or empty")
    return lock_id


async def run_spawn_pipeline(
    engine: Engine,
    events: EventHub,
    config: Config,
    task_id: int,
    runner: WorkflowRunner,
    *,
    model_override: str | None = None,
    thinking_override: str | None = None,
) -> None:
    task = get_task(engine, task_id)

    # Resolve the template ONCE at pipeline start (design D-3): base branch,
    # branch pattern, and the project (hence checkout and remotes) all come
    # from it. A template deleted between the 202 and this point fails the
    # task with a clear error, before any git command runs.
    assert task.template_name is not None  # guaranteed by the spawn route
    try:
        template = get_template(engine, task.template_name)
    except TemplateNotFoundError:
        stderr = f"template {task.template_name!r} no longer exists; cannot spawn"
        failed = mark_failed(engine, task_id, stderr)
        events.publish("task_updated", asdict(failed))
        return
    project = get_project(engine, template.project_name)
    # Effective omp settings: spawn-time override ?? template value ?? omitted
    # (omp default). Overrides are spawn-time only — never persisted; the
    # workflow engine carries them to every lazy session spawn of the run.
    effective_model = model_override if model_override is not None else template.model
    effective_thinking = (
        thinking_override if thinking_override is not None else template.thinking
    )

    if Path(task.clone_path).exists():
        # Crash residue from an earlier spawn: fail loudly, never reuse.
        stderr = f"target directory already exists: {task.clone_path}"
        events.publish(
            "spawn_step",
            {"task_id": task_id, "step": "clone", "status": "failed", "stderr": stderr},
        )
        failed = mark_failed(engine, task_id, stderr)
        events.publish("task_updated", asdict(failed))
        return

    async def run_clone_step(step: Step) -> None:
        await _run_step(step)
        if step.name == "clone":
            _exclude_outcome_dir(task.clone_path)

    async def run_workshop() -> None:
        # my-workshop creates/augments workshop.yaml, hides it from git, and
        # launches the container. Its contract with the daemon is only
        # "exit 0 and leave a .workshop.lock" (design D-1).
        await _run_step(
            Step(
                "workshop",
                [*config.my_workshop_command],
                config.workshop_step_timeout,
                cwd=task.clone_path,
            )
        )
        lock_id = _read_workshop_lock(task.clone_path)
        mark_workshop_launched(engine, task_id, lock_id)

    def subprocess_runner(step: Step) -> Callable[[], Awaitable[None]]:
        async def run() -> None:
            await run_clone_step(step)

        return run

    steps: list[tuple[str, Callable[[], Awaitable[None]]]] = [
        (step.name, subprocess_runner(step))
        for step in _git_steps(config, project, template, task)
    ]
    steps.append(("workshop", run_workshop))

    for name, runner_fn in steps:
        events.publish("spawn_step", {"task_id": task_id, "step": name, "status": "started"})
        try:
            await runner_fn()
        except StepFailedError as exc:
            events.publish(
                "spawn_step",
                {"task_id": task_id, "step": name, "status": "failed", "stderr": exc.stderr},
            )
            failed = mark_failed(engine, task_id, f"step {name!r} failed:\n{exc.stderr}")
            events.publish("task_updated", asdict(failed))
            return
        events.publish("spawn_step", {"task_id": task_id, "step": name, "status": "ok"})

    # Workspace ready: record spawn completion (its startup-reconciliation
    # meaning is unchanged), then hand the task to the workflow engine —
    # session spawn and prompt delivery are workflow execution now.
    completed = mark_spawn_completed(engine, task_id)
    events.publish("task_updated", asdict(completed))
    try:
        runner.start_run(
            completed, template, model=effective_model, thinking=effective_thinking
        )
    except UnknownWorkflowNameError as exc:
        failed = mark_failed(engine, task_id, str(exc))
        events.publish("task_updated", asdict(failed))
