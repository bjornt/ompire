"""Spawn pipeline: fetch → hardlink clone → branch → workshop launch
(SPEC Decision 5, steps 1–2).

Runs as an asyncio background job after POST /api/tasks returns 202. Each step
is a subprocess exec'd with an argument list (no shell), bounded by a
per-step timeout, with stderr captured. Progress is ephemeral — broadcast as
`spawn_step` events, never persisted; the registry records only the outcome
(and, for the workshop step, the launched workshop's lock id).
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy import Engine

from ompire_daemon.config import Config
from ompire_daemon.events import EventHub
from ompire_daemon.registry.projects import Project
from ompire_daemon.registry.tasks import (
    Task,
    get_task,
    mark_failed,
    mark_spawn_completed,
    mark_workshop_launched,
)

_STDERR_LIMIT = 64 * 1024

WORKSHOP_LOCK_FILENAME = ".workshop.lock"


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
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    stderr = stderr_bytes[-_STDERR_LIMIT:].decode("utf-8", errors="replace")
    if process.returncode != 0:
        raise StepFailedError(step.name, stderr)
    return stderr


def _pipeline_steps(config: Config, project: Project, task: Task) -> list[Step]:
    clone_path = task.clone_path
    git_timeout = config.spawn_step_timeout
    return [
        Step("fetch", ["git", "-C", project.checkout_path, "fetch", "origin"], git_timeout),
        # Local source path => hardlink clone, near-instant.
        Step("clone", ["git", "clone", project.checkout_path, clone_path], git_timeout),
        Step(
            "branch",
            ["git", "-C", clone_path, "checkout", "-b", task.branch, f"origin/{project.base_branch}"],
            git_timeout,
        ),
        # my-workshop creates/augments workshop.yaml, hides it from git, and
        # launches the container. Its contract with the daemon is only
        # "exit 0 and leave a .workshop.lock" (design D-1).
        Step(
            "workshop",
            [*config.my_workshop_command],
            config.workshop_step_timeout,
            cwd=clone_path,
        ),
    ]


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
    project: Project,
) -> None:
    task = get_task(engine, task_id)

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

    for step in _pipeline_steps(config, project, task):
        events.publish("spawn_step", {"task_id": task_id, "step": step.name, "status": "started"})
        try:
            await _run_step(step)
            if step.name == "workshop":
                lock_id = _read_workshop_lock(task.clone_path)
                mark_workshop_launched(engine, task_id, lock_id)
        except StepFailedError as exc:
            events.publish(
                "spawn_step",
                {"task_id": task_id, "step": step.name, "status": "failed", "stderr": exc.stderr},
            )
            failed = mark_failed(engine, task_id, f"step {step.name!r} failed:\n{exc.stderr}")
            events.publish("task_updated", asdict(failed))
            return
        except TimeoutError:
            stderr = f"timed out after {step.timeout}s"
            events.publish(
                "spawn_step",
                {"task_id": task_id, "step": step.name, "status": "failed", "stderr": stderr},
            )
            failed = mark_failed(engine, task_id, f"step {step.name!r} {stderr}")
            events.publish("task_updated", asdict(failed))
            return
        events.publish("spawn_step", {"task_id": task_id, "step": step.name, "status": "ok"})

    completed = mark_spawn_completed(engine, task_id)
    events.publish("task_updated", asdict(completed))
