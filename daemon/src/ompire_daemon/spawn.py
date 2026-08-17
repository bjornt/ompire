"""Spawn pipeline: fetch → hardlink clone → branch → workshop launch →
agent start → prompt delivery (SPEC Decision 5, steps 1–4).

Runs as an asyncio background job after POST /api/tasks returns 202. The
first four steps are subprocesses exec'd with argument lists (no shell),
bounded by per-step timeouts, with stderr captured; the `agent` and `prompt`
steps are coroutines against the supervisor (design D-5). All six share the
same `spawn_step` started/ok/failed event contract. Progress is ephemeral —
broadcast, never persisted; the registry records only the outcome (and, for
the workshop step, the launched workshop's lock id).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy import Engine

from ompire_daemon.agent import AgentStartError, AgentSupervisor
from ompire_daemon.config import Config
from ompire_daemon.events import EventHub
from ompire_daemon.registry.projects import Project, get_project
from ompire_daemon.registry.tasks import (
    Task,
    get_task,
    mark_failed,
    mark_session_id,
    mark_spawn_completed,
    mark_workshop_launched,
)
from ompire_daemon.registry.templates import Template, TemplateNotFoundError, get_template
from ompire_daemon.rpc import AgentGoneError, RequestFailedError
from ompire_daemon.sessions import SessionTracker

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
    supervisor: AgentSupervisor,
    tracker: SessionTracker,
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
        tracker.spawn_step_failed(task_id, "template missing at pipeline start")
        events.publish("task_updated", asdict(failed))
        return
    project = get_project(engine, template.project_name)
    # Effective omp settings: spawn-time override ?? template value ?? omitted
    # (omp default). Overrides are spawn-time only — never persisted.
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

    async def run_agent() -> None:
        # The ask-timeout preflight and ready handshake live in the
        # supervisor; the ready timeout bounds the whole start (design D-5).
        try:
            handle = await supervisor.start(
                task_id,
                task.clone_path,
                model=effective_model,
                thinking=effective_thinking,
            )
        except AgentStartError as exc:
            detail = str(exc) if not exc.stderr else f"{exc}\n{exc.stderr}"
            raise StepFailedError("agent", detail) from exc
        except Exception as exc:  # noqa: BLE001 — e.g. a concurrent-start race
            raise StepFailedError("agent", str(exc)) from exc
        # Best-effort session-id capture (crash-recovery capability, design
        # D-2): a fresh spawn only — resumes already know their id. A capture
        # miss is logged inside `read_session_id` and never fails the spawn.
        session_id = await handle.read_session_id()
        if session_id is not None:
            updated = mark_session_id(engine, task_id, session_id)
            events.publish("task_updated", asdict(updated))

    # The prompt step sends preamble + blank line + prompt when the template
    # has a preamble (design D-3); an empty stored prompt still skips the
    # step entirely — a preamble alone never prompts.
    effective_prompt = (
        f"{template.preamble}\n\n{task.prompt}"
        if task.prompt and template.preamble
        else task.prompt
    )

    async def run_prompt() -> None:
        handle = supervisor.get(task_id)
        if handle is None:
            raise StepFailedError("prompt", "agent is no longer live")
        try:
            # The ack is a receipt ("queued"), not turn completion (spike
            # finding): the step is done once omp has accepted the prompt.
            await asyncio.wait_for(
                handle.prompt(effective_prompt), timeout=config.spawn_step_timeout
            )
        except TimeoutError:
            raise StepFailedError(
                "prompt", f"no ack within {config.spawn_step_timeout}s"
            ) from None
        except (RequestFailedError, AgentGoneError) as exc:
            raise StepFailedError("prompt", str(exc)) from exc

    def subprocess_runner(step: Step) -> Callable[[], Awaitable[None]]:
        async def run() -> None:
            await _run_step(step)

        return run

    steps: list[tuple[str, Callable[[], Awaitable[None]]]] = [
        (step.name, subprocess_runner(step))
        for step in _git_steps(config, project, template, task)
    ]
    steps.append(("workshop", run_workshop))
    steps.append(("agent", run_agent))
    if task.prompt:
        steps.append(("prompt", run_prompt))

    for name, runner in steps:
        events.publish("spawn_step", {"task_id": task_id, "step": name, "status": "started"})
        try:
            await runner()
        except StepFailedError as exc:
            events.publish(
                "spawn_step",
                {"task_id": task_id, "step": name, "status": "failed", "stderr": exc.stderr},
            )
            failed = mark_failed(engine, task_id, f"step {name!r} failed:\n{exc.stderr}")
            # A session only exists once the agent step began; earlier
            # failures no-op in the tracker (design D-2).
            tracker.spawn_step_failed(task_id, f"spawn step {name!r} failed")
            events.publish("task_updated", asdict(failed))
            return
        events.publish("spawn_step", {"task_id": task_id, "step": name, "status": "ok"})

    completed = mark_spawn_completed(engine, task_id)
    events.publish("task_updated", asdict(completed))
    if not task.prompt:
        # Promptless task: ready → idle instead of hanging in starting.
        tracker.prompt_skipped(task_id)
