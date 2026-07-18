"""Spawn pipeline v1: fetch → hardlink clone → branch (SPEC Decision 5, step 1).

Runs as an asyncio background job after POST /api/tasks returns 202. Each step
is a git subprocess exec'd with an argument list (no shell), bounded by the
configured timeout, with stderr captured. Progress is ephemeral — broadcast as
`spawn_step` events, never persisted; the registry records only the outcome.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path

from sqlalchemy import Engine

from ompire_daemon.config import Config
from ompire_daemon.events import EventHub
from ompire_daemon.registry.projects import Project
from ompire_daemon.registry.tasks import Task, get_task, mark_failed, mark_spawn_completed

_STDERR_LIMIT = 64 * 1024


class StepFailedError(Exception):
    def __init__(self, step: str, stderr: str) -> None:
        super().__init__(f"spawn step {step!r} failed")
        self.step = step
        self.stderr = stderr


async def _run_git(args: list[str], timeout: int) -> str:
    """Run git with the given args; return stderr text. Raises on failure/timeout."""
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    stderr = stderr_bytes[-_STDERR_LIMIT:].decode("utf-8", errors="replace")
    if process.returncode != 0:
        raise StepFailedError("", stderr)
    return stderr


def _pipeline_steps(project: Project, task: Task) -> list[tuple[str, list[str]]]:
    clone_path = task.clone_path
    return [
        ("fetch", ["-C", project.checkout_path, "fetch", "origin"]),
        # Local source path => hardlink clone, near-instant.
        ("clone", ["clone", project.checkout_path, clone_path]),
        (
            "branch",
            ["-C", clone_path, "checkout", "-b", task.branch, f"origin/{project.base_branch}"],
        ),
    ]


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

    for step, args in _pipeline_steps(project, task):
        events.publish("spawn_step", {"task_id": task_id, "step": step, "status": "started"})
        try:
            await _run_git(args, config.spawn_step_timeout)
        except StepFailedError as exc:
            events.publish(
                "spawn_step",
                {"task_id": task_id, "step": step, "status": "failed", "stderr": exc.stderr},
            )
            failed = mark_failed(engine, task_id, f"step {step!r} failed:\n{exc.stderr}")
            events.publish("task_updated", asdict(failed))
            return
        except TimeoutError:
            stderr = f"timed out after {config.spawn_step_timeout}s"
            events.publish(
                "spawn_step",
                {"task_id": task_id, "step": step, "status": "failed", "stderr": stderr},
            )
            failed = mark_failed(engine, task_id, f"step {step!r} {stderr}")
            events.publish("task_updated", asdict(failed))
            return
        events.publish("spawn_step", {"task_id": task_id, "step": step, "status": "ok"})

    completed = mark_spawn_completed(engine, task_id)
    events.publish("task_updated", asdict(completed))
