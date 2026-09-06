"""Workflow engine: executing a task's *pinned* declarative definition.

Architecture: ADR-0008, ADR-0009, ADR-0027, ADR-0028
(docs/adr/0008-model-tasks-as-workflows-over-named-sessions.md,
docs/adr/0028-retain-declarative-workflow-revisions.md)

A definition is data (`workflow_definitions.py`), retained by content identity
(`registry/workflow_definitions.py`), and pinned to a task at acceptance. This
module is the part that *carries it out*: it resolves each step, prompts the
right session under the right accepted policy, records what happened, and
decides where the run goes next — always from the task's own revision, never
from whatever the workflow's name means today.

A run executes its steps sequentially. Fall-through follows declaration order;
a decision step is the only jump. Run state lives in the registry (task row
plus one record per attempt), so a daemon restart re-drives from persisted
state rather than from anything held in this process (design D-6).

Sessions are declared up front and spawned lazily on first use by an `agent`
step (design D-1); all of a task's sessions share the task's clone and
workshop container — the working tree is the primary handoff channel between
steps. `.ompire/outcome.json` is the deterministic-first secondary channel
(design D-3): an `expects_outcome` step's prompt carries a fixed instruction
block naming the path and schema, the engine unlinks any stale file before
prompting, and the parsed document is recorded at the session's debounced idle.

**There is no judge.** When the evidence a step or a route needs is missing or
malformed, the run pauses and says what it was waiting for. It does not ask a
model to classify the result, and it does not fall through as if the missing
evidence had been accepted — both of those turn "we do not know" into an
answer, which is the one thing an orchestrator must never do quietly. A
declared negative result (`status: "failed"`, a nonzero exit code) is not
missing evidence: it is data, and it follows the definition's own routes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import traceback
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import Engine as SAEngine

from ompire_daemon.agent import AgentSupervisor
from ompire_daemon.config import Config
from ompire_daemon.events import EventHub
from ompire_daemon.execution_inputs import (
    ConsumerBinding,
    MissingConsumerBindingError,
    ModelPolicy,
    TaskExecutionInputs,
)
from ompire_daemon.projectfiles import mention_tokens, unresolved_mentions
from ompire_daemon.registry.sessions import (
    build_applied_policy,
    mark_session_id,
    record_applied_policy,
    record_session_spawned,
)
from ompire_daemon.registry.tasks import require_task_inputs, task_payload
from ompire_daemon.registry.workflow_definitions import register_revisions
from ompire_daemon.registry.workflows import (
    PAUSE_CONDITION_UNRESOLVED,
    PAUSE_MISSING_OUTCOME,
    PAUSE_PROMPT_UNRENDERABLE,
    PAUSE_UNRESOLVED_DECISION,
    RETRY_NOTE,
    StepRecord,
    WorkflowWaitConflictError,
    append_step_record,
    build_pause,
    finish_step_record,
    latest_step_record,
    list_step_records,
    mark_prompt_sent,
    park_gate,
    pause_step,
    retry_paused_step,
    set_run_failed,
    set_run_status,
)
from ompire_daemon.rpc import AgentGoneError, RequestFailedError
from ompire_daemon.sessions import SessionTracker
from ompire_daemon.workflow_definitions import (
    AgentStep,
    CommandStep,
    DecisionStep,
    Destination,
    EvaluationContext,
    GateStep,
    HistoryRecord,
    PauseDestination,
    RenderError,
    Step,
    StepDestination,
    Unresolved,
    WorkflowDefinition,
    WorkflowDocumentError,
    WorkflowRevision,
    describe,
    evaluate_predicate,
    load_definition,
    render_text,
)

if TYPE_CHECKING:
    from ompire_daemon.registry.tasks import Task

logger = logging.getLogger(__name__)

# --- outcome-file convention (design D-3) ------------------------------------
# These are interpreter protocol constants of format 1, not adjustable global
# prompt configuration: changing what they mean to a running definition would
# change the meaning of every retained revision, which is what a new format
# version is for.

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

# Prefixed to a prompt the *operator* authorized retrying after an uncertainty
# pause. Deliberately not the restart nudge: the previous attempt ran to
# completion and may already have changed the working tree, so telling this
# turn to "continue" would invite it to redo side effects it cannot see.
RETRY_PREFIX = """\
Your previous attempt at this step finished without leaving a valid result, so \
an operator asked for another attempt. Work may already have been done and \
files may already have been changed: inspect the working tree first and finish \
what is missing rather than repeating anything. The original instruction \
follows."""

_COMMAND_OUTPUT_TAIL = 8 * 1024

# A decision's recorded route when it finishes the run. Deliberately not
# slug-format, so it can never collide with a declared step name, and
# unchanged from the pre-declarative engine so existing records still read.
COMPLETE = "__complete__"


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


# --- the packaged catalog (ADR-0028) ------------------------------------------
# Definitions ship with the daemon as package resources. In this change that is
# the *only* source: no project-directory scan, no upload, no CRUD, no plugin
# loader. A packaged definition that does not validate fails startup, because
# shipping an unexecutable built-in is a build error, not a runtime surprise.

BUILTIN_PACKAGE = "ompire_daemon.builtin_workflows"
BUILTIN_NAMES = ("single-step", "bugfix")

_catalog: dict[str, WorkflowRevision] = {}


class PackagedWorkflowError(RuntimeError):
    """A definition shipped with the daemon is not loadable. Fails startup."""


def load_packaged_workflows() -> dict[str, WorkflowRevision]:
    """Parse and validate every packaged definition, by package resource.

    Read through `importlib.resources`, so an installed daemon finds its
    definitions inside the wheel rather than relative to a checkout that is
    not there.
    """
    loaded: dict[str, WorkflowRevision] = {}
    for name in BUILTIN_NAMES:
        resource = resources.files(BUILTIN_PACKAGE) / f"{name}.yaml"
        try:
            text = resource.read_text(encoding="utf-8")
        except (OSError, FileNotFoundError) as exc:
            raise PackagedWorkflowError(
                f"packaged workflow {name!r} is missing from the installed package"
            ) from exc
        try:
            revision = load_definition(text)
        except WorkflowDocumentError as exc:
            raise PackagedWorkflowError(
                f"packaged workflow {name!r} is invalid: {exc}"
            ) from exc
        if revision.name != name:
            raise PackagedWorkflowError(
                f"packaged workflow {name!r} declares the name {revision.name!r}"
            )
        loaded[name] = revision
    return loaded


def catalog() -> dict[str, WorkflowRevision]:
    """The process-local name → current revision map."""
    if not _catalog:
        _catalog.update(load_packaged_workflows())
    return _catalog


def current_revision(name: str) -> WorkflowRevision:
    """What a *new* launch of this name would pin. Never used to resolve an
    already-accepted task: that is the whole point of pinning."""
    try:
        return catalog()[name]
    except KeyError:
        raise UnknownWorkflowNameError(name) from None


def catalog_names() -> tuple[str, ...]:
    return tuple(sorted(catalog()))


def describe_catalog() -> list[Any]:
    """Every installed definition, in name order. Installed definitions change
    only with the daemon, so the catalog is constant for the life of the
    process and carries no change event."""
    return [describe(catalog()[name]) for name in catalog_names()]


def install_definition(revision: WorkflowRevision) -> None:
    """Add one definition to the process catalog.

    The seam startup registration and tests both use. It does not retain the
    revision — `register_catalog` does that — because entering the catalog and
    being durably readable are two different facts.
    """
    _catalog.update(catalog())
    _catalog[revision.name] = revision


def uninstall_definition(name: str) -> None:
    """Test support: remove a definition installed for one test."""
    _catalog.pop(name, None)


def reset_catalog() -> None:
    """Test support: forget installed definitions, packaged ones included."""
    _catalog.clear()


def register_catalog(engine: SAEngine) -> list[str]:
    """Retain every installed definition's revision. Runs at startup, before
    launch initialization and recovery, so nothing can resolve a task against
    a revision the database does not hold."""
    return register_revisions(engine, [catalog()[name] for name in catalog_names()])


# --- outcome reading (design D-3) --------------------------------------------


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


# --- the runner ---------------------------------------------------------------


class _StepInfraFailure(Exception):
    """The step could not be executed at all (session spawn failure,
    `workshop exec` failure, agent exit mid-step): the run lands `failed`
    with the error, sessions stay alive for the escape hatch."""


@dataclass(frozen=True)
class _Pause:
    reason: str
    message: str
    error: str


@dataclass(frozen=True)
class _StepResult:
    outcome: dict[str, Any] | None = None
    error_note: str | None = None  # recorded on an otherwise-ok record
    destination: Destination | None = None  # decisions: the chosen route
    gate_message: str | None = None  # a declared gate parks the run here
    pause: _Pause | None = None  # the engine will not guess


@dataclass(frozen=True)
class _Attempt:
    """One persisted attempt the loop is about to drive.

    Always a row that already exists. Normal entry, an operator retry, and
    restart recovery all hand the loop a record rather than each appending one
    of their own — which is what keeps a declared visit bound counting *work*
    attempts instead of daemon restarts.
    """

    record: StepRecord
    step: Step
    nudge: bool = False  # an interrupted sent prompt: resume, do not re-send
    retry: bool = False  # an operator-authorized retry after a pause


def evaluation_context(
    task: Task, inputs: TaskExecutionInputs, records: list[StepRecord], before_seq: int
) -> EvaluationContext:
    """What a definition may read at this attempt's entry.

    The history is cut at the current attempt's own sequence, so a step's
    prompt sees what happened *before* it — which is how a retried `fix` can
    carry the previous iteration's rejection report without reading its own
    empty record.
    """
    return EvaluationContext(
        inputs={
            "task.prompt": task.prompt,
            "task.slug": task.slug,
            "task.branch": task.branch,
            "workspace.preamble": inputs.preamble,
        },
        records=tuple(
            HistoryRecord(
                seq=record.seq,
                step=record.step,
                status=record.status,
                outcome=record.outcome,
            )
            for record in records
            if record.seq < before_seq
        ),
    )


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

    def start_run(self, task: Task, revision: WorkflowRevision) -> None:
        """Begin the task's workflow from its first step (spawn-pipeline
        handoff, design D-4).

        Everything the run needs comes off the task itself: the inputs it was
        accepted under carry the preamble, the role bindings, the branch, and
        the definition revision. Nothing is re-read from the project, the
        profile registry, or the workflow catalog, so an edit made while the
        task was queuing cannot change what runs.
        """
        if task.id in self._runs:
            logger.warning("task %d already has a workflow run; ignoring", task.id)
            return
        inputs = require_task_inputs(task)
        updated = set_run_status(self._engine, task.id, "running", None)
        self._publish_task_updated(updated)
        self._kick(task.id, inputs, revision, recover=False)

    def recover_run(self, task: Task, revision: WorkflowRevision) -> None:
        """Re-drive a `running`/`waiting` run from persisted state after a
        daemon restart (design D-6). Runs `complete`/`failed` are never
        re-driven. Sessions must already be resumed (crash-recovery)."""
        if task.id in self._runs or task.workflow_status not in ("running", "waiting"):
            return
        inputs = require_task_inputs(task)
        self._kick(task.id, inputs, revision, recover=True)

    def resume_gate(self, task_id: int, *, expected_seq: int, note: str | None) -> None:
        """Operator resume for a parked declared gate.

        `expected_seq` names the attempt the operator was actually looking at.
        A stale tab and a double submit are indistinguishable here, and both
        must be refused rather than applied to whatever the run is waiting on
        now.
        """
        from ompire_daemon.registry.tasks import get_task

        task = get_task(self._engine, task_id)  # TaskNotFoundError → caller's 404
        record = latest_step_record(self._engine, task_id)
        future = self._gate_waits.get(task_id)
        if task.workflow_status != "waiting" or record is None or record.status != "waiting":
            raise WorkflowNotWaitingError(task_id, task.workflow_status)
        if record.seq != expected_seq:
            raise WorkflowWaitConflictError(task_id, expected_seq, record.seq)
        if record.pause is not None:
            raise WorkflowNotWaitingError(task_id, "uncertainty-pause")
        if future is None or future.done():
            raise WorkflowNotWaitingError(task_id, task.workflow_status)
        future.set_result(note)

    def retry_step(
        self, task: Task, revision: WorkflowRevision, *, expected_seq: int
    ) -> Task:
        """Retry the attempt an uncertainty pause is waiting on.

        The transaction is committed before anything is scheduled, so a crash
        immediately after authorization leaves an ordinary interrupted attempt
        for the next startup to re-drive — never a lost authorization, and
        never a second attempt appended for the same decision.
        """
        inputs = require_task_inputs(task)
        record, updated = retry_paused_step(
            self._engine,
            task.id,
            expected_seq,
            target=self._retry_target(task.id, revision.definition, expected_seq),
        )
        step = revision.definition.step_named(record.step)
        if step is None:
            failed = set_run_failed(
                self._engine,
                task.id,
                f"step {record.step!r} is not declared by the pinned workflow revision",
            )
            self._publish_task_updated(failed)
            return failed
        self._publish_task_updated(updated)
        # No `started` here: the run loop publishes it for this same attempt.
        # Two would read as two attempts to a client that appends on `started`.
        self._kick(
            task.id,
            inputs,
            revision,
            recover=False,
            attempt=_Attempt(record=record, step=step, retry=True),
        )
        return updated

    def _retry_target(
        self, task_id: int, definition: WorkflowDefinition, expected_seq: int
    ) -> tuple[str, str] | None:
        """Where a retry actually lands, honouring the declared visit bound.

        A retry is another attempt at the blocked step. It is *not* a way past
        a bound the definition set: a step already at `max_visits` sends the
        run to its declared exhaustion gate instead, exactly as the engine
        would have. Returning None leaves the pause's own target in place,
        which is the normal case.
        """
        records = list_step_records(self._engine, task_id)
        waiting = next((r for r in records if r.seq == expected_seq), None)
        if waiting is None or waiting.pause is None:
            return None
        blocked = definition.step_named(waiting.pause.get("retry_step", waiting.step))
        if blocked is None or blocked.max_visits is None:
            return None
        attempts = len([r for r in records if r.step == blocked.name])
        if attempts < blocked.max_visits:
            return None
        assert blocked.on_exhausted is not None
        target = definition.step_named(blocked.on_exhausted.step)
        assert target is not None
        logger.info(
            "task %d retry of %r is at its %d-visit bound; routing to %r",
            task_id,
            blocked.name,
            blocked.max_visits,
            target.name,
        )
        return target.name, target.kind

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
        inputs: TaskExecutionInputs,
        revision: WorkflowRevision,
        *,
        recover: bool,
        attempt: _Attempt | None = None,
    ) -> None:
        run = asyncio.create_task(
            self._execute(task_id, inputs, revision, recover=recover, attempt=attempt)
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
        inputs: TaskExecutionInputs,
        revision: WorkflowRevision,
        *,
        recover: bool,
        attempt: _Attempt | None = None,
    ) -> None:
        from ompire_daemon.registry.tasks import get_task

        definition = revision.definition
        current: _Attempt | None
        if attempt is not None:
            current = attempt
        elif recover:
            current = await self._recover_attempt(task_id, definition)
            if current is None:
                return  # run completed, failed, or re-parked during recovery
        else:
            current = self._open(task_id, definition, definition.steps[0])

        while current is not None:
            task = get_task(self._engine, task_id)
            records = list_step_records(self._engine, task_id)
            ctx = evaluation_context(task, inputs, records, current.record.seq)
            step = current.step
            updated = set_run_status(self._engine, task_id, "running", step.name)
            self._publish_task_updated(updated)
            self._publish_step(task_id, step, "started")
            try:
                result = await self._run_step(current, ctx, task, inputs)
            except _StepInfraFailure as exc:
                self._fail_step(task_id, step, current.record.seq, str(exc))
                return
            except Exception:  # noqa: BLE001 — a buggy step must not kill sessions
                error = traceback.format_exc()
                logger.error(
                    "workflow step %r raised for task %d:\n%s", step.name, task_id, error
                )
                self._fail_step(task_id, step, current.record.seq, error)
                return

            if result.pause is not None:
                self._pause(task_id, step, current.record.seq, result.pause)
                return

            if result.gate_message is not None:
                next_step = await self._park_at_gate(
                    task_id, definition, step, current.record.seq, result.gate_message
                )
                current = (
                    self._open(task_id, definition, next_step)
                    if next_step is not None
                    else None
                )
                continue

            finish_step_record(
                self._engine,
                task_id,
                current.record.seq,
                status="ok",
                outcome=result.outcome,
                error=result.error_note,
            )
            self._publish_step(task_id, step, "ok")
            next_step = self._destination_step(definition, step, result.destination)
            current = (
                self._open(task_id, definition, next_step)
                if next_step is not None
                else None
            )

        updated = set_run_status(self._engine, task_id, "complete", None)
        self._publish_task_updated(updated)

    def _destination_step(
        self, definition: WorkflowDefinition, step: Step, destination: Destination | None
    ) -> Step | None:
        """Where the run goes after a step finished `ok`."""
        if destination is None:
            return definition.step_after(step.name)
        if isinstance(destination, StepDestination):
            target = definition.step_named(destination.step)
            assert target is not None  # validated at load
            return target
        return None  # `complete` — a pause never reaches here

    def _open(
        self, task_id: int, definition: WorkflowDefinition, step: Step
    ) -> _Attempt:
        """Append the next attempt, enforcing declared visit bounds first.

        The bound is checked here, in the engine, and not by any route
        predicate: a definition whose routing is wrong must still not be able
        to loop forever, so the count that stops it is the one taken before a
        new attempt is opened. Resuming an already-open attempt never passes
        through here, which is why a restart costs no visit.
        """
        records = list_step_records(self._engine, task_id)
        seen: set[str] = set()
        while step.max_visits is not None:
            attempts = len([r for r in records if r.step == step.name])
            if attempts < step.max_visits:
                break
            assert step.on_exhausted is not None
            if step.name in seen:  # pragma: no cover - validated at load
                break
            seen.add(step.name)
            target = definition.step_named(step.on_exhausted.step)
            assert target is not None
            logger.info(
                "task %d step %r reached its %d-visit bound; routing to %r",
                task_id,
                step.name,
                step.max_visits,
                target.name,
            )
            step = target
        record = append_step_record(
            self._engine,
            task_id,
            step=step.name,
            kind=step.kind,
            session=step.session if isinstance(step, AgentStep) else None,
        )
        return _Attempt(record=record, step=step)

    def _fail_step(self, task_id: int, step: Step, seq: int, error: str) -> None:
        finish_step_record(self._engine, task_id, seq, status="failed", error=error)
        self._publish_step(task_id, step, "failed", error=error)
        updated = set_run_failed(self._engine, task_id, error)
        self._publish_task_updated(updated)

    def _pause(self, task_id: int, step: Step, seq: int, pause: _Pause) -> None:
        """Stop and say what is missing, keeping the attempt's own evidence.

        The record keeps its kind, its absent outcome, and the error it hit.
        Nothing is written that could later read as a result.
        """
        document = build_pause(
            reason=pause.reason,
            message=pause.message,
            step=step.name,
            retry_step=step.name,
        )
        _record, updated = pause_step(
            self._engine, task_id, seq, pause=document, error=pause.error
        )
        self._publish_task_updated(updated)
        self._hub.publish(
            "workflow_step",
            {
                "task_id": task_id,
                "step": step.name,
                "kind": step.kind,
                "session": step.session if isinstance(step, AgentStep) else None,
                "status": "waiting",
                "pause": document,
            },
        )

    async def _park_at_gate(
        self,
        task_id: int,
        definition: WorkflowDefinition,
        step: Step,
        seq: int,
        message: str,
    ) -> Step | None:
        """Persist the declared gate's wait, park until the operator resumes,
        then continue at the gate's fall-through."""
        _record, updated = park_gate(
            self._engine, task_id, seq, step=step.name, message=message
        )
        self._publish_task_updated(updated)
        self._publish_gate_step(task_id, step.name, "waiting", message)
        return await self._await_gate(task_id, definition, seq, step.name, message)

    async def _await_gate(
        self,
        task_id: int,
        definition: WorkflowDefinition,
        seq: int,
        step_name: str,
        message: str,
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
        return definition.step_after(step_name)

    # --- restart recovery (design D-6) ------------------------------------------

    async def _recover_attempt(
        self, task_id: int, definition: WorkflowDefinition
    ) -> _Attempt | None:
        """Where a recovered run continues, and how the interrupted attempt is
        re-driven. Returns the attempt to enter the loop with, or None when
        recovery itself finished or re-parked the run."""
        records = list_step_records(self._engine, task_id)
        last = records[-1] if records else None
        if last is None:
            return self._open(task_id, definition, definition.steps[0])
        if last.status == "waiting":
            if last.pause is not None:
                # An uncertainty pause is re-armed exactly as persisted: no
                # prompt, no automatic retry, no fresh evaluation. The
                # operator's Retry is still the only thing that moves it.
                from ompire_daemon.registry.tasks import get_task

                self._publish_task_updated(get_task(self._engine, task_id))
                self._hub.publish(
                    "workflow_step",
                    {
                        "task_id": task_id,
                        "step": last.step,
                        "kind": last.kind,
                        "session": last.session,
                        "status": "waiting",
                        "pause": last.pause,
                    },
                )
                return None
            # Declared gate: re-arm the SAME record (history stays one row)
            # and re-broadcast the persisted message.
            message = (last.outcome or {}).get("message")
            if not isinstance(message, str) or not message:
                message = "workflow gate"
            updated = set_run_status(self._engine, task_id, "waiting", last.step)
            self._publish_task_updated(updated)
            self._publish_gate_step(task_id, last.step, "waiting", message)
            next_step = await self._await_gate(
                task_id, definition, last.seq, last.step, message
            )
            if next_step is None:
                # The gate is the last declared step: resuming fell off the
                # end — complete the run here, mirroring the main loop's
                # fall-off (bugfix's `escalate` gate relies on this).
                updated = set_run_status(self._engine, task_id, "complete", None)
                self._publish_task_updated(updated)
                return None
            return self._open(task_id, definition, next_step)
        if last.status == "running":
            # The shutdown interrupted this attempt. Re-drive *the same row*
            # rather than closing it and opening another: a restart is not a
            # work attempt, and a declared visit bound counts work.
            step = definition.step_named(last.step)
            if step is None:
                logger.warning(
                    "task %d pinned workflow %r no longer declares step %r; "
                    "failing the run",
                    task_id,
                    definition.name,
                    last.step,
                )
                updated = set_run_failed(
                    self._engine, task_id, f"step {last.step!r} no longer declared"
                )
                self._publish_task_updated(updated)
                return None
            nudge = last.kind == "agent" and last.prompted_at is not None
            retry = _is_retry_attempt(records, last)
            return _Attempt(record=last, step=step, nudge=nudge, retry=retry)
        # Last record finished: a decision routes explicitly (its recorded
        # route); anything else falls through.
        if last.kind == "decision" and last.outcome is not None:
            route = last.outcome.get("route")
            if route == COMPLETE:
                # The run completed itself at the decision; a restart in the
                # narrow window before the status write lands completes here.
                updated = set_run_status(self._engine, task_id, "complete", None)
                self._publish_task_updated(updated)
                return None
            if isinstance(route, str):
                target = definition.step_named(route)
                if target is not None:
                    return self._open(task_id, definition, target)
        following = definition.step_after(last.step)
        return (
            self._open(task_id, definition, following) if following is not None else None
        )

    # --- step execution ----------------------------------------------------------

    async def _run_step(
        self,
        attempt: _Attempt,
        ctx: EvaluationContext,
        task: Task,
        inputs: TaskExecutionInputs,
    ) -> _StepResult:
        step = attempt.step
        if isinstance(step, AgentStep):
            return await self._run_agent_step(attempt, step, ctx, task, inputs)
        if isinstance(step, CommandStep):
            return await self._run_command_step(step, task)
        if isinstance(step, DecisionStep):
            return self._run_decision_step(step, ctx)
        assert isinstance(step, GateStep)
        try:
            message = render_text(step.message, ctx)
        except RenderError as exc:
            return _StepResult(
                pause=_Pause(
                    reason=PAUSE_PROMPT_UNRENDERABLE,
                    message=(
                        f"The gate {step.name!r} could not state why it is "
                        f"waiting: {exc.reason}."
                    ),
                    error=exc.reason,
                )
            )
        return _StepResult(gate_message=message)

    def _run_decision_step(
        self, step: DecisionStep, ctx: EvaluationContext
    ) -> _StepResult:
        """Choose the first case that is *true*.

        An unresolved case stops the run right there. It is deliberately not
        skipped in favour of a later case: "this rule could not be applied" is
        not the same as "this rule does not apply", and treating it as the
        latter is how a run silently takes a route nobody chose.
        """
        for index, case in enumerate(step.cases):
            verdict = evaluate_predicate(case.when, ctx)
            if isinstance(verdict, Unresolved):
                return _StepResult(
                    pause=_Pause(
                        reason=PAUSE_UNRESOLVED_DECISION,
                        message=(
                            f"The decision {step.name!r} cannot choose a route: "
                            f"{verdict.reason}. Retry it once the evidence it "
                            "needs exists; retrying re-reads the recorded "
                            "results and nothing else."
                        ),
                        error=f"case {index} unresolved: {verdict.reason}",
                    )
                )
            if verdict:
                return self._decision_result(step, case.next)
        return self._decision_result(step, step.otherwise)

    def _decision_result(self, step: DecisionStep, destination: Destination) -> _StepResult:
        if isinstance(destination, PauseDestination):
            return _StepResult(
                pause=_Pause(
                    reason=PAUSE_UNRESOLVED_DECISION,
                    message=(
                        f"The decision {step.name!r} found no declared route for "
                        "this result, and its definition says to wait for a "
                        "person rather than pick one."
                    ),
                    error="no declared case matched; the definition routes to a pause",
                )
            )
        route = (
            destination.step if isinstance(destination, StepDestination) else COMPLETE
        )
        return _StepResult(outcome={"route": route}, destination=destination)

    async def _ensure_session(
        self,
        task: Task,
        session: str,
        *,
        binding: ConsumerBinding,
        consumer_kind: str,
        consumer_name: str,
    ):
        """Put this session on the consumer's accepted policy and hand back a
        handle that may be prompted (ADR-0027).

        This covers all four cases in one place: a first lazy spawn, a step
        that wants exactly what the session already runs, a step that changes
        only the active pair, and a step that changes an auxiliary role and
        therefore needs the process replaced around the same native session.
        A cached live handle is never by itself the answer — the supervisor
        re-asserts and reads back the active pair every time, because an
        operator `/model` inside the container would otherwise masquerade as
        the accepted policy.

        The applied policy is committed durably before the caller can prompt,
        so a restart continues this session on what it actually ran under
        rather than on whichever step happened to open it.
        """
        policy = ModelPolicy.from_binding(binding)
        # The row must exist before the applied-policy write, which updates it.
        record_session_spawned(self._engine, task.id, session)

        def commit() -> None:
            record_applied_policy(
                self._engine,
                task.id,
                session,
                build_applied_policy(
                    policy,
                    profile_name=binding.profile_name,
                    role=binding.role,
                    consumer_kind=consumer_kind,
                    consumer_name=consumer_name,
                ),
            )

        # Only for the failure message: a session that had no live child was
        # being spawned, one that had a live child was being handed over, and
        # the operator reading the reason should be told which.
        was_live = self._supervisor.get(task.id, session) is not None
        try:
            handle = await self._supervisor.apply_session_policy(
                task.id,
                session,
                task.clone_path,
                policy=policy,
                commit=commit,
            )
        except Exception as exc:
            detail = str(exc)
            stderr = getattr(exc, "stderr", "")
            if stderr:
                detail = f"{detail}\n{stderr}"
            what = "model policy handoff" if was_live else "session spawn"
            self._tracker.session_start_failed(
                task.id, session, f"{what} failed: {exc}"
            )
            raise _StepInfraFailure(detail) from exc
        # Best-effort identity capture (crash-recovery): a miss is logged
        # inside `read_session_id` and never fails the step. Re-read after a
        # replacement too — the recorded id is what the next resume uses.
        session_id = await handle.read_session_id()
        if session_id is not None:
            mark_session_id(self._engine, task.id, session, session_id)
        return handle

    async def _run_agent_step(
        self,
        attempt: _Attempt,
        step: AgentStep,
        ctx: EvaluationContext,
        task: Task,
        inputs: TaskExecutionInputs,
    ) -> _StepResult:
        gate = evaluate_predicate(step.when, ctx)
        if isinstance(gate, Unresolved):
            return _StepResult(
                pause=_Pause(
                    reason=PAUSE_CONDITION_UNRESOLVED,
                    message=(
                        f"The step {step.name!r} cannot tell whether it should "
                        f"run: {gate.reason}."
                    ),
                    error=f"`when` unresolved: {gate.reason}",
                )
            )
        try:
            binding = inputs.binding_for_step(step.name)
        except MissingConsumerBindingError as exc:
            # The definition declares a step this task never accepted a policy
            # for. Failing the step is the only honest option: there is no
            # reviewed policy to run it under.
            raise _StepInfraFailure(str(exc)) from exc
        # Even a step that will not be prompted gets its session put on the
        # accepted policy: the declaration says this session participates, and
        # the next step in it must not inherit whatever came before.
        handle = await self._ensure_session(
            task,
            step.session,
            binding=binding,
            consumer_kind="step",
            consumer_name=step.name,
        )

        if attempt.nudge:
            prompt = (
                f"{RESUME_NUDGE} Finish by writing `{OUTCOME_PATH}`."
                if step.expects_outcome
                else RESUME_NUDGE
            )
        elif not gate:
            prompt = ""
        else:
            try:
                prompt = render_text(step.prompt, ctx)
            except RenderError as exc:
                return _StepResult(
                    pause=_Pause(
                        reason=PAUSE_PROMPT_UNRENDERABLE,
                        message=(
                            f"The prompt for {step.name!r} could not be built: "
                            f"{exc.reason}."
                        ),
                        error=exc.reason,
                    )
                )

        if not prompt:
            # Nothing sent; the step completes once the session is ready
            # (parity with the old promptless-spawn idle behavior). No outcome
            # instruction was given, so any file on disk is stale by
            # definition — record a null outcome without reading it, and never
            # pause a step that was deliberately inert.
            self._tracker.prompt_skipped(task.id, step.session)
            return _StepResult(
                error_note=(
                    None
                    if attempt.nudge
                    else "step deliberately not prompted (its `when` is false "
                    "or its prompt renders empty)"
                )
            )

        if not attempt.nudge:
            if attempt.retry:
                prompt = f"{RETRY_PREFIX}\n\n{prompt}"
            if step.expects_outcome:
                # A stale file from an earlier step or a failed attempt is
                # never this attempt's result (design D-3). Skipped for a
                # restart nudge: the agent may have written it before the
                # restart.
                try:
                    (Path(task.clone_path) / OUTCOME_PATH).unlink()
                except FileNotFoundError:
                    pass
                prompt = f"{prompt}\n\n{OUTCOME_INSTRUCTION}"

        # Omp resolves `@path` mentions against the child's working directory
        # and drops silently what it cannot find (findings-omp-file-mentions.md),
        # so a mention the clone no longer carries would cost the operator
        # context with nothing anywhere saying so. Only the operator's own
        # mentions are gated; the standing preamble's stray `@word` is prose.
        operator_mentions = set(mention_tokens(task.prompt))
        if operator_mentions:
            dangling = [
                token
                for token in unresolved_mentions(prompt, task.clone_path)
                if token in operator_mentions
            ]
            if dangling:
                listed = ", ".join(f"@{token}" for token in dangling)
                raise _StepInfraFailure(
                    f"prompt file mention does not resolve in the task clone: {listed}"
                )

        try:
            # The ack is a receipt ("queued"), not turn completion.
            await asyncio.wait_for(
                handle.prompt(prompt), timeout=self._config.spawn_step_timeout
            )
        except (TimeoutError, RequestFailedError, AgentGoneError) as exc:
            raise _StepInfraFailure(f"prompt delivery failed: {exc}") from exc
        mark_prompt_sent(self._engine, task.id, attempt.record.seq)
        # Completion is the session's debounced idle (design D-2). A session
        # dying mid-step is an infra failure; a pending question just keeps
        # the run running until the operator answers and the turn ends.
        await self._await_step_idle(task.id, step.session)
        if not step.expects_outcome:
            return _StepResult()
        outcome, note = read_outcome(task.clone_path)
        if outcome is not None:
            return _StepResult(outcome=outcome)
        # The agent had its chance and produced nothing readable. The attempt
        # keeps its absent result and the reason; a person decides what next.
        return _StepResult(
            pause=_Pause(
                reason=PAUSE_MISSING_OUTCOME,
                message=(
                    f"The step {step.name!r} finished without a valid result "
                    f"({note}). Retry the step to give it another attempt; the "
                    "run will not continue on a result that was never written."
                ),
                error=note or "no outcome file written",
            )
        )

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

    async def _run_command_step(self, step: CommandStep, task: Task) -> _StepResult:
        argv = ["workshop", "exec", "-p", task.clone_path, "--", *step.argv]
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            raise _StepInfraFailure(f"cannot exec 'workshop': {exc}") from exc
        try:
            output_bytes, _ = await asyncio.wait_for(
                process.communicate(), timeout=step.timeout
            )
        except (asyncio.CancelledError, TimeoutError) as exc:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.communicate()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise _StepInfraFailure(f"command timed out after {step.timeout}s") from None
        tail = output_bytes[-_COMMAND_OUTPUT_TAIL:].decode("utf-8", errors="replace")
        # A non-zero exit is outcome DATA (routing on it is a following
        # decision step's job); only the inability to execute fails the run.
        return _StepResult(outcome={"exit_code": process.returncode, "output": tail})

    # --- events ------------------------------------------------------------------

    def _publish_task_updated(self, task: Task) -> None:
        self._hub.publish("task_updated", task_payload(task, engine=self._engine))

    def _publish_step(self, task_id: int, step: Step, status: str, **extra: Any) -> None:
        self._hub.publish(
            "workflow_step",
            {
                "task_id": task_id,
                "step": step.name,
                "kind": step.kind,
                "session": step.session if isinstance(step, AgentStep) else None,
                "status": status,
                **extra,
            },
        )

    def _publish_gate_step(
        self, task_id: int, step_name: str, status: str, message: str | None = None
    ) -> None:
        """Gate transitions by name (the step object isn't always at hand —
        recovery re-arms from the persisted record)."""
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


def _is_retry_attempt(records: list[StepRecord], attempt: StepRecord) -> bool:
    """Whether this attempt was opened by an operator retry.

    Read back from the immediately preceding record rather than held in
    memory, so a restart between the retry transaction and the first prompt
    still tells the agent it is re-attempting rather than starting fresh.
    """
    previous = next(
        (record for record in reversed(records) if record.seq < attempt.seq), None
    )
    if previous is None or previous.step != attempt.step:
        return False
    return previous.status == "failed" and RETRY_NOTE in (previous.error or "")


__all__ = [
    "BUILTIN_NAMES",
    "COMPLETE",
    "OUTCOME_INSTRUCTION",
    "OUTCOME_PATH",
    "RESUME_NUDGE",
    "RETRY_PREFIX",
    "PackagedWorkflowError",
    "UnknownWorkflowNameError",
    "WorkflowNotWaitingError",
    "WorkflowRunner",
    "catalog",
    "catalog_names",
    "current_revision",
    "describe_catalog",
    "install_definition",
    "load_packaged_workflows",
    "read_outcome",
    "register_catalog",
    "reset_catalog",
    "uninstall_definition",
]
