"""Workflow engine (SPEC Decision 8; design D-1..D-10).

Workflows are frozen Python dataclasses registered by name in a module-level
registry — no declarative DSL in v1. A run executes its steps sequentially
(linear fall-through; a `decision` step's route is the only jump) with run
state persisted on the task row and one history row per executed step, so a
daemon restart re-drives the run from persisted state (design D-6).

Sessions are declared up front and spawned lazily on first use by an `agent`
step (design D-1); all of a task's sessions share the task's clone and
workshop container — the working tree is the primary handoff channel between
steps. The `.ompire/outcome.json` file is the deterministic-first secondary
channel (design D-3): an `expects_outcome` agent step's prompt carries a
fixed instruction block naming the path and schema; the engine unlinks any
stale file before prompting and records the parsed document (or NULL with a
note in the record's error field) at the session's debounced idle.

The registry contains exactly `single-step` until ROADMAP #18; tests register
their own workflows via `register_workflow`/`unregister_workflow`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import traceback
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import Engine as SAEngine

from ompire_daemon.agent import AgentSupervisor
from ompire_daemon.config import Config
from ompire_daemon.events import EventHub
from ompire_daemon.registry.sessions import mark_session_id, record_session_spawned
from ompire_daemon.registry.workflows import (
    StepRecord,
    append_step_record,
    finish_step_record,
    list_step_records,
    mark_prompt_sent,
    set_gate_waiting,
    set_run_failed,
    set_run_status,
)
from ompire_daemon.rpc import AgentGoneError, RequestFailedError
from ompire_daemon.sessions import SessionTracker

if TYPE_CHECKING:
    from ompire_daemon.registry.tasks import Task
    from ompire_daemon.registry.templates import Template

logger = logging.getLogger(__name__)

# --- outcome-file convention (design D-3) ------------------------------------

OUTCOME_PATH = ".ompire/outcome.json"

OUTCOME_INSTRUCTION = f"""\
When you have finished the work above, write your result as JSON to \
`{OUTCOME_PATH}` with exactly this schema:
{{
  "version": 1,
  "status": "success" | "failed",
  "summary": "<one-paragraph human-readable result>",
  "artifacts": {{ "<name>": "<value>", ... }}   // optional
}}"""

# Sent once to a session whose in-flight turn was lost to a daemon restart
# (design D-6); the resumed session retains its context, so this only asks it
# to continue (and re-states the outcome instruction when the step is
# outcome-bearing).
RESUME_NUDGE = "The daemon restarted while you were working; please continue."

_COMMAND_OUTPUT_TAIL = 8 * 1024


# --- step/workflow definitions (design D-2) -----------------------------------


class RunContext(Protocol):
    """What a workflow's prompt/route/message callables see."""

    task: Task  # registry row (prompt, branch, paths, …)
    template: Template  # resolved at pipeline start (preamble, model, …)

    def outcome(self, step: str) -> dict[str, Any] | None:
        """The latest ok record's outcome for a step name, or None."""
        ...

    def records(self) -> list[StepRecord]:
        """The run's full ordered step history."""
        ...


@dataclass(frozen=True)
class AgentStep:
    name: str
    session: str  # must be in Workflow.sessions
    prompt: Callable[[RunContext], str]  # '' → no prompt sent
    expects_outcome: bool = False


@dataclass(frozen=True)
class CommandStep:
    name: str
    argv: tuple[str, ...]  # run via workshop exec in the clone
    timeout: float = 600.0
    # Authors MUST keep commands idempotent: a daemon restart mid-command
    # re-runs the step on recovery (design D-6).


@dataclass(frozen=True)
class DecisionStep:
    name: str
    route: Callable[[RunContext], str | None]  # next step name; None/raise → escalate


@dataclass(frozen=True)
class GateStep:
    name: str
    message: Callable[[RunContext], str]


Step = AgentStep | CommandStep | DecisionStep | GateStep

_SESSION_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def step_kind(step: Step) -> str:
    if isinstance(step, AgentStep):
        return "agent"
    if isinstance(step, CommandStep):
        return "command"
    if isinstance(step, DecisionStep):
        return "decision"
    return "gate"


class WorkflowDefinitionError(ValueError):
    """A registered workflow is malformed (duplicate steps, undeclared
    session, …). Raised at registration — i.e. at daemon startup."""


class UnknownWorkflowNameError(ValueError):
    def __init__(self, name: str) -> None:
        super().__init__(f"unknown workflow {name!r}")
        self.name = name


class WorkflowNotWaitingError(Exception):
    def __init__(self, task_id: int, status: str | None) -> None:
        super().__init__(
            f"task {task_id} workflow is not waiting (status: {status or 'none'})"
        )
        self.task_id = task_id
        self.status = status


@dataclass(frozen=True)
class Workflow:
    name: str
    sessions: tuple[str, ...]
    steps: tuple[Step, ...]  # unique names; declaration order = fall-through order
    primary: str = ""  # default: sessions[0]

    def __post_init__(self) -> None:
        # Startup validation (registration is at import time, so a bad
        # definition breaks daemon startup/tests, never a task at runtime).
        if not self.sessions:
            raise WorkflowDefinitionError(f"workflow {self.name!r} declares no sessions")
        for session in self.sessions:
            if not _SESSION_NAME_RE.match(session):
                raise WorkflowDefinitionError(
                    f"workflow {self.name!r} session {session!r} is not slug-format"
                )
        if len(set(self.sessions)) != len(self.sessions):
            raise WorkflowDefinitionError(f"workflow {self.name!r} declares duplicate sessions")
        primary = self.primary or self.sessions[0]
        if primary not in self.sessions:
            raise WorkflowDefinitionError(
                f"workflow {self.name!r} primary {primary!r} is not a declared session"
            )
        object.__setattr__(self, "primary", primary)
        if not self.steps:
            raise WorkflowDefinitionError(f"workflow {self.name!r} declares no steps")
        seen: set[str] = set()
        for step in self.steps:
            if step.name in seen:
                raise WorkflowDefinitionError(
                    f"workflow {self.name!r} declares duplicate step {step.name!r}"
                )
            seen.add(step.name)
            if isinstance(step, AgentStep) and step.session not in self.sessions:
                raise WorkflowDefinitionError(
                    f"workflow {self.name!r} step {step.name!r} names undeclared "
                    f"session {step.session!r}"
                )

    def step_named(self, name: str) -> Step | None:
        for step in self.steps:
            if step.name == name:
                return step
        return None

    def step_after(self, name: str) -> Step | None:
        """The fall-through target: the step declared after `name`."""
        names = [step.name for step in self.steps]
        try:
            index = names.index(name)
        except ValueError:
            return None
        return self.steps[index + 1] if index + 1 < len(self.steps) else None


_WORKFLOWS: dict[str, Workflow] = {}


def register_workflow(workflow: Workflow) -> None:
    if workflow.name in _WORKFLOWS:
        raise WorkflowDefinitionError(f"workflow {workflow.name!r} is already registered")
    _WORKFLOWS[workflow.name] = workflow


def unregister_workflow(name: str) -> None:
    """Test support: remove a workflow registered for one test."""
    _WORKFLOWS.pop(name, None)


def get_workflow(name: str) -> Workflow:
    try:
        return _WORKFLOWS[name]
    except KeyError:
        raise UnknownWorkflowNameError(name) from None


def registered_workflows() -> tuple[str, ...]:
    return tuple(sorted(_WORKFLOWS))


# --- the single-step workflow (design D-10) ------------------------------------


def join_preamble(preamble: str, prompt: str) -> str:
    """The pre-workflow concatenation, byte-identical: preamble + blank line +
    prompt when both are present (a preamble alone never prompts)."""
    if prompt and preamble:
        return f"{preamble}\n\n{prompt}"
    return prompt


register_workflow(
    Workflow(
        name="single-step",
        sessions=("main",),
        steps=(
            AgentStep(
                name="work",
                session="main",
                prompt=lambda ctx: join_preamble(ctx.template.preamble, ctx.task.prompt),
            ),
        ),
    )
)


# --- outcome reading (design D-3) ----------------------------------------------


def read_outcome(clone_path: str) -> tuple[dict[str, Any] | None, str | None]:
    """Read and validate `<clone>/.ompire/outcome.json`. Returns
    (outcome, None) on success or (None, note) on any absence/violation —
    never guessed, and never a step failure by itself."""
    path = Path(clone_path) / OUTCOME_PATH
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None, "no outcome file written"
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"outcome file is not valid JSON: {exc}"
    if not isinstance(document, dict):
        return None, "outcome file is not a JSON object"
    if document.get("version") != 1:
        return None, f"outcome version must be 1, got {document.get('version')!r}"
    if document.get("status") not in ("success", "failed"):
        return None, f"outcome status must be 'success' or 'failed', got {document.get('status')!r}"
    if not isinstance(document.get("summary"), str):
        return None, "outcome summary must be a string"
    artifacts = document.get("artifacts")
    if artifacts is not None and not (
        isinstance(artifacts, dict) and all(isinstance(k, str) for k in artifacts)
    ):
        return None, "outcome artifacts must be a string-keyed map"
    return document, None


# --- the runner (design D-2/D-4/D-6) -------------------------------------------


class _StepInfraFailure(Exception):
    """The step could not be executed at all (session spawn failure,
    `workshop exec` failure, agent exit mid-step): the run lands `failed`
    with the error, sessions stay alive for the escape hatch."""


@dataclass(frozen=True)
class _StepResult:
    outcome: dict[str, Any] | None = None
    error_note: str | None = None  # recorded on an otherwise-ok record
    route: str | None = None  # decision steps: explicit next step name
    gate_message: str | None = None  # park the run here


class _NudgedAgentStep:
    """Marker wrapper for an agent step recovered after its prompt was
    already sent (design D-6): the resumed session retains its context, so
    the engine sends the fixed resume nudge instead of the full prompt, and
    never unlinks a possibly-already-written outcome file."""

    def __init__(self, step: AgentStep) -> None:
        self._step = step
        self.name = step.name
        self.session = step.session
        self.expects_outcome = step.expects_outcome

    @property
    def nudge(self) -> str:
        if self.expects_outcome:
            return f"{RESUME_NUDGE} Finish by writing `{OUTCOME_PATH}`."
        return RESUME_NUDGE


class _RunContext:
    """RunContext over the step history captured at step start."""

    def __init__(self, task: Task, template: Template, records: list[StepRecord]) -> None:
        self.task = task
        self.template = template
        self._records = records

    def outcome(self, step: str) -> dict[str, Any] | None:
        for record in reversed(self._records):
            if record.step == step and record.status == "ok":
                return record.outcome
        return None

    def records(self) -> list[StepRecord]:
        return list(self._records)


class WorkflowRunner:
    """Executes workflow runs; one run per task at a time, steps strictly
    sequential. Run state lives in the registry (task row + step records);
    this class holds only the in-memory asyncio machinery (run tasks, gate
    futures), so a daemon restart re-drives from persisted state."""

    def __init__(
        self,
        engine: SAEngine,
        config: Config,
        events: EventHub,
        supervisor: AgentSupervisor,
        tracker: SessionTracker,
    ) -> None:
        self._engine = engine
        self._config = config
        self._hub = events
        self._supervisor = supervisor
        self._tracker = tracker
        self._runs: dict[int, asyncio.Task] = {}
        # task_id → future completed with the operator's note on gate resume.
        self._gate_waits: dict[int, asyncio.Future[str | None]] = {}

    # --- public surface -------------------------------------------------------

    def start_run(
        self,
        task: Task,
        template: Template,
        *,
        model: str | None = None,
        thinking: str | None = None,
    ) -> None:
        """Begin the task's workflow from its first step (spawn-pipeline
        handoff, design D-4). Model/thinking are the spawn-resolved effective
        values, carried to every lazy session spawn of the run."""
        if task.id in self._runs:
            logger.warning("task %d already has a workflow run; ignoring", task.id)
            return
        workflow = get_workflow(task.workflow_name)
        updated = set_run_status(self._engine, task.id, "running", None)
        self._publish_task_updated(updated)
        self._kick(task.id, template, workflow, model=model, thinking=thinking, recover=False)

    def recover_run(
        self,
        task: Task,
        template: Template,
        *,
        model: str | None = None,
        thinking: str | None = None,
    ) -> None:
        """Re-drive a `running`/`waiting` run from persisted state after a
        daemon restart (design D-6). Runs `complete`/`failed` are never
        re-driven. Sessions must already be resumed (crash-recovery)."""
        if task.id in self._runs or task.workflow_status not in ("running", "waiting"):
            return
        workflow = get_workflow(task.workflow_name)
        self._kick(task.id, template, workflow, model=model, thinking=thinking, recover=True)

    def resume_gate(self, task_id: int, *, note: str | None) -> None:
        """Operator resume for a parked gate: finishes the gate `ok` with the
        note as its outcome and continues the run at the gate's
        fall-through. Raises WorkflowNotWaitingError unless the run is
        actually parked."""
        from ompire_daemon.registry.tasks import get_task

        task = get_task(self._engine, task_id)  # TaskNotFoundError → caller's 404
        future = self._gate_waits.get(task_id)
        if task.workflow_status != "waiting" or future is None or future.done():
            raise WorkflowNotWaitingError(task_id, task.workflow_status)
        future.set_result(note)

    async def shutdown(self) -> None:
        """Cancel in-memory runs on daemon shutdown; persisted state is the
        recovery input for the next startup."""
        runs = list(self._runs.values())
        for run in runs:
            run.cancel()
        if runs:
            await asyncio.gather(*runs, return_exceptions=True)

    # --- run loop --------------------------------------------------------------

    def _kick(
        self,
        task_id: int,
        template: Template,
        workflow: Workflow,
        *,
        model: str | None,
        thinking: str | None,
        recover: bool,
    ) -> None:
        run = asyncio.create_task(
            self._execute(task_id, template, workflow, model=model, thinking=thinking, recover=recover)
        )
        self._runs[task_id] = run
        run.add_done_callback(lambda t: self._run_done(task_id, t))

    def _run_done(self, task_id: int, run: asyncio.Task) -> None:
        if self._runs.get(task_id) is run:
            self._runs.pop(task_id, None)
        if run.cancelled():
            return
        exc = run.exception()
        if exc is not None:
            # Unreachable by construction (steps are wrapped) — last line of
            # defense so a runner bug fails the run instead of vanishing.
            error = "".join(traceback.format_exception(exc))
            logger.error("workflow run for task %d crashed:\n%s", task_id, error)
            finish_note = f"workflow runner crashed: {exc}"
            updated = set_run_failed(self._engine, task_id, finish_note)
            self._publish_task_updated(updated)

    async def _execute(
        self,
        task_id: int,
        template: Template,
        workflow: Workflow,
        *,
        model: str | None,
        thinking: str | None,
        recover: bool,
    ) -> None:
        from ompire_daemon.registry.tasks import get_task

        records = list_step_records(self._engine, task_id)
        if recover:
            step = await self._recover_step(task_id, template, workflow, records)
            if step is None:
                return  # run completed, failed, or re-parked during recovery
        else:
            step = workflow.steps[0]

        while step is not None:
            task = get_task(self._engine, task_id)
            ctx = _RunContext(task, template, list_step_records(self._engine, task_id))
            is_nudge = isinstance(step, _NudgedAgentStep)
            declared = step._step if is_nudge else step  # noqa: SLF001
            record = append_step_record(
                self._engine,
                task_id,
                step=declared.name,
                kind="agent" if is_nudge else step_kind(declared),
                session=declared.session if isinstance(declared, AgentStep) else None,
            )
            updated = set_run_status(self._engine, task_id, "running", declared.name)
            self._publish_task_updated(updated)
            self._publish_step(task_id, declared, "started")
            try:
                result = await self._run_step(step, ctx, record, model, thinking)
            except _StepInfraFailure as exc:
                self._fail_step(task_id, declared, record.seq, str(exc))
                return
            except Exception:  # noqa: BLE001 — a buggy step must not kill sessions
                error = traceback.format_exc()
                logger.error(
                    "workflow step %r raised for task %d:\n%s", declared.name, task_id, error
                )
                self._fail_step(task_id, declared, record.seq, error)
                return

            if result.gate_message is not None:
                step = await self._park_at_gate(
                    task_id, workflow, declared, record.seq, result
                )
                continue

            finish_step_record(
                self._engine,
                task_id,
                record.seq,
                status="ok",
                outcome=result.outcome,
                error=result.error_note,
            )
            self._publish_step(task_id, declared, "ok")
            if result.route is not None:
                target = workflow.step_named(result.route)
                if target is None:
                    # Route names a step that does not exist: escalate rather
                    # than guess (design D-2).
                    step = GateStep(
                        name=declared.name,
                        message=lambda ctx, r=result.route, d=declared.name: (
                            f"decision {d!r} routed to unknown step {r!r}; "
                            "resume to continue at the step after the decision"
                        ),
                    )
                    continue
                step = target
            else:
                step = workflow.step_after(declared.name)

        updated = set_run_status(self._engine, task_id, "complete", None)
        self._publish_task_updated(updated)

    def _fail_step(self, task_id: int, step: Step, seq: int, error: str) -> None:
        finish_step_record(self._engine, task_id, seq, status="failed", error=error)
        self._publish_step(task_id, step, "failed", error=error)
        updated = set_run_failed(self._engine, task_id, error)
        self._publish_task_updated(updated)

    async def _park_at_gate(
        self,
        task_id: int,
        workflow: Workflow,
        step: Step,
        seq: int,
        result: _StepResult,
    ) -> Step | None:
        """Persist the gate wait, park until the operator resumes, then
        continue at the step's fall-through (a synthesized decision-escalation
        gate shares the decision's name, so its fall-through is the step after
        the decision)."""
        message = result.gate_message
        assert message is not None
        if step_kind(step) == "decision":
            # Escalation: the decision record finishes ok (route unresolvable
            # is data, per design D-3's missing-outcome stance) and the gate
            # is synthesized as a separate record under the decision's name.
            finish_step_record(
                self._engine, task_id, seq, status="ok", outcome=None, error=message
            )
            self._publish_step(task_id, step, "ok")
            gate_record = append_step_record(
                self._engine, task_id, step=step.name, kind="gate"
            )
            seq = gate_record.seq
        set_gate_waiting(self._engine, task_id, seq, message=message)
        updated = set_run_status(self._engine, task_id, "waiting", step.name)
        self._publish_task_updated(updated)
        self._publish_gate_step(task_id, step.name, "waiting", message)
        return await self._await_gate(task_id, workflow, seq, step.name, message)

    async def _await_gate(
        self, task_id: int, workflow: Workflow, seq: int, step_name: str, message: str
    ) -> Step | None:
        """Park until the operator resumes (or shutdown cancels); finish the
        gate record `ok` with the note and return the fall-through step."""
        future: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()
        self._gate_waits[task_id] = future
        try:
            note = await future
        finally:
            self._gate_waits.pop(task_id, None)
        finish_step_record(
            self._engine,
            task_id,
            seq,
            status="ok",
            outcome={"message": message, "note": note},
        )
        self._publish_gate_step(task_id, step_name, "ok")
        updated = set_run_status(self._engine, task_id, "running", step_name)
        self._publish_task_updated(updated)
        return workflow.step_after(step_name)

    # --- restart recovery (design D-6) ------------------------------------------

    async def _recover_step(
        self,
        task_id: int,
        template: Template,
        workflow: Workflow,
        records: list[StepRecord],
    ) -> Step | None:
        """Compute where a recovered run continues and re-drive the
        interrupted step per kind. Returns the step to enter the main loop
        with, or None when recovery itself finished/parked the run."""
        last = records[-1] if records else None
        if last is None:
            return workflow.steps[0]
        if last.status == "waiting":
            # Gate: re-arm the SAME record (history stays one row) and
            # re-broadcast the persisted message.
            message = (last.outcome or {}).get("message")
            if not isinstance(message, str) or not message:
                message = "workflow gate"
            updated = set_run_status(self._engine, task_id, "waiting", last.step)
            self._publish_task_updated(updated)
            self._publish_gate_step(task_id, last.step, "waiting", message)
            return await self._await_gate(task_id, workflow, last.seq, last.step, message)
        if last.status == "running":
            # The shutdown interrupted this step. Close its record and
            # re-drive per kind (commands/decisions re-execute; agent steps
            # send fresh or nudge depending on whether the prompt went out).
            finish_step_record(
                self._engine,
                task_id,
                last.seq,
                status="failed",
                error="interrupted by daemon restart",
            )
            step = workflow.step_named(last.step)
            if step is None:
                logger.warning(
                    "task %d workflow %r no longer declares step %r; failing the run",
                    task_id,
                    workflow.name,
                    last.step,
                )
                updated = set_run_failed(
                    self._engine, task_id, f"step {last.step!r} no longer declared"
                )
                self._publish_task_updated(updated)
                return None
            if last.kind == "agent" and last.prompted_at is not None:
                return _NudgedAgentStep(step)  # type: ignore[arg-type]
            return step
        # Last record finished ok: a decision routes explicitly (its recorded
        # route); anything else falls through.
        if last.kind == "decision" and last.outcome is not None:
            route = last.outcome.get("route")
            if isinstance(route, str):
                target = workflow.step_named(route)
                if target is not None:
                    return target
        return workflow.step_after(last.step)

    # --- step execution ----------------------------------------------------------

    async def _run_step(
        self,
        step: Step | _NudgedAgentStep,
        ctx: _RunContext,
        record: StepRecord,
        model: str | None,
        thinking: str | None,
    ) -> _StepResult:
        if isinstance(step, _NudgedAgentStep):
            return await self._run_agent_step(step, ctx, record, model, thinking)
        if isinstance(step, AgentStep):
            return await self._run_agent_step(step, ctx, record, model, thinking)
        if isinstance(step, CommandStep):
            return await self._run_command_step(step, ctx)
        if isinstance(step, DecisionStep):
            try:
                route = step.route(ctx)
            except Exception as exc:  # noqa: BLE001 — escalate, never guess
                return _StepResult(
                    gate_message=(
                        f"decision {step.name!r} could not resolve a route: {exc}; "
                        "resume to continue at the step after the decision"
                    )
                )
            if route is None:
                return _StepResult(
                    gate_message=(
                        f"decision {step.name!r} resolved no route (a required outcome "
                        "is missing?); resume to continue at the step after the decision"
                    )
                )
            return _StepResult(outcome={"route": route}, route=route)
        assert isinstance(step, GateStep)
        return _StepResult(gate_message=step.message(ctx))

    async def _run_agent_step(
        self,
        step: AgentStep | _NudgedAgentStep,
        ctx: _RunContext,
        record: StepRecord,
        model: str | None,
        thinking: str | None,
    ) -> _StepResult:
        task = ctx.task
        nudged = isinstance(step, _NudgedAgentStep)
        handle = self._supervisor.get(task.id, step.session)
        if handle is None or handle.returncode is not None:
            # Lazy spawn (design D-1): the same supervised start the old
            # pipeline used — ask-timeout preflight, ready handshake, then
            # per-session omp identity capture — with the spawn-resolved
            # effective model/thinking.
            try:
                handle = await self._supervisor.start(
                    task.id,
                    step.session,
                    task.clone_path,
                    model=model,
                    thinking=thinking,
                )
            except Exception as exc:
                detail = str(exc)
                stderr = getattr(exc, "stderr", "")
                if stderr:
                    detail = f"{detail}\n{stderr}"
                self._tracker.session_start_failed(
                    task.id, step.session, f"session spawn failed: {exc}"
                )
                raise _StepInfraFailure(detail) from exc
            record_session_spawned(self._engine, task.id, step.session)
            # Best-effort identity capture (crash-recovery): a miss is logged
            # inside `read_session_id` and never fails the step.
            session_id = await handle.read_session_id()
            if session_id is not None:
                mark_session_id(self._engine, task.id, step.session, session_id)

        if nudged:
            prompt = step.nudge
        else:
            prompt = step.prompt(ctx)
            if prompt and step.expects_outcome:
                # A stale file from an earlier step is never this step's
                # result (design D-3). Skipped for nudges: the agent may have
                # written the outcome before the restart.
                try:
                    (Path(task.clone_path) / OUTCOME_PATH).unlink()
                except FileNotFoundError:
                    pass
                prompt = f"{prompt}\n\n{OUTCOME_INSTRUCTION}"

        if not prompt:
            # Empty prompt: nothing sent; the step completes once the session
            # is ready (parity with the old promptless-spawn idle behavior).
            self._tracker.prompt_skipped(task.id, step.session)
            return self._outcome_result(task, step)

        try:
            # The ack is a receipt ("queued"), not turn completion.
            await asyncio.wait_for(
                handle.prompt(prompt), timeout=self._config.spawn_step_timeout
            )
        except (TimeoutError, RequestFailedError, AgentGoneError) as exc:
            raise _StepInfraFailure(f"prompt delivery failed: {exc}") from exc
        mark_prompt_sent(self._engine, task.id, record.seq)
        # Completion is the session's debounced idle (design D-2). A session
        # dying mid-step is an infra failure; a pending question just keeps
        # the run running until the operator answers and the turn ends.
        await self._await_step_idle(task.id, step.session)
        return self._outcome_result(task, step)

    def _outcome_result(
        self, task: Task, step: AgentStep | _NudgedAgentStep
    ) -> _StepResult:
        """Read the outcome file at the turn boundary (design D-3). Only
        outcome-bearing steps consult the file; a missing/malformed file is a
        null outcome with a note, never a failure."""
        if not step.expects_outcome:
            return _StepResult()
        outcome, note = read_outcome(task.clone_path)
        return _StepResult(outcome=outcome, error_note=note)

    async def _await_step_idle(self, task_id: int, session: str) -> None:
        """Wait for the debounced idle turn boundary, watching hub events;
        the session's exit underneath the step fails the run (infra)."""
        queue = self._hub.subscribe()
        try:
            while True:
                event = await queue.get()
                if event.type == "agent_exited":
                    payload = event.payload
                    if (
                        payload.get("task_id") == task_id
                        and payload.get("session") == session
                    ):
                        raise _StepInfraFailure(
                            f"session {session!r} exited mid-step "
                            f"(code {payload.get('exit_code')})"
                        )
                elif event.type == "status_changed":
                    payload = event.payload
                    if (
                        payload.get("task_id") == task_id
                        and payload.get("session") == session
                        and payload.get("to") == "idle"
                    ):
                        return
        finally:
            self._hub.unsubscribe(queue)

    async def _run_command_step(self, step: CommandStep, ctx: _RunContext) -> _StepResult:
        task = ctx.task
        argv = ["workshop", "exec", "-p", task.clone_path, "--", *step.argv]
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            raise _StepInfraFailure(f"cannot exec 'workshop': {exc}") from exc
        try:
            output_bytes, _ = await asyncio.wait_for(process.communicate(), timeout=step.timeout)
        except TimeoutError:
            process.kill()
            # `wait()` also waits for pipe EOF; a grandchild holding the pipe
            # (real workshop exec shape) must not hang the run — bound it.
            import contextlib

            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), timeout=5)
            raise _StepInfraFailure(f"command timed out after {step.timeout}s") from None
        tail = output_bytes[-_COMMAND_OUTPUT_TAIL:].decode("utf-8", errors="replace")
        # A non-zero exit is outcome DATA (routing on it is a following
        # decision step's job); only the inability to execute fails the run.
        return _StepResult(outcome={"exit_code": process.returncode, "output": tail})

    # --- events ------------------------------------------------------------------

    def _publish_task_updated(self, task: Task) -> None:
        self._hub.publish("task_updated", asdict(task))

    def _publish_step(self, task_id: int, step: Step, status: str, **extra: Any) -> None:
        self._hub.publish(
            "workflow_step",
            {
                "task_id": task_id,
                "step": step.name,
                "kind": step_kind(step),
                "session": step.session if isinstance(step, AgentStep) else None,
                "status": status,
                **extra,
            },
        )

    def _publish_gate_step(
        self, task_id: int, step_name: str, status: str, message: str | None = None
    ) -> None:
        """Gate transitions by name (the step object isn't always at hand —
        recovery re-arms from the persisted record; an escalation gate shares
        its decision's name)."""
        payload: dict[str, Any] = {
            "task_id": task_id,
            "step": step_name,
            "kind": "gate",
            "session": None,
            "status": status,
        }
        if message is not None:
            payload["message"] = message
        self._hub.publish("workflow_step", payload)
