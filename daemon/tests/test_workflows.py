"""Workflow-engine tests: the runner driving `AgentSupervisor` with the fake
omp (same monkeypatch pattern as test_sessions.py), fake workshop CLI for
command steps, and test-installed YAML definitions for multi-step coverage.

Covers single-step parity (prompt bytes, empty-prompt idle), the outcome
convention, command exit codes and infra failure, decision routing and
declared gates, restart recovery, and the whole bugfix built-in.

Two things are new here and load-bearing (ADR-0028). Every run executes the
definition the *task* pinned, so a test can edit the catalog and prove the
running task is unaffected. And the engine never guesses: a missing required
outcome or an undecidable route pauses with its reason, and only an explicit
operator retry opens another attempt.
"""

from __future__ import annotations

import asyncio
import json
import time
from importlib import resources
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from ompire_daemon import agent as agent_module
from ompire_daemon.agent import AgentSupervisor
from ompire_daemon.config import Config
from ompire_daemon.db import db_path_for, ensure_db_dir, make_engine
from ompire_daemon.delivery import WorkspaceGuard
from ompire_daemon.events import EventHub
from ompire_daemon.migrate import upgrade_head
from ompire_daemon.oversight.tasks import task_payload
from ompire_daemon.registry.results import get_result, list_results
from ompire_daemon.registry.sessions import get_session
from ompire_daemon.registry.workflow_library import (
    UnknownWorkflowNameError,
    WorkflowNotLaunchableError,
    list_entries,
    resolve_current,
    set_archived,
)
from ompire_daemon.registry.workflows import (
    WorkflowGateChoiceError,
    WorkflowWaitConflictError,
    list_step_records,
    resolve_gate,
)
from ompire_daemon.results import ResultManager
from ompire_daemon.sessions import SessionTracker
from ompire_daemon.taskdefinition import (
    TaskDefinitionUnavailableError,
    resolve_task_definition,
)
from ompire_daemon.work.projects import create_project
from ompire_daemon.work.tasks import Task, create_task, get_task
from ompire_daemon.workflows import (
    COMPLETE,
    WorkflowNotWaitingError,
    WorkflowRunner,
)
from tests.conftest import (
    TEST_ROLES,
    fake_argv_builder,
    install_plain_workflow,
    install_test_workflow,
    make_execution_inputs,
    make_test_policy,
    register_builtin_workflows,
)

DEBOUNCE = 0.1


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_path_for(data_dir)
    ensure_db_dir(db_path)
    upgrade_head(db_path)
    engine = make_engine(db_path)
    register_builtin_workflows(engine)
    return engine


@pytest.fixture
def project(engine: Engine, tmp_path: Path):
    checkout = tmp_path / "checkout"
    checkout.mkdir(parents=True, exist_ok=True)
    return create_project(
        engine,
        name="demo",
        title="Demo",
        upstream_url="https://example.com/demo.git",
        checkout_path=str(checkout),
        default_checkout_root=tmp_path,
    )


def _make_task(
    engine: Engine,
    tmp_path: Path,
    workflow: str = "plain",
    prompt: str = "do it",
    *,
    preamble: str = "",
    roles: dict | None = None,
    slug: str | None = None,
    revision=None,
    **bindings,
) -> Task:
    """A task carrying the launch inputs it was accepted under, definition
    included (ADR-0026, ADR-0028). The engine reads the preamble, the role
    bindings, and the pinned revision off the task; there is no second object
    to hand it."""
    if workflow == "plain":
        install_plain_workflow(engine)
    name = slug or f"task-{workflow}"
    clone_path = tmp_path / "tasks" / name
    clone_path.mkdir(parents=True, exist_ok=True)
    return create_task(
        engine,
        project_name="demo",
        slug=name,
        branch="ompire/task",
        clone_path=str(clone_path),
        prompt=prompt,
        workflow_name=workflow,
        execution_inputs=make_execution_inputs(
            engine=engine,
            checkout_path=str(tmp_path / "checkout"),
            workflow_name=workflow,
            preamble=preamble,
            roles=roles,
            branch="ompire/task",
            revision=revision,
            **bindings,
        ),
    )


def _start(runner: WorkflowRunner, engine: Engine, task: Task) -> None:
    """Start the run the way the spawn pipeline does: through the task's own
    pinned definition."""
    runner.start_run(task, resolve_task_definition(engine, task))


def _recover(runner: WorkflowRunner, engine: Engine, task: Task) -> None:
    runner.recover_run(task, resolve_task_definition(engine, task))


class _StubReviews:
    """A `ReviewManager` stand-in with the same durable contract.

    It writes the iteration through the real registry and then wakes the
    parked step, because that ordering is the property under test: the runner
    must route on what was *recorded*, not on anything handed to it.
    """

    def __init__(
        self, engine: Engine, verdicts: list[dict], *, default: dict | None = None
    ) -> None:
        self._engine = engine
        self._verdicts = verdicts
        self._default = default
        self._completions: dict[int, asyncio.Future] = {}
        self.started: list[int] = []

    def watch_completion(self, task_id: int) -> asyncio.Future:
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._completions[task_id] = future
        return future

    async def start_review(self, task, *, workflow_seq: int | None = None) -> None:
        from ompire_daemon.registry.reviews import append_iteration, open_review

        self.started.append(workflow_seq)
        if not self._verdicts and self._default is not None:
            verdict = dict(self._default)
        else:
            verdict = self._verdicts.pop(0)
        if isinstance(verdict, Exception):
            raise verdict
        open_review(
            self._engine,
            task.id,
            candidate_id="cand-1",
            workflow_seq=workflow_seq,
        )
        append_iteration(
            self._engine,
            task.id,
            outcome=verdict["outcome"],
            candidate_id="cand-1",
            workflow_seq=workflow_seq,
            findings=verdict.get("findings"),
            status=verdict["outcome"] if verdict["outcome"] == "approved" else None,
        )
        future = self._completions.pop(task.id, None)
        if future is not None and not future.done():
            future.set_result(None)


@pytest.fixture
async def rig(engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Runner + supervisor + tracker wired to fake omp with a fast debounce.
    `scenario` lets a test switch the fake omp behavior before the run."""
    scenario = {"name": "happy"}
    monkeypatch.setattr(agent_module, "build_agent_argv", fake_argv_builder(scenario))

    async def no_preflight(clone_path: str) -> None:
        return None

    monkeypatch.setattr(agent_module, "verify_ask_timeout", no_preflight)
    hub = EventHub()
    tracker = SessionTracker(hub, idle_debounce=DEBOUNCE, stall_threshold=300)
    config = Config(
        data_dir=tmp_path / "data",
        task_dir_root=tmp_path / "tasks",
        checkout_root=tmp_path / "proj",
        session_idle_debounce=DEBOUNCE,
        spawn_step_timeout=10,
    )
    supervisor = AgentSupervisor(config, hub, tracker)
    runner = WorkflowRunner(engine, config, hub, supervisor, tracker)
    # A format-3 run reaches a `review` step, which the trusted service owns.
    # The stub is unbounded and approves by default, so a test about routing
    # says only what it means to say; one about verdicts sets them explicitly.
    runner.set_operations(_StubReviews(engine, [], default={"outcome": "approved"}), None)
    try:
        yield runner, supervisor, tracker, hub, scenario
    finally:
        await runner.shutdown()
        await supervisor.shutdown()


def _restart_rig(engine: Engine, tmp_path: Path):
    """Fresh in-memory machinery over the same database: what the next daemon
    process would have."""
    hub = EventHub()
    tracker = SessionTracker(hub, idle_debounce=DEBOUNCE, stall_threshold=300)
    config = Config(
        data_dir=tmp_path / "data",
        task_dir_root=tmp_path / "tasks",
        checkout_root=tmp_path / "proj",
        session_idle_debounce=DEBOUNCE,
        spawn_step_timeout=10,
    )
    supervisor = AgentSupervisor(config, hub, tracker)
    runner = WorkflowRunner(engine, config, hub, supervisor, tracker)
    runner.set_operations(_StubReviews(engine, [], default={"outcome": "approved"}), None)
    return runner, supervisor, tracker, hub


async def _resume_recorded_sessions(engine, supervisor, tracker, task) -> None:
    from ompire_daemon.registry.sessions import list_resumable_sessions

    for row in list_resumable_sessions(engine, task.id):
        tracker.recovering(task.id, row.name)
        await supervisor.start(
            task.id,
            row.name,
            task.clone_path,
            resume=row.omp_session_id,
            policy=make_test_policy(),
        )
        tracker.session_recovered(task.id, row.name)


async def wait_for_run(engine: Engine, task_id: int, statuses: set[str], timeout: float = 10.0):
    """Poll the registry until the run lands in one of `statuses`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = get_task(engine, task_id)
        if task.workflow_status in statuses:
            return task
        await asyncio.sleep(0.02)
    raise RuntimeError(f"run did not reach {statuses} (at {get_task(engine, task_id).workflow_status})")


def user_prompts(supervisor: AgentSupervisor, task_id: int, session: str) -> list[str]:
    """The prompt texts fake omp echoed back as user messages on a session."""
    handle = supervisor.get(task_id, session)
    assert handle is not None
    texts = []
    for event in handle.snapshot():
        if event.type != "message_start":
            continue
        message = event.payload.get("message") or {}
        if message.get("role") != "user":
            continue
        for part in message.get("content", []):
            if part.get("type") == "text":
                texts.append(part["text"])
    return texts


def waiting_seq(engine: Engine, task_id: int) -> int:
    """The sequence number of the attempt the run is waiting on."""
    record = list_step_records(engine, task_id)[-1]
    assert record.status == "waiting", record
    return record.seq


def resume(runner: WorkflowRunner, engine: Engine, task_id: int, note: str | None = None) -> None:
    runner.resume_gate(task_id, expected_seq=waiting_seq(engine, task_id), note=note)


def retry(runner: WorkflowRunner, engine: Engine, task: Task):
    return runner.retry_step(
        get_task(engine, task.id),
        resolve_task_definition(engine, task),
        expected_seq=waiting_seq(engine, task.id),
    )


# --- test definitions ---------------------------------------------------------
# Written the way an author writes them: YAML documents installed into the
# process catalog and retained, exactly as a packaged definition is.

# The engine's own baseline: one agent step, one turn, whatever the operator
# typed. It is deliberately *not* the packaged `single-step`, which declares
# review, an approval, and delivery. Tests about prompt bytes, restart
# recovery, and mention handling are about the engine, and pinning them to a
# workflow that also publishes would make them fail for reasons that have
# nothing to do with what they check.

OUTCOME_YAML = """
format: 1
name: outcome-wf
sessions: [main]
primary: main
steps:
  - name: work
    kind: agent
    session: main
    expects_outcome: true
    prompt:
      parts:
        - value: {op: input, name: task.prompt}
          format: text
"""

BRANCH_YAML = """
format: 1
name: branch-wf
sessions: [main]
primary: main
steps:
  - name: probe
    kind: command
    argv: ["probe-cmd"]
    timeout: 5
    idempotent: true
  - name: route
    kind: decision
    cases:
      - when:
          op: eq
          left:
            op: get
            value: {op: latest, steps: [probe]}
            keys: [outcome, exit_code]
          right: {op: literal, value: 0}
        next: {step: fix}
    otherwise: {step: bail}
  # Fall-through is linear: the gate sits between the decision and `fix`, so
  # routing to `fix` skips it, and resuming the gate continues at `fix`.
  - name: bail
    kind: gate
    message:
      parts:
        - text: "probe failed; operator call"
  - name: fix
    kind: agent
    session: main
    prompt:
      parts:
        - text: "fix it"
"""

TWO_SESSION_YAML = """
format: 1
name: two-session-wf
sessions: [coder, reviewer]
primary: reviewer
steps:
  - name: implement
    kind: agent
    session: coder
    prompt:
      parts:
        - text: "implement it"
  - name: inspect
    kind: agent
    session: reviewer
    prompt:
      separator: ""
      parts:
        - text: "records so far: "
        - value: {op: count, step: implement}
          format: text
"""

GATE_YAML = """
format: 1
name: gate-wf
sessions: [main]
primary: main
steps:
  - name: approve
    kind: gate
    message:
      parts:
        - text: "ship it?"
  - name: after
    kind: command
    argv: ["true"]
    timeout: 5
    idempotent: true
"""

SHARED_SESSION_YAML = """
format: 1
name: shared-policy
sessions: [shared]
primary: shared
steps:
  - name: first
    kind: agent
    session: shared
    prompt:
      parts:
        - text: "first turn"
  - name: second
    kind: agent
    session: shared
    prompt:
      parts:
        - text: "second turn"
"""

LOOP_YAML = """
format: 1
name: loop-policy
sessions: [coder, checker]
primary: coder
steps:
  - name: fix
    kind: agent
    session: coder
    max_visits: 2
    on_exhausted: {step: stop}
    prompt:
      separator: ""
      parts:
        - text: "fix "
        - value: {op: count, step: fix}
          format: text
  - name: check
    kind: agent
    session: checker
    prompt:
      parts:
        - text: "check"
  - name: again
    kind: decision
    cases:
      - when:
          op: lt
          left: {op: count, step: fix}
          right: {op: literal, value: 2}
        next: {step: fix}
    otherwise: {complete: true}
  - name: stop
    kind: gate
    message:
      parts:
        - text: "out of attempts"
"""


@pytest.fixture
def outcome_workflow(engine: Engine):
    return install_test_workflow(engine, OUTCOME_YAML)


@pytest.fixture
def branch_workflow(engine: Engine, fake_workshop_cli: Path):
    """Install the branching definition; the autouse fake workshop exits 0 for
    any command, so `probe-cmd` records exit_code 0 by default."""
    return install_test_workflow(engine, BRANCH_YAML)


# --- the library and the pinned revision (ADR-0028, ADR-0031) -----------------


def test_the_packaged_definitions_are_the_builtin_entries(engine: Engine) -> None:
    assert [entry.name for entry in list_entries(engine)] == ["bugfix", "planning", "single-step"]
    assert {entry.origin for entry in list_entries(engine)} == {"builtin"}
    with engine.connect() as conn:
        revision = resolve_current(conn, "single-step")
        assert revision.revision.startswith("sha256:")
        assert revision.definition.primary == "main"
        with pytest.raises(UnknownWorkflowNameError):
            resolve_current(conn, "no-such-workflow")


def test_a_broken_packaged_definition_stops_the_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shipping an unexecutable built-in is a build error.

    Starting anyway would leave launches silently unavailable, and the failure
    would surface on a task rather than on the release.
    """
    from ompire_daemon import workflows as workflows_module

    class _BadResource:
        def read_text(self, encoding: str = "utf-8") -> str:
            return "format: 1\nname: single-step\nsessions: []\n"

    class _BadPackage:
        def __truediv__(self, _name: str) -> _BadResource:
            return _BadResource()

    monkeypatch.setattr(
        workflows_module.resources, "files", lambda _package: _BadPackage()
    )
    with pytest.raises(workflows_module.PackagedWorkflowError, match="is invalid"):
        workflows_module.load_packaged_workflows()


def test_a_missing_packaged_resource_stops_the_daemon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ompire_daemon import workflows as workflows_module

    class _AbsentResource:
        def read_text(self, encoding: str = "utf-8") -> str:
            raise FileNotFoundError("not packaged")

    class _AbsentPackage:
        def __truediv__(self, _name: str) -> _AbsentResource:
            return _AbsentResource()

    monkeypatch.setattr(
        workflows_module.resources, "files", lambda _package: _AbsentPackage()
    )
    with pytest.raises(workflows_module.PackagedWorkflowError, match="missing"):
        workflows_module.load_packaged_workflows()


def test_launch_rejects_an_uninstalled_workflow(engine: Engine, project) -> None:
    """The library is the only source of valid workflow names, and a launch
    names one directly (ADR-0026, ADR-0031)."""
    from ompire_daemon.work.launch import (
        LaunchInputError,
        LaunchRequest,
        resolve_launch,
    )

    request = LaunchRequest(
        project_name="demo",
        workflow_name="no-such-workflow",
        slug="x",
        prompt="p",
        model_profile=None,
        profile_explicit=False,
        workspace_overrides={},
    )
    with engine.connect() as conn, pytest.raises(LaunchInputError) as exc_info:
        resolve_launch(conn, request)
    assert exc_info.value.field == "workflow_name"


async def test_a_running_task_keeps_its_revision_when_the_definition_changes(
    rig, engine, project, tmp_path: Path
) -> None:
    """The point of pinning, end to end.

    A task is accepted under one definition; the library entry is then edited
    and a new executable revision saved. The run still sends the prompt it was
    accepted with, still resolves to its own revision, and the *new* revision
    is what a new launch would get. Both revisions stay retained and readable
    at once.
    """
    original = install_test_workflow(
        engine,
        """
format: 1
name: editable
sessions: [main]
primary: main
steps:
  - name: work
    kind: agent
    session: main
    prompt:
      parts:
        - text: "the original instruction"
""",
    )
    task = _make_task(engine, tmp_path, workflow="editable")

    edited = install_test_workflow(
        engine,
        """
format: 1
name: editable
sessions: [main]
primary: main
steps:
  - name: work
    kind: agent
    session: main
    prompt:
      parts:
        - text: "a completely different instruction"
""",
    )
    assert edited.revision != original.revision
    with engine.connect() as conn:
        assert resolve_current(conn, "editable").revision == edited.revision

    runner, supervisor, _tracker, _hub, _scenario = rig
    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"complete"})

    assert user_prompts(supervisor, task.id, "main") == ["the original instruction"]
    resolved = resolve_task_definition(engine, get_task(engine, task.id))
    assert resolved.revision == original.revision

    # Both are retained: the old one is still readable after the edit, which
    # is what makes an old task's history explainable.
    from ompire_daemon.registry.workflow_definitions import clear_cache, get_revision

    clear_cache()
    assert get_revision(engine, original.revision).revision == original.revision
    assert get_revision(engine, edited.revision).revision == edited.revision

    # And the task survives its workflow leaving the launch choices entirely —
    # the entry is archived. Nothing resolves by name.
    version = next(e.version for e in list_entries(engine) if e.name == "editable")
    set_archived(engine, "editable", archived=True, expected_version=version)
    with engine.connect() as conn, pytest.raises(WorkflowNotLaunchableError):
        resolve_current(conn, "editable")
    clear_cache()
    still = resolve_task_definition(engine, get_task(engine, task.id))
    assert still.revision == original.revision
    assert still.definition.primary == "main"


def test_a_task_whose_revision_is_unavailable_is_refused_not_substituted(
    engine: Engine, project, tmp_path: Path
) -> None:
    """A damaged or absent revision blocks *this* task and says why. It never
    falls back to the library's current definition of the same name."""
    from sqlalchemy import text as sa_text

    from ompire_daemon.registry.workflow_definitions import clear_cache

    task = _make_task(engine, tmp_path, workflow="single-step")
    revision = task.execution_inputs.workflow_binding.revision
    with engine.begin() as conn:
        conn.execute(
            sa_text("DELETE FROM workflow_revisions WHERE revision = :rev"),
            {"rev": revision},
        )
    clear_cache()

    with pytest.raises(TaskDefinitionUnavailableError) as exc_info:
        resolve_task_definition(engine, get_task(engine, task.id))
    assert exc_info.value.reason == "missing"

    # The task is still listed and still readable: one damaged row must not
    # take the dashboard with it.
    from ompire_daemon.oversight.tasks import task_payload
    from ompire_daemon.work.tasks import list_tasks

    payload = task_payload(get_task(engine, task.id), engine=engine)
    assert payload["workflow_ready"] is False
    assert payload["workflow_readiness_reason"] == "missing"
    assert payload["workflow_primary_session"] is None
    assert [t.id for t in list_tasks(engine)] == [task.id]


def test_a_corrupt_stored_document_fails_its_integrity_check(
    engine: Engine, project, tmp_path: Path
) -> None:
    """Content identity is verified before a retained document is executed, so
    an edited row cannot masquerade as the revision a task accepted."""
    from sqlalchemy import text as sa_text

    from ompire_daemon.registry.workflow_definitions import clear_cache

    task = _make_task(engine, tmp_path, workflow="single-step")
    revision = task.execution_inputs.workflow_binding.revision
    with engine.begin() as conn:
        row = conn.execute(
            sa_text("SELECT document_json FROM workflow_revisions WHERE revision = :r"),
            {"r": revision},
        ).scalar_one()
        tampered = json.loads(row)
        tampered["steps"][0]["prompt"]["parts"] = [{"text": "do something else"}]
        conn.execute(
            sa_text("UPDATE workflow_revisions SET document_json = :d WHERE revision = :r"),
            {"d": json.dumps(tampered), "r": revision},
        )
    clear_cache()

    with pytest.raises(TaskDefinitionUnavailableError) as exc_info:
        resolve_task_definition(engine, get_task(engine, task.id))
    assert exc_info.value.reason == "integrity"


# --- single-step parity (D-10) ------------------------------------------------


async def test_single_step_delivers_preamble_plus_prompt(rig, engine, project, tmp_path: Path) -> None:
    runner, supervisor, tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, preamble="PRE", prompt="do it")

    _start(runner, engine, task)
    final = await wait_for_run(engine, task.id, {"complete"})

    assert final.workflow_status == "complete"
    assert final.workflow_step is None
    assert user_prompts(supervisor, task.id, "main") == ["PRE\n\ndo it"]
    # No outcome instruction tail on single-step prompts (D-3 parity).
    assert "outcome.json" not in user_prompts(supervisor, task.id, "main")[0]
    info = tracker.get(task.id, "main")
    assert info is not None and info.status == "idle"
    records = list_step_records(engine, task.id)
    assert [(r.step, r.kind, r.session, r.status) for r in records] == [
        ("work", "agent", "main", "ok")
    ]
    assert records[0].outcome is None
    assert records[0].prompted_at is not None
    # The session row carries the captured fake omp identity for --resume.
    session_row = get_session(engine, task.id, "main")
    assert session_row is not None and session_row.omp_session_id == "fake-session-id"


async def test_single_step_empty_prompt_sends_nothing(rig, engine, project, tmp_path: Path) -> None:
    """A rendered-empty prompt spends no turn, and — crucially — does not
    pause. Nothing was asked, so nothing is missing."""
    runner, supervisor, tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, preamble="PRE", prompt="")

    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"complete"})

    assert user_prompts(supervisor, task.id, "main") == []
    info = tracker.get(task.id, "main")
    assert info is not None and info.status == "idle"
    assert "no prompt" in info.reason
    record = list_step_records(engine, task.id)[0]
    assert record.prompted_at is None
    assert record.status == "ok"
    assert record.pause is None
    assert "deliberately not prompted" in (record.error or "")


async def test_run_fails_when_session_spawn_fails(rig, engine, project, tmp_path: Path) -> None:
    runner, _supervisor, tracker, _hub, scenario = rig
    scenario["name"] = "crash"
    task = _make_task(engine, tmp_path)

    _start(runner, engine, task)
    final = await wait_for_run(engine, task.id, {"failed"})

    assert final.workflow_status == "failed"
    assert final.state == "created"  # workflow failure is not workspace failure
    assert final.error
    info = tracker.get(task.id, "main")
    assert info is not None and info.status == "failed"
    records = list_step_records(engine, task.id)
    assert records[0].status == "failed"
    assert records[0].error


# --- outcome convention (D-3) -------------------------------------------------


async def _write_outcome_when_prompted(
    supervisor: AgentSupervisor, task_id: int, clone_path: Path, content: str
) -> None:
    """Simulate the container-side agent writing the outcome file mid-turn:
    as soon as the prompt echo appears, write the file (well before the
    debounced idle the engine waits for)."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        handle = supervisor.get(task_id, "main")
        if handle is not None and user_prompts(supervisor, task_id, "main"):
            (clone_path / ".ompire").mkdir(exist_ok=True)
            (clone_path / ".ompire" / "outcome.json").write_text(content)
            return
        await asyncio.sleep(0.01)
    raise RuntimeError("prompt never observed")


async def _write_outcome_when_session_prompted(
    supervisor: AgentSupervisor,
    task_id: int,
    session: str,
    clone_path: Path,
    content: str,
    *,
    prompt_count: int = 1,
) -> None:
    """Like `_write_outcome_when_prompted`, but for an arbitrary session and
    waiting for its Nth prompt (loop revisits prompt a session repeatedly)."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if supervisor.get(task_id, session) is not None and len(
            user_prompts(supervisor, task_id, session)
        ) >= prompt_count:
            (clone_path / ".ompire").mkdir(exist_ok=True)
            (clone_path / ".ompire" / "outcome.json").write_text(content)
            return
        await asyncio.sleep(0.01)
    raise RuntimeError(f"prompt {prompt_count} on session {session} never observed")


async def test_outcome_written(rig, engine, project, tmp_path: Path, outcome_workflow) -> None:
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, workflow="outcome-wf")
    # A stale file from "an earlier step" must be unlinked before prompting.
    (Path(task.clone_path) / ".ompire").mkdir(parents=True)
    (Path(task.clone_path) / ".ompire" / "outcome.json").write_text(
        '{"version": 1, "status": "failed", "summary": "stale"}'
    )

    writer = asyncio.create_task(
        _write_outcome_when_prompted(
            supervisor,
            task.id,
            Path(task.clone_path),
            json.dumps(
                {
                    "version": 1,
                    "status": "success",
                    "summary": "fixed it",
                    "artifacts": {"repro_command": "python repro.py"},
                }
            ),
        )
    )
    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"complete"})
    await writer

    record = list_step_records(engine, task.id)[0]
    assert record.status == "ok"
    assert record.outcome is not None
    assert record.outcome["status"] == "success"
    assert record.outcome["summary"] == "fixed it"
    assert record.outcome["artifacts"] == {"repro_command": "python repro.py"}
    assert record.error is None
    # The prompt carried the outcome instruction (outcome-bearing step).
    assert "outcome.json" in user_prompts(supervisor, task.id, "main")[0]


async def test_a_missing_required_outcome_pauses_instead_of_being_judged(
    rig, engine, project, tmp_path: Path, outcome_workflow
) -> None:
    """The engine had a prompt sent and got nothing readable back. It stops
    and says so; it does not ask a model what the result probably was, and it
    does not continue as if the missing result had been accepted."""
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, workflow="outcome-wf")

    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})

    record = list_step_records(engine, task.id)[0]
    # The attempt keeps its own kind, its absent result, and its reason.
    assert (record.kind, record.status, record.outcome) == ("agent", "waiting", None)
    assert record.error and "no outcome file" in record.error
    assert record.pause is not None
    assert record.pause["reason"] == "missing_outcome"
    assert record.pause["retry_step"] == "work"
    assert "no outcome file" in record.pause["message"]
    # Nothing was spawned to judge it.
    assert supervisor.get(task.id, "judge") is None


async def test_a_malformed_outcome_pauses_with_the_parse_error(
    rig, engine, project, tmp_path: Path, outcome_workflow
) -> None:
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, workflow="outcome-wf")

    writer = asyncio.create_task(
        _write_outcome_when_prompted(
            supervisor, task.id, Path(task.clone_path), "not json {"
        )
    )
    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})
    await writer

    record = list_step_records(engine, task.id)[0]
    assert record.outcome is None
    assert record.error and "JSON" in record.error
    assert record.pause["reason"] == "missing_outcome"


async def test_retrying_opens_a_new_attempt_and_keeps_the_original_record(
    rig, engine, project, tmp_path: Path, outcome_workflow
) -> None:
    """Retry is another attempt at the blocked step, never permission to skip
    it. The paused attempt keeps its evidence, and the new turn is told the
    working tree may already have changed."""
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, workflow="outcome-wf")

    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})
    first = list_step_records(engine, task.id)[0]

    # Wait for the *second* prompt: the engine unlinks any stale outcome file
    # before re-prompting, so a file written against the first turn is
    # deliberately not this attempt's result.
    writer = asyncio.create_task(
        _write_outcome_when_session_prompted(
            supervisor,
            task.id,
            "main",
            Path(task.clone_path),
            json.dumps({"version": 1, "status": "success", "summary": "second try"}),
            prompt_count=2,
        )
    )
    retry(runner, engine, task)
    await wait_for_run(engine, task.id, {"complete"})
    await writer

    records = list_step_records(engine, task.id)
    assert [(r.step, r.status) for r in records] == [("work", "failed"), ("work", "ok")]
    # The original evidence survives, with the retry recorded beside it.
    assert "no outcome file" in records[0].error
    assert "retried by the operator" in records[0].error
    assert records[0].pause is None
    assert records[0].seq == first.seq
    assert records[1].outcome["summary"] == "second try"

    prompts = user_prompts(supervisor, task.id, "main")
    assert len(prompts) == 2
    assert prompts[1].startswith("Your previous attempt at this step")
    assert "inspect the working tree first" in prompts[1]
    # Not the restart nudge: the previous turn ran to completion.
    assert "daemon restarted" not in prompts[1]
    # The original instruction travels with the retry.
    assert "do it" in prompts[1]


async def test_a_retry_announces_exactly_one_new_attempt(
    rig, engine, project, tmp_path: Path, outcome_workflow
) -> None:
    """One attempt, one `started`.

    A client that appends a record on `started` would otherwise draw a third
    attempt that never existed — which is what a browser check caught.
    """
    runner, _supervisor, _tracker, hub, _scenario = rig
    task = _make_task(engine, tmp_path, workflow="outcome-wf")

    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})

    events: list[dict] = []
    queue = hub.subscribe()

    async def collect() -> None:
        while True:
            event = await queue.get()
            if event.type == "workflow_step":
                events.append(event.payload)

    collector = asyncio.create_task(collect())
    retry(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})
    await asyncio.sleep(0.2)
    collector.cancel()

    assert [e["status"] for e in events] == ["started", "waiting"]
    assert len(list_step_records(engine, task.id)) == 2


async def test_a_repeated_missing_result_pauses_again(
    rig, engine, project, tmp_path: Path, outcome_workflow
) -> None:
    """Retrying is not a second chance at guessing: the same absence stops the
    run again rather than falling through."""
    runner, _supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, workflow="outcome-wf")

    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})
    retry(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})

    records = list_step_records(engine, task.id)
    assert [(r.step, r.status) for r in records] == [("work", "failed"), ("work", "waiting")]
    assert records[-1].pause["reason"] == "missing_outcome"


async def test_a_retry_cannot_walk_past_a_declared_visit_bound(
    rig, engine, project, tmp_path: Path
) -> None:
    """An operator retry is a human decision, not a way around a bound.

    Retrying the blocked step is what the action means — right up to the point
    where the definition says that step has no attempts left. From there the
    run goes to the declared exhaustion gate, exactly as the engine would have
    sent it.
    """
    install_test_workflow(
        engine,
        """
format: 1
name: bounded-retry-wf
sessions: [main]
primary: main
steps:
  - name: work
    kind: agent
    session: main
    expects_outcome: true
    max_visits: 2
    on_exhausted: {step: stop}
    prompt:
      parts:
        - text: "do it"
  - name: stop
    kind: gate
    message:
      parts:
        - text: "out of attempts"
""",
    )
    runner, _supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, workflow="bounded-retry-wf")

    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})

    # Attempt 1 paused. The bound allows a second, so the retry is a retry.
    retry(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})
    records = list_step_records(engine, task.id)
    assert [(r.step, r.status) for r in records] == [
        ("work", "failed"),
        ("work", "waiting"),
    ]

    # Attempt 2 paused, and the bound is now spent: the next retry lands on
    # the declared gate rather than opening a third attempt.
    retry(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})
    records = list_step_records(engine, task.id)
    assert [(r.step, r.kind, r.status) for r in records] == [
        ("work", "agent", "failed"),
        ("work", "agent", "failed"),
        ("stop", "gate", "waiting"),
    ]
    assert records[-1].outcome == {"message": "out of attempts"}
    assert records[-1].pause is None
    assert len([r for r in records if r.step == "work"]) == 2


async def test_a_stale_or_repeated_retry_cannot_advance_another_attempt(
    rig, engine, project, tmp_path: Path, outcome_workflow
) -> None:
    """A second browser tab and a double submit look identical from here, and
    both must be refused rather than applied to whatever is waiting now."""
    runner, _supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, workflow="outcome-wf")

    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})
    stale = waiting_seq(engine, task.id)

    retry(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})

    revision = resolve_task_definition(engine, get_task(engine, task.id))
    with pytest.raises(WorkflowWaitConflictError):
        runner.retry_step(get_task(engine, task.id), revision, expected_seq=stale)
    # And resuming it as if it were a declared gate is refused too.
    with pytest.raises(WorkflowNotWaitingError):
        runner.resume_gate(
            task.id, expected_seq=waiting_seq(engine, task.id), note="carry on"
        )


async def test_a_pause_survives_restart_without_a_new_prompt(
    rig, engine, project, tmp_path: Path, outcome_workflow
) -> None:
    """Recovery re-arms a persisted pause. It does not re-prompt, does not
    retry automatically, and does not append a second attempt."""
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, workflow="outcome-wf")

    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})
    await runner.shutdown()
    await supervisor.shutdown()

    runner2, supervisor2, tracker2, hub2 = _restart_rig(engine, tmp_path)
    events: list[dict] = []
    queue = hub2.subscribe()

    async def collect() -> None:
        while True:
            event = await queue.get()
            if event.type == "workflow_step":
                events.append(event.payload)

    collector = asyncio.create_task(collect())
    await _resume_recorded_sessions(engine, supervisor2, tracker2, task)
    _recover(runner2, engine, get_task(engine, task.id))
    await asyncio.sleep(0.3)
    collector.cancel()

    assert get_task(engine, task.id).workflow_status == "waiting"
    records = list_step_records(engine, task.id)
    assert len(records) == 1 and records[0].status == "waiting"
    assert records[0].pause["reason"] == "missing_outcome"
    assert [e["status"] for e in events] == ["waiting"]
    assert events[0]["pause"]["retry_step"] == "work"
    # No prompt was sent by the recovery itself.
    assert user_prompts(supervisor2, task.id, "main") == []
    await runner2.shutdown()
    await supervisor2.shutdown()


# --- command and decision steps -----------------------------------------------


async def test_command_and_decision_routing(rig, engine, project, tmp_path: Path, branch_workflow) -> None:
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, workflow="branch-wf")

    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"complete"})

    records = list_step_records(engine, task.id)
    assert [r.step for r in records] == ["probe", "route", "fix"]
    assert records[0].outcome == {"exit_code": 0, "output": ""}
    assert records[1].outcome == {"route": "fix"}
    assert records[2].kind == "agent" and records[2].status == "ok"
    assert user_prompts(supervisor, task.id, "main") == ["fix it"]


async def test_command_exit_code_is_outcome_data(
    rig, engine, project, tmp_path: Path, branch_workflow, fake_workshop_cli: Path
) -> None:
    """A non-zero command exit finishes the step ok with the code as outcome;
    the decision then routes on it (here: to the `bail` gate). A declared
    negative result is data, not missing evidence — it never pauses."""
    fake_workshop_cli.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *"config get ask.timeout"*) echo 0 ;;\n'
        '  *"--mode rpc-ui"*) exit 1 ;;\n'
        '  *probe-cmd*) echo "probe output"; exit 3 ;;\n'
        '  *) exit 0 ;;\n'
        "esac\n"
    )
    runner, _supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, workflow="branch-wf")

    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})

    records = list_step_records(engine, task.id)
    assert records[0].step == "probe"
    assert records[0].status == "ok"
    assert records[0].outcome["exit_code"] == 3
    assert "probe output" in records[0].outcome["output"]
    assert records[1].outcome == {"route": "bail"}
    # The run parked at the declared gate with its message — not a pause.
    gate = records[2]
    assert gate.kind == "gate" and gate.status == "waiting"
    assert gate.outcome == {"message": "probe failed; operator call"}
    assert gate.pause is None

    resume(runner, engine, task.id, note="looks fine")
    await wait_for_run(engine, task.id, {"complete"})
    records = list_step_records(engine, task.id)
    gate = records[2]
    assert gate.status == "ok"
    assert gate.outcome == {"message": "probe failed; operator call", "note": "looks fine"}
    # Resuming continues at the gate's fall-through: `fix` runs.
    assert [r.step for r in records] == ["probe", "route", "bail", "fix"]
    assert records[3].status == "ok"


async def test_command_infra_failure_fails_run(
    rig, engine, project, tmp_path: Path, fake_workshop_cli: Path
) -> None:
    """`workshop exec` itself failing (timeout here) fails the step and the
    run; the task registry state stays `created`."""
    fake_workshop_cli.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *"config get ask.timeout"*) echo 0 ;;\n'
        "  *slow-cmd*) sleep 30 ;;\n"
        '  *) exit 0 ;;\n'
        "esac\n"
    )
    install_test_workflow(
        engine,
        """
format: 1
name: slow-cmd-wf
sessions: [main]
primary: main
steps:
  - name: slow
    kind: command
    argv: ["slow-cmd"]
    timeout: 0.2
    idempotent: true
""",
    )
    runner, _supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, workflow="slow-cmd-wf")

    _start(runner, engine, task)
    final = await wait_for_run(engine, task.id, {"failed"})

    assert final.state == "created"
    records = list_step_records(engine, task.id)
    assert records[0].status == "failed"
    assert "timed out" in (records[0].error or "")


async def test_an_undecidable_route_pauses_with_its_reason(
    rig, engine, project, tmp_path: Path
) -> None:
    """A decision whose evidence is missing stops the run and names what it
    was waiting for. It does not skip to a later case, and it does not fall
    through to the next step as if the rule had said "no"."""
    install_test_workflow(
        engine,
        """
format: 1
name: undecidable-wf
sessions: [main]
primary: main
steps:
  - name: triage
    kind: decision
    cases:
      - when:
          op: eq
          left:
            op: get
            value: {op: latest, steps: [after]}
            keys: [outcome, exit_code]
          right: {op: literal, value: 0}
        next: {complete: true}
    otherwise: {step: after}
  - name: after
    kind: command
    argv: ["true"]
    timeout: 5
    idempotent: true
""",
    )
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, workflow="undecidable-wf")

    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})

    records = list_step_records(engine, task.id)
    assert len(records) == 1
    paused = records[0]
    # The decision keeps its own kind and its absent outcome: no synthesized
    # gate record, and nothing that could later read as a route.
    assert (paused.step, paused.kind, paused.status) == ("triage", "decision", "waiting")
    assert paused.outcome is None
    assert paused.pause["reason"] == "unresolved_decision"
    assert "is missing" in paused.pause["message"]
    assert paused.pause["retry_step"] == "triage"
    assert supervisor.get(task.id, "judge") is None

    # Retrying re-reads exactly the same recorded evidence, so it pauses
    # again. That is the honest answer, not a repair.
    retry(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})
    records = list_step_records(engine, task.id)
    assert [(r.step, r.status) for r in records] == [
        ("triage", "failed"),
        ("triage", "waiting"),
    ]


async def test_a_declared_otherwise_pause_is_a_route_the_author_chose(
    rig, engine, project, tmp_path: Path
) -> None:
    """`otherwise: {pause: true}` is a definition saying "ask a person" rather
    than inventing a destination."""
    install_test_workflow(
        engine,
        """
format: 1
name: declared-pause-wf
sessions: [main]
primary: main
steps:
  - name: probe
    kind: command
    argv: ["true"]
    timeout: 5
    idempotent: true
  - name: pick
    kind: decision
    cases:
      - when:
          op: eq
          left:
            op: get
            value: {op: latest, steps: [probe]}
            keys: [outcome, exit_code]
          right: {op: literal, value: 99}
        next: {complete: true}
    otherwise: {pause: true}
""",
    )
    runner, _supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, workflow="declared-pause-wf")

    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})

    paused = list_step_records(engine, task.id)[-1]
    assert paused.step == "pick" and paused.kind == "decision"
    assert paused.pause["reason"] == "unresolved_decision"
    assert "wait for a person" in paused.pause["message"]


async def test_resume_rejects_a_run_that_is_not_waiting(rig, engine, project, tmp_path: Path) -> None:
    runner, _supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path)

    with pytest.raises(WorkflowNotWaitingError):
        runner.resume_gate(task.id, expected_seq=1, note=None)

    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"complete"})
    with pytest.raises(WorkflowNotWaitingError):
        runner.resume_gate(task.id, expected_seq=1, note=None)


# --- multi-step, multi-session ------------------------------------------------


async def test_multi_step_run_with_two_named_sessions(rig, engine, project, tmp_path: Path) -> None:
    revision = install_test_workflow(engine, TWO_SESSION_YAML)
    runner, supervisor, tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, workflow="two-session-wf")

    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"complete"})

    # Each session ran exactly its own step, on its own child process. At the
    # second step's prompt time, history holds the first step's finished
    # record (its own record is excluded by the sequence boundary).
    assert user_prompts(supervisor, task.id, "coder") == ["implement it"]
    assert user_prompts(supervisor, task.id, "reviewer") == ["records so far: 1"]
    records = list_step_records(engine, task.id)
    assert [(r.step, r.session, r.status) for r in records] == [
        ("implement", "coder", "ok"),
        ("inspect", "reviewer", "ok"),
    ]
    for session in ("coder", "reviewer"):
        info = tracker.get(task.id, session)
        assert info is not None and info.status == "idle"
    assert revision.definition.primary == "reviewer"


# --- restart recovery (design D-6) --------------------------------------------


async def test_interrupted_agent_step_is_nudged_once(
    rig, engine, project, tmp_path: Path, outcome_workflow
) -> None:
    """Restart mid-turn: the resumed session gets ONE resume nudge (not the
    full prompt), on the SAME attempt — a restart is not a work attempt, and a
    declared visit bound must not count it as one."""
    runner, supervisor, _tracker, _hub, scenario = rig
    scenario["name"] = "no-end"  # burst without agent_end: the turn never ends
    task = _make_task(engine, tmp_path, workflow="outcome-wf")

    _start(runner, engine, task)
    # Wait until the prompt is durably marked sent (the crash window the
    # nudge path exists for).
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        records = list_step_records(engine, task.id)
        if records and records[0].prompted_at is not None:
            break
        await asyncio.sleep(0.02)
    assert records[0].prompted_at is not None

    await runner.shutdown()
    await supervisor.shutdown()
    scenario["name"] = "happy"
    runner2, supervisor2, tracker2, _hub2 = _restart_rig(engine, tmp_path)
    await _resume_recorded_sessions(engine, supervisor2, tracker2, task)

    writer = asyncio.create_task(
        _write_outcome_when_prompted(
            supervisor2,
            task.id,
            Path(task.clone_path),
            json.dumps({"version": 1, "status": "success", "summary": "finished"}),
        )
    )
    _recover(runner2, engine, get_task(engine, task.id))
    await wait_for_run(engine, task.id, {"complete"})
    await writer

    prompts = user_prompts(supervisor2, task.id, "main")
    assert len(prompts) == 1
    assert prompts[0].startswith("The daemon restarted")
    assert "outcome.json" in prompts[0]  # outcome-bearing: instruction re-stated
    records = list_step_records(engine, task.id)
    assert [(r.step, r.status) for r in records] == [("work", "ok")]
    await runner2.shutdown()
    await supervisor2.shutdown()


async def test_unsent_agent_step_sends_fresh_after_restart(
    rig, engine, project, tmp_path: Path
) -> None:
    """Crash after the attempt was opened but before the prompt went out:
    recovery sends the step prompt, not a nudge."""
    from ompire_daemon.registry.workflows import (
        append_step_record,
        finish_step_record,
        set_run_status,
    )

    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path)

    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"complete"})
    await runner.shutdown()
    await supervisor.shutdown()

    # Reset the persisted history to the pre-prompt state: one running record
    # with no prompted_at.
    records = list_step_records(engine, task.id)
    finish_step_record(engine, task.id, records[0].seq, status="ok")
    append_step_record(engine, task.id, step="work", kind="agent", session="main")
    set_run_status(engine, task.id, "running", "work")

    runner2, supervisor2, _tracker2, _hub2 = _restart_rig(engine, tmp_path)
    _recover(runner2, engine, get_task(engine, task.id))

    await wait_for_run(engine, task.id, {"complete"})
    assert user_prompts(supervisor2, task.id, "main") == ["do it"]
    await runner2.shutdown()
    await supervisor2.shutdown()


async def test_gate_survives_restart(rig, engine, project, tmp_path: Path) -> None:
    """A run waiting at a declared gate re-arms after a restart: same record,
    same message, resumable."""
    install_test_workflow(engine, GATE_YAML)
    runner, _supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, workflow="gate-wf")

    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})
    assert list_step_records(engine, task.id)[0].outcome == {"message": "ship it?"}

    await runner.shutdown()
    runner2, supervisor2, _tracker2, hub2 = _restart_rig(engine, tmp_path)
    events: list[dict] = []
    queue = hub2.subscribe()

    async def collect() -> None:
        while True:
            event = await queue.get()
            if event.type == "workflow_step":
                events.append(event.payload)

    collector = asyncio.create_task(collect())
    _recover(runner2, engine, get_task(engine, task.id))
    await asyncio.sleep(0.2)
    assert get_task(engine, task.id).workflow_status == "waiting"
    # Exactly one waiting re-broadcast, same record, same message.
    assert events == [
        {
            "task_id": task.id,
            # Addressed by sequence: a bounded step is visited under the same
            # name repeatedly, so the name alone cannot identify an attempt.
            "seq": 1,
            "step": "approve",
            "kind": "gate",
            "session": None,
            "status": "waiting",
            "message": "ship it?",
        }
    ]
    assert len(list_step_records(engine, task.id)) == 1

    resume(runner2, engine, task.id, note="yes")
    await wait_for_run(engine, task.id, {"complete"})
    collector.cancel()
    records = list_step_records(engine, task.id)
    assert records[0].status == "ok"
    assert records[0].outcome == {"message": "ship it?", "note": "yes"}
    assert [r.step for r in records] == ["approve", "after"]
    await runner2.shutdown()
    await supervisor2.shutdown()


# --- REST surface -------------------------------------------------------------


def test_workflow_resume_endpoint_404_and_409(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.post(
        "/api/tasks/999/workflow/resume", headers=auth_headers, json={"expected_seq": 1}
    )
    assert response.status_code == 404

    from .conftest import make_adoptable_checkout

    checkout = make_adoptable_checkout(client.app.state.config.checkout_root, "demo")
    r = client.post(
        "/api/projects",
        headers=auth_headers,
        json={
            "name": "demo",
            "title": "Demo",
            "upstream_url": "https://example.com/demo.git",
            "checkout_path": str(checkout),
        },
    )
    assert r.status_code == 201, r.text
    from ompire_daemon.work.tasks import create_task as _create

    task = _create(
        client.app.state.engine,
        project_name="demo",
        slug="idle-task",
        branch="ompire/idle-task",
        clone_path="/tmp/nonexistent-clone",
        prompt="x",
        execution_inputs=make_execution_inputs(checkout_path=str(checkout)),
    )
    response = client.post(
        f"/api/tasks/{task.id}/workflow/resume",
        headers=auth_headers,
        json={"expected_seq": 1},
    )
    assert response.status_code == 409

    # The waiting attempt has to be named: advancing "whatever is waiting now"
    # is not what an operator decided.
    missing = client.post(
        f"/api/tasks/{task.id}/workflow/resume", headers=auth_headers, json={}
    )
    assert missing.status_code == 422


def test_a_choice_is_required_at_a_choice_gate_and_refused_anywhere_else(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """The request contract, from the daemon's side.

    Which kind of wait this is comes from what the run is actually waiting on,
    never from what the caller sent. A gate with named choices needs one; an
    uncertainty pause and a format-1 gate refuse one, because answering with a
    choice would be answering a question nobody asked.
    """
    from ompire_daemon.registry.workflows import (
        append_step_record,
        build_gate_snapshot,
        build_pause,
        park_gate,
        pause_step,
        set_run_status,
    )
    from ompire_daemon.work.tasks import create_task as _create

    from .conftest import make_adoptable_checkout

    engine = client.app.state.engine
    checkout = make_adoptable_checkout(client.app.state.config.checkout_root, "demo")
    assert (
        client.post(
            "/api/projects",
            headers=auth_headers,
            json={
                "name": "demo",
                "title": "Demo",
                "upstream_url": "https://example.com/demo.git",
                "checkout_path": str(checkout),
            },
        ).status_code
        == 201
    )

    def waiting_task(slug: str):
        task = _create(
            engine,
            project_name="demo",
            slug=slug,
            branch=f"ompire/{slug}",
            clone_path="/tmp/nonexistent-clone",
            prompt="x",
            workflow_name="bugfix",
            execution_inputs=make_execution_inputs(
                checkout_path=str(checkout),
                workflow_name="bugfix",
            ),
        )
        set_run_status(engine, task.id, "running", "reproduction-gate")
        return task

    def post(task_id: int, **body):
        return client.post(
            f"/api/tasks/{task_id}/workflow/resume", headers=auth_headers, json=body
        )

    # --- a gate with declared choices -------------------------------------
    gated = waiting_task("gated")
    record = append_step_record(
        engine, gated.id, step="reproduction-gate", kind="gate"
    )
    snapshot = build_gate_snapshot(
        message="QA still cannot reproduce it.",
        choices=[
            {
                "id": "stop",
                "label": "Stop without a fix",
                "feedback_required": False,
                "next": {"complete": True, "result": "stopped-without-fix"},
            },
            {
                "id": "proceed-without-reproduction",
                "label": "Fix it anyway",
                "feedback_required": True,
                "next": {"step": "fix"},
            },
        ],
        evidence={},
    )
    park_gate(
        engine,
        gated.id,
        record.seq,
        step="reproduction-gate",
        message=snapshot["message"],
        snapshot=snapshot,
    )

    # No choice at all: the question has options, so it needs an answer.
    missing = post(gated.id, expected_seq=record.seq)
    assert missing.status_code == 422
    assert "choice_id" in missing.json()["detail"]

    # A choice this gate does not offer names the field, not a reload.
    unknown = post(gated.id, expected_seq=record.seq, choice_id="invented")
    assert unknown.status_code == 422
    assert unknown.json()["detail"]["field"] == "choice_id"

    # A choice that declares feedback required cannot be answered without it.
    blank = post(
        gated.id,
        expected_seq=record.seq,
        choice_id="proceed-without-reproduction",
        note="   ",
    )
    assert blank.status_code == 422
    assert blank.json()["detail"]["field"] == "note"

    # Feedback is bounded: it is stored, re-rendered, and handed to a prompt.
    too_long = post(
        gated.id,
        expected_seq=record.seq,
        choice_id="proceed-without-reproduction",
        note="x" * (16 * 1024 + 1),
    )
    assert too_long.status_code == 422
    assert too_long.json()["detail"]["field"] == "note"

    # An attempt the operator was not looking at is a conflict, not an answer.
    stale = post(gated.id, expected_seq=record.seq + 5, choice_id="stop")
    assert stale.status_code == 409

    # Nothing above advanced the run.
    assert get_task(engine, gated.id).workflow_status == "waiting"

    # --- an uncertainty pause ---------------------------------------------
    paused = waiting_task("paused")
    pause_record = append_step_record(
        engine, paused.id, step="reproduce", kind="agent", session="reproducer"
    )
    pause_step(
        engine,
        paused.id,
        pause_record.seq,
        pause=build_pause(
            reason="missing_outcome",
            message="no result",
            step="reproduce",
            retry_step="reproduce",
        ),
        error="no outcome file written",
    )
    refused = post(paused.id, expected_seq=pause_record.seq, choice_id="stop")
    assert refused.status_code == 422
    assert "not waiting at a gate with declared choices" in refused.json()["detail"]
    assert get_task(engine, paused.id).workflow_status == "waiting"

    # Unknown request fields are refused rather than ignored.
    extra = client.post(
        f"/api/tasks/{paused.id}/workflow/resume",
        headers=auth_headers,
        json={"expected_seq": pause_record.seq, "authorize": True},
    )
    assert extra.status_code == 422


def test_answering_a_gate_over_rest_records_it_and_advances_once(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """The accepted path, and its refusal on a second submit."""
    from ompire_daemon.registry.workflows import (
        append_step_record,
        build_gate_snapshot,
        list_step_records,
        park_gate,
        set_run_status,
    )
    from ompire_daemon.work.tasks import create_task as _create

    from .conftest import make_adoptable_checkout

    engine = client.app.state.engine
    checkout = make_adoptable_checkout(client.app.state.config.checkout_root, "demo")
    assert (
        client.post(
            "/api/projects",
            headers=auth_headers,
            json={
                "name": "demo",
                "title": "Demo",
                "upstream_url": "https://example.com/demo.git",
                "checkout_path": str(checkout),
            },
        ).status_code
        == 201
    )
    task = _create(
        engine,
        project_name="demo",
        slug="answerable",
        branch="ompire/answerable",
        clone_path="/tmp/nonexistent-clone",
        prompt="x",
        workflow_name="bugfix",
        execution_inputs=make_execution_inputs(
            checkout_path=str(checkout),
            workflow_name="bugfix",
        ),
    )
    set_run_status(engine, task.id, "running", "investigation-exhausted")
    record = append_step_record(
        engine, task.id, step="investigation-exhausted", kind="gate"
    )
    snapshot = build_gate_snapshot(
        message="Investigation has used up its attempts.",
        choices=[
            {
                "id": "stop",
                "label": "Stop without a fix",
                "feedback_required": False,
                "next": {"complete": True, "result": "stopped-without-fix"},
            }
        ],
        evidence={},
    )
    park_gate(
        engine,
        task.id,
        record.seq,
        step="investigation-exhausted",
        message=snapshot["message"],
        snapshot=snapshot,
    )

    accepted = client.post(
        f"/api/tasks/{task.id}/workflow/resume",
        headers=auth_headers,
        json={"expected_seq": record.seq, "choice_id": "stop", "note": "agreed"},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["workflow"] == "answered"
    assert accepted.json()["result"] == "stopped-without-fix"

    answered = list_step_records(engine, task.id)[-1]
    decision = answered.outcome["decision"]
    assert decision["choice_id"] == "stop"
    assert decision["feedback"] == "agreed"
    assert decision["actor"] == "operator"
    # The question is still readable beside the answer.
    assert answered.outcome["message"].startswith("Investigation has used up")
    # And the run's ending is named on the task itself.
    final = get_task(engine, task.id)
    assert (final.workflow_status, final.workflow_result) == (
        "complete",
        "stopped-without-fix",
    )
    assert final.workflow_result in task_payload(final, engine=engine).values()

    # A second submit of the same decision advances nothing.
    replay = client.post(
        f"/api/tasks/{task.id}/workflow/resume",
        headers=auth_headers,
        json={"expected_seq": record.seq, "choice_id": "stop"},
    )
    assert replay.status_code in (409, 422)
    assert len(list_step_records(engine, task.id)) == 1


def test_a_retained_revision_is_readable_and_a_damaged_one_is_classified(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """Read-only inspection by content identity: the operator can see exactly
    what a task accepted, and an unreadable document is reported rather than
    executed to answer a read."""
    from sqlalchemy import text as sa_text

    from ompire_daemon.registry.workflow_definitions import clear_cache

    with client.app.state.engine.connect() as conn:
        revision = resolve_current(conn, "bugfix").revision
    response = client.get(f"/api/workflows/revisions/{revision}", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "bugfix"
    assert body["format"] == 3
    assert body["primary_session"] == "coder"
    assert body["definition"]["steps"][0]["name"] == "reproduce"

    assert (
        client.get(
            f"/api/workflows/revisions/sha256:{'0' * 64}", headers=auth_headers
        ).status_code
        == 404
    )

    with client.app.state.engine.begin() as conn:
        conn.execute(
            sa_text("UPDATE workflow_revisions SET document_json = :d WHERE revision = :r"),
            {"d": '{"format": 1, "name": "bugfix"}', "r": revision},
        )
    clear_cache()
    damaged = client.get(f"/api/workflows/revisions/{revision}", headers=auth_headers)
    assert damaged.status_code == 409
    detail = damaged.json()["detail"]
    assert detail["reason"] == "workflow_definition_unavailable"
    assert detail["unavailable_reason"] in ("invalid", "integrity")


# --- run completion -----------------------------------------------------------


async def test_decision_route_complete_finishes_run_early(
    rig, engine, project, tmp_path: Path
) -> None:
    """A decision routing to `complete` finishes the run without executing the
    remaining declared steps, recording the sentinel as its route outcome."""
    install_test_workflow(
        engine,
        """
format: 1
name: complete-wf
sessions: [main]
primary: main
steps:
  - name: probe
    kind: command
    argv: ["true"]
    timeout: 5
    idempotent: true
  - name: fin
    kind: decision
    cases:
      - when: true
        next: {complete: true}
    otherwise: {step: never}
  - name: never
    kind: command
    argv: ["false"]
    timeout: 5
    idempotent: true
""",
    )
    runner, _supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, workflow="complete-wf")

    _start(runner, engine, task)
    final = await wait_for_run(engine, task.id, {"complete"})

    assert final.workflow_status == "complete"
    records = list_step_records(engine, task.id)
    assert [r.step for r in records] == ["probe", "fin"]
    assert records[1].outcome == {"route": COMPLETE}
    assert records[1].status == "ok"


async def test_a_visit_bound_is_enforced_by_the_engine_not_the_route(
    rig, engine, project, tmp_path: Path
) -> None:
    """The bound is counted before a new attempt opens, so a route that keeps
    saying "go back" still cannot loop forever."""
    install_test_workflow(
        engine,
        """
format: 1
name: runaway-wf
sessions: [main]
primary: main
steps:
  - name: again
    kind: agent
    session: main
    max_visits: 2
    on_exhausted: {step: stop}
    prompt:
      parts:
        - text: "try again"
  - name: back
    kind: decision
    cases:
      - when: true
        next: {step: again}
    otherwise: {step: stop}
  - name: stop
    kind: gate
    message:
      parts:
        - text: "out of attempts"
""",
    )
    runner, _supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, workflow="runaway-wf")

    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})

    records = list_step_records(engine, task.id)
    assert [r.step for r in records if r.step == "again"] == ["again", "again"]
    assert records[-1].step == "stop" and records[-1].kind == "gate"
    assert records[-1].outcome == {"message": "out of attempts"}


# --- the bugfix built-in, format 2 -------------------------------------------
#
# The flow these cover is the epic's worked example, and the property that
# matters most is the one format 1 could not express: failing to reproduce
# does not end the run and does not permit a fix. It reaches diagnosis with
# its negative evidence, and a candidate cause goes back to QA — in QA's own
# session, against unfixed code — before anything is changed.


def _bug_result(result: str, summary: str, **artifacts) -> str:
    return json.dumps(
        {
            "version": 2,
            "result": result,
            "summary": summary,
            "artifacts": artifacts,
        }
    )


def _reproduced(summary: str = "reproduced it", *, script: bool = True) -> str:
    return _bug_result(
        "reproduced",
        summary,
        attempts="ran the failing input",
        expected_behavior="returns empty",
        observed_behavior="IndexError",
        reproduction_evidence="traceback in the log",
        script_available=script,
    )


def _not_reproduced(summary: str = "could not reproduce") -> str:
    return _bug_result(
        "not-reproduced",
        summary,
        attempts="ran the suite and the reported steps",
        observed_behavior="everything passed",
        missing_prerequisites="none known",
    )


def _candidate(summary: str = "found a candidate") -> str:
    return _bug_result(
        "candidate-found",
        summary,
        findings="the empty branch is unguarded",
        suspected_trigger="an empty input list",
        suggested_reproduction="call it with []",
    )


def _no_root_cause(summary: str = "nothing conclusive") -> str:
    return _bug_result(
        "no-root-cause",
        summary,
        findings="read the parser and the caller",
        missing_information="the exact input that failed",
    )


def _implemented(summary: str = "guarded the empty case") -> str:
    return _bug_result(
        "implemented", summary, changes="added a guard", validation_notes="ran the tests"
    )


def _verified(result: str, summary: str) -> str:
    return _bug_result(
        result,
        summary,
        checks="re-ran the reproduction",
        observations="no longer raises",
        limitations="only the reported input",
    )


def _feed(supervisor, task_id: int, clone: Path, plan: list[tuple[str, int, str]]):
    """Answer each session's Nth prompt with a result document.

    Every entry waits for its own session's own prompt count, so the writers
    fire in the order the run actually prompts rather than on a timer.
    """

    async def drive() -> None:
        for session, count, content in plan:
            await _write_outcome_when_session_prompted(
                supervisor, task_id, session, clone, content, prompt_count=count
            )

    return asyncio.create_task(drive())


async def _wait_for_prompted(
    engine: Engine, task_id: int, step: str, timeout: float = 10.0
) -> None:
    """Poll until `step`'s newest attempt has had its prompt sent."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        matching = [r for r in list_step_records(engine, task_id) if r.step == step]
        if matching and matching[-1].prompted_at is not None:
            return
        await asyncio.sleep(0.02)
    raise RuntimeError(f"{step!r} was never prompted")


async def _wait_for_step_count(
    engine: Engine, task_id: int, step: str, count: int, timeout: float = 10.0
) -> None:
    """Poll until `step` has been attempted `count` times."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        records = list_step_records(engine, task_id)
        if len([r for r in records if r.step == step]) >= count:
            return
        await asyncio.sleep(0.02)
    raise RuntimeError(f"{step!r} never reached {count} attempts")


def _failing_repro_script(fake_workshop_cli: Path) -> None:
    """Make `bash .ompire/repro.sh` exit non-zero through the fake workshop."""
    fake_workshop_cli.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *"config get ask.timeout"*) echo 0 ;;\n'
        '  *"--mode rpc-ui"*) exit 1 ;;\n'
        '  *repro.sh*) echo "still broken"; exit 1 ;;\n'
        '  *) exit 0 ;;\n'
        "esac\n"
    )


def _bugfix_task(
    engine, tmp_path: Path, slug: str, workflow: str = "bugfix", **kwargs
) -> Task:
    return _make_task(
        engine,
        tmp_path,
        workflow=workflow,
        slug=slug,
        prompt="bug: off by one",
        **kwargs,
    )


async def _finish_without_publishing(runner, engine, task):
    """Answer the bugfix approval with the ending that publishes nothing.

    A validated fix no longer ends the run: it goes to review and then to a
    person. Every test that used to assert `validated` now has to say who
    decided that, which is the point of the change.
    """
    waiting = await wait_for_run(engine, task.id, {"waiting", "complete", "failed"})
    assert waiting.workflow_status == "waiting", waiting.workflow_status
    record = list_step_records(engine, task.id)[-1]
    assert record.step in ("approve", "approve-unreproduced"), record.step
    return runner.answer_gate(
        get_task(engine, task.id),
        resolve_task_definition(engine, task),
        expected_seq=record.seq,
        choice_id="finish",
        note=None,
    )


async def test_bugfix_reproduced_runs_through_diagnosis_script_and_qa(
    rig, engine, project, tmp_path: Path
) -> None:
    """The straightforward path — and it still spends a QA turn.

    A passing script says the script passes. The epic's contract is that QA
    verifies in its original session, so the script is evidence handed to that
    turn rather than a substitute for it.
    """
    runner, supervisor, tracker, _hub, _scenario = rig
    task = _bugfix_task(engine, tmp_path, "bugfix-happy", preamble="PRE")
    clone = Path(task.clone_path)
    driver = _feed(
        supervisor,
        task.id,
        clone,
        [
            ("reproducer", 1, _reproduced()),
            ("coder", 1, _candidate()),
            ("coder", 2, _implemented()),
            ("reproducer", 2, _verified("validated", "the bug is gone")),
        ],
    )
    _start(runner, engine, task)
    final = await _finish_without_publishing(runner, engine, task)
    await driver

    records = list_step_records(engine, task.id)
    assert [(r.step, r.kind, r.status) for r in records] == [
        ("reproduce", "agent", "ok"),
        ("diagnose", "agent", "ok"),
        ("route-diagnosis", "decision", "ok"),
        ("fix", "agent", "ok"),
        ("route-fix", "decision", "ok"),
        ("run-script", "command", "ok"),
        ("verify", "agent", "ok"),
        ("route-verification", "decision", "ok"),
        ("review", "review", "ok"),
        ("route-review", "decision", "ok"),
        ("approve", "gate", "ok"),
    ]
    # A validated fix is reviewed and then decided; nothing was published, and
    # the ending says exactly that.
    assert final.workflow_result == "validated"
    assert records[-1].outcome["decision"]["choice_id"] == "finish"
    # QA reproduced and verified in one conversation; the coder owns the change.
    assert len(user_prompts(supervisor, task.id, "reproducer")) == 2
    assert tracker.get(task.id, "coder") is not None
    assert supervisor.get(task.id, "judge") is None
    repro_prompt = user_prompts(supervisor, task.id, "reproducer")[0]
    assert repro_prompt.startswith("PRE\n\n")
    assert "bug: off by one" in repro_prompt
    # Verification was handed the script's result, not asked to trust the coder.
    verify_prompt = user_prompts(supervisor, task.id, "reproducer")[1]
    assert "exited 0" in verify_prompt
    assert "do not take the coder's word" in verify_prompt


async def test_bugfix_non_reproduction_reaches_diagnosis_and_returns_to_qa(
    rig, engine, project, tmp_path: Path
) -> None:
    """The epic's headline journey.

    QA cannot reproduce. That does not end the run and does not authorize a
    fix: diagnosis gets the negative evidence, its candidate findings go back
    to the *same QA session* against still-unfixed code, and only QA's second
    attempt establishes the reproduction that permits fixing.
    """
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _bugfix_task(engine, tmp_path, "bugfix-informed")
    clone = Path(task.clone_path)
    driver = _feed(
        supervisor,
        task.id,
        clone,
        [
            ("reproducer", 1, _not_reproduced("nothing on main")),
            ("coder", 1, _candidate("the empty branch looks wrong")),
            ("reproducer", 2, _reproduced("now it fails", script=False)),
            ("coder", 2, _implemented()),
            ("reproducer", 3, _verified("validated", "gone")),
        ],
    )
    _start(runner, engine, task)
    final = await _finish_without_publishing(runner, engine, task)
    await driver

    records = list_step_records(engine, task.id)
    assert [r.step for r in records] == [
        "reproduce",
        "diagnose",
        "route-diagnosis",
        "reproduce-informed",
        "route-informed",
        "fix",
        "route-fix",
        "verify",
        "route-verification",
        "review",
        "route-review",
        "approve",
    ]
    assert final.workflow_result == "validated"
    # Diagnosis saw the failure as a failure, with what QA tried intact.
    diagnose_prompt = user_prompts(supervisor, task.id, "coder")[0]
    assert "not-reproduced" in diagnose_prompt
    assert "ran the suite and the reported steps" in diagnose_prompt
    assert "not proof that the code is correct" in diagnose_prompt
    assert "do not commit" in diagnose_prompt.lower()
    # The informed attempt is the same QA conversation, told what to try, and
    # told the code is still unfixed.
    informed_prompt = user_prompts(supervisor, task.id, "reproducer")[1]
    assert "the empty branch is unguarded" in informed_prompt
    assert "call it with []" in informed_prompt
    assert "still unfixed" in informed_prompt
    assert "plausible is not a reproduction" in informed_prompt
    # The fix was given the *informed* reproduction, not the original failure.
    fix_record = next(r for r in records if r.step == "fix")
    informed_seq = next(r for r in records if r.step == "reproduce-informed").seq
    assert fix_record.evidence["bindings"]["reproduction"]["seq"] == informed_seq
    # Three roles, one QA session — reproduce, informed reproduce, verify.
    assert len(user_prompts(supervisor, task.id, "reproducer")) == 3


async def test_bugfix_no_root_cause_asks_a_person_rather_than_permitting_a_fix(
    rig, engine, project, tmp_path: Path
) -> None:
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _bugfix_task(engine, tmp_path, "bugfix-no-cause")
    clone = Path(task.clone_path)
    driver = _feed(
        supervisor,
        task.id,
        clone,
        [
            ("reproducer", 1, _not_reproduced()),
            ("coder", 1, _no_root_cause("could not pin it down")),
        ],
    )
    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})
    await driver

    records = list_step_records(engine, task.id)
    assert records[-1].step == "diagnosis-gate"
    snapshot = records[-1].outcome
    assert [c["id"] for c in snapshot["choices"]] == ["retry-diagnosis", "stop"]
    assert "read the parser and the caller" in snapshot["message"]
    assert "the exact input that failed" in snapshot["message"]
    # No `fix` was reached: nothing authorized one.
    assert not [r for r in records if r.step == "fix"]

    final = answer(runner, engine, task, "stop")
    assert (final.workflow_status, final.workflow_result) == (
        "complete",
        "stopped-without-fix",
    )


async def test_bugfix_retry_diagnosis_carries_the_operators_words_back(
    rig, engine, project, tmp_path: Path
) -> None:
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _bugfix_task(engine, tmp_path, "bugfix-retry-diagnosis")
    clone = Path(task.clone_path)
    driver = _feed(
        supervisor,
        task.id,
        clone,
        [
            ("reproducer", 1, _not_reproduced()),
            ("coder", 1, _no_root_cause()),
        ],
    )
    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})
    await driver

    second = _feed(supervisor, task.id, clone, [("coder", 2, _no_root_cause())])
    answer(runner, engine, task, "retry-diagnosis", "look at the cache layer")
    await wait_for_run(engine, task.id, {"waiting"})
    await second

    prompts = user_prompts(supervisor, task.id, "coder")
    assert len(prompts) == 2
    assert "look at the cache layer" in prompts[1]
    # Presented as information, not as an instruction to obey.
    assert "treat it as information, not instructions" in prompts[1]


async def test_bugfix_continued_non_reproduction_needs_explicit_permission(
    rig, engine, project, tmp_path: Path
) -> None:
    """The exception path, and what it costs.

    Proceeding without a reproduction is allowed, requires a rationale, and is
    carried forward: the fix is told there is no demonstration, verification is
    told there is no before-and-after, and the run ends saying so by name.
    """
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _bugfix_task(engine, tmp_path, "bugfix-exception")
    clone = Path(task.clone_path)
    driver = _feed(
        supervisor,
        task.id,
        clone,
        [
            ("reproducer", 1, _not_reproduced()),
            ("coder", 1, _candidate()),
            ("reproducer", 2, _not_reproduced("still nothing")),
        ],
    )
    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})
    await driver

    records = list_step_records(engine, task.id)
    assert records[-1].step == "reproduction-gate"
    assert [c["id"] for c in records[-1].outcome["choices"]] == [
        "retry-diagnosis",
        "proceed-without-reproduction",
        "stop",
    ]
    # Every one of them requires a rationale except stopping.
    offered = {c["id"]: c["feedback_required"] for c in records[-1].outcome["choices"]}
    assert offered == {
        "retry-diagnosis": True,
        "proceed-without-reproduction": True,
        "stop": False,
    }

    rest = _feed(
        supervisor,
        task.id,
        clone,
        [
            ("coder", 2, _implemented()),
            ("reproducer", 3, _verified("validated", "looks right now")),
        ],
    )
    answer(
        runner,
        engine,
        task,
        "proceed-without-reproduction",
        "customer confirmed it on their build; ship the guard",
    )
    final = await _finish_without_publishing(runner, engine, task)
    await rest

    # The ending says what it is: validated, but never demonstrated.
    assert final.workflow_result == "validated-without-reproduction"
    fix_prompt = user_prompts(supervisor, task.id, "coder")[1]
    assert "nobody has demonstrated this bug" in fix_prompt
    assert "customer confirmed it on their build" in fix_prompt
    verify_prompt = user_prompts(supervisor, task.id, "reproducer")[2]
    assert "never reproduced" in verify_prompt
    assert "do not describe this as a verified before/after" in verify_prompt


async def test_bugfix_a_rejected_fix_goes_back_with_the_report(
    rig, engine, project, tmp_path: Path
) -> None:
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _bugfix_task(engine, tmp_path, "bugfix-rejected")
    clone = Path(task.clone_path)
    driver = _feed(
        supervisor,
        task.id,
        clone,
        [
            ("reproducer", 1, _reproduced(script=False)),
            ("coder", 1, _candidate()),
            ("coder", 2, _implemented("first attempt")),
            (
                "reproducer",
                2,
                _bug_result(
                    "rejected",
                    "still broken",
                    checks="re-ran the reproduction",
                    observations="still raises IndexError",
                    limitations="none",
                ),
            ),
            ("coder", 3, _implemented("second attempt")),
            ("reproducer", 3, _verified("validated", "fixed now")),
        ],
    )
    _start(runner, engine, task)
    final = await _finish_without_publishing(runner, engine, task)
    await driver

    assert final.workflow_result == "validated"
    records = list_step_records(engine, task.id)
    fixes = [r for r in records if r.step == "fix"]
    assert len(fixes) == 2
    # The second fix carries the rejection of the first, bound to that exact
    # attempt rather than to whatever verification is newest.
    second_fix_prompt = user_prompts(supervisor, task.id, "coder")[2]
    assert "did NOT pass verification" in second_fix_prompt
    assert "still raises IndexError" in second_fix_prompt
    first_verify = next(r for r in records if r.step == "verify")
    assert fixes[1].evidence["bindings"]["rejection"]["seq"] == first_verify.seq
    # And the verification that passed checked the *second* fix.
    last_verify = [r for r in records if r.step == "verify"][-1]
    assert last_verify.evidence["bindings"]["fix"]["seq"] == fixes[1].seq


async def test_bugfix_a_failing_script_is_not_overridable_by_a_positive_verdict(
    rig, engine, project, tmp_path: Path, fake_workshop_cli: Path
) -> None:
    """Deterministic evidence wins.

    If the reproducer script still fails, the bug is still there — whatever
    the verification turn concluded. The run goes back to the coder.
    """
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _bugfix_task(engine, tmp_path, "bugfix-script-fails")
    clone = Path(task.clone_path)
    (clone / ".ompire").mkdir(parents=True, exist_ok=True)
    _failing_repro_script(fake_workshop_cli)
    driver = _feed(
        supervisor,
        task.id,
        clone,
        [
            ("reproducer", 1, _reproduced()),
            ("coder", 1, _candidate()),
            ("coder", 2, _implemented()),
            ("reproducer", 2, _verified("validated", "looks fine to me")),
        ],
    )
    _start(runner, engine, task)
    # The positive verdict does not complete the run; it re-enters `fix`.
    await _wait_for_step_count(engine, task.id, "fix", 2)
    driver.cancel()

    records = list_step_records(engine, task.id)
    script = next(r for r in records if r.step == "run-script")
    assert script.outcome["exit_code"] != 0
    verify = next(r for r in records if r.step == "verify")
    assert verify.outcome["result"] == "validated"
    route = [r for r in records if r.step == "route-verification"][-1]
    assert route.outcome == {"route": "fix"}
    assert get_task(engine, task.id).workflow_result is None


async def test_bugfix_an_inconclusive_verification_asks_rather_than_deciding(
    rig, engine, project, tmp_path: Path
) -> None:
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _bugfix_task(engine, tmp_path, "bugfix-inconclusive")
    clone = Path(task.clone_path)
    driver = _feed(
        supervisor,
        task.id,
        clone,
        [
            ("reproducer", 1, _reproduced(script=False)),
            ("coder", 1, _candidate()),
            ("coder", 2, _implemented()),
            ("reproducer", 2, _verified("inconclusive", "cannot tell from here")),
        ],
    )
    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})
    await driver

    records = list_step_records(engine, task.id)
    assert records[-1].step == "validation-gate"
    assert [c["id"] for c in records[-1].outcome["choices"]] == [
        "retry-verification",
        "stop",
    ]
    # Neither a pass nor a failure was recorded on the way here.
    assert next(r for r in records if r.step == "verify").outcome["result"] == (
        "inconclusive"
    )

    final = answer(runner, engine, task, "stop")
    assert (final.workflow_status, final.workflow_result) == (
        "complete",
        "stopped-unvalidated",
    )


async def test_bugfix_unable_to_fix_stops_instead_of_entering_verification(
    rig, engine, project, tmp_path: Path
) -> None:
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _bugfix_task(engine, tmp_path, "bugfix-unable")
    clone = Path(task.clone_path)
    driver = _feed(
        supervisor,
        task.id,
        clone,
        [
            ("reproducer", 1, _reproduced(script=False)),
            ("coder", 1, _candidate()),
            (
                "coder",
                2,
                _bug_result(
                    "unable-to-fix",
                    "needs an API change we cannot make here",
                    changes="none",
                    validation_notes="nothing to validate",
                ),
            ),
        ],
    )
    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})
    await driver

    records = list_step_records(engine, task.id)
    assert records[-1].step == "correction-exhausted"
    assert not [r for r in records if r.step == "verify"]
    final = answer(runner, engine, task, "stop")
    assert final.workflow_result == "stopped-unvalidated"


async def test_bugfix_a_missing_result_pauses_and_keeps_its_reason(
    rig, engine, project, tmp_path: Path
) -> None:
    runner, _supervisor, _tracker, _hub, _scenario = rig
    task = _bugfix_task(engine, tmp_path, "bugfix-missing-result")

    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})

    record = list_step_records(engine, task.id)[-1]
    assert (record.step, record.status) == ("reproduce", "waiting")
    assert record.outcome is None
    assert record.pause["reason"] == "missing_outcome"
    assert record.pause["retry_step"] == "reproduce"


async def test_bugfix_restart_during_the_return_to_qa_keeps_the_conversation(
    rig, engine, project, tmp_path: Path
) -> None:
    """The interruption that would be worst to get wrong.

    The daemon dies mid-way through QA's informed reproduction. On restart the
    same attempt is re-driven in the same native session — nudged, not
    re-prompted from scratch — and its evidence still points at the diagnosis
    that sent it back.
    """
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _bugfix_task(engine, tmp_path, "bugfix-restart-informed")
    clone = Path(task.clone_path)
    driver = _feed(
        supervisor,
        task.id,
        clone,
        [
            ("reproducer", 1, _not_reproduced()),
            ("coder", 1, _candidate()),
        ],
    )
    _start(runner, engine, task)
    # Wait until QA has actually been sent back with the findings, then die
    # mid-turn: an attempt whose prompt was never delivered has no lost turn
    # to resume, and would correctly be prompted afresh.
    await _wait_for_prompted(engine, task.id, "reproduce-informed")
    await driver
    informed = next(
        r for r in list_step_records(engine, task.id) if r.step == "reproduce-informed"
    )
    await runner.shutdown()
    await supervisor.shutdown()

    runner2, supervisor2, tracker2, _hub2 = _restart_rig(engine, tmp_path)
    try:
        await _resume_recorded_sessions(
            engine, supervisor2, tracker2, get_task(engine, task.id)
        )
        rest = _feed(
            supervisor2,
            task.id,
            clone,
            [
                ("reproducer", 1, _reproduced("now it fails", script=False)),
                ("coder", 1, _implemented()),
                ("reproducer", 2, _verified("validated", "gone")),
            ],
        )
        _recover(runner2, engine, get_task(engine, task.id))
        final = await _finish_without_publishing(runner2, engine, task)
        await rest

        assert final.workflow_result == "validated"
        after = list_step_records(engine, task.id)
        # One informed attempt, not two: a restart is not a work attempt.
        assert len([r for r in after if r.step == "reproduce-informed"]) == 1
        again = next(r for r in after if r.step == "reproduce-informed")
        assert again.seq == informed.seq
        assert again.evidence == informed.evidence
        # The resumed turn was nudged into its existing conversation.
        assert user_prompts(supervisor2, task.id, "reproducer")[0].startswith(
            "The daemon restarted"
        )
    finally:
        await runner2.shutdown()
        await supervisor2.shutdown()


async def test_bugfix_exhausted_investigation_can_only_stop(
    rig, engine, project, tmp_path: Path
) -> None:
    """Three diagnoses is the run's budget, and no answer refills it."""
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _bugfix_task(engine, tmp_path, "bugfix-exhausted")
    clone = Path(task.clone_path)
    driver = _feed(
        supervisor,
        task.id,
        clone,
        [("reproducer", 1, _not_reproduced()), ("coder", 1, _no_root_cause())],
    )
    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})
    await driver

    for attempt in (2, 3):
        more = _feed(supervisor, task.id, clone, [("coder", attempt, _no_root_cause())])
        answer(runner, engine, task, "retry-diagnosis", f"hint {attempt}")
        await wait_for_run(engine, task.id, {"waiting"})
        await more

    assert len([r for r in list_step_records(engine, task.id) if r.step == "diagnose"]) == 3
    # A fourth retry reaches the exhaustion gate instead of a fourth diagnosis.
    answer(runner, engine, task, "retry-diagnosis", "one more please")
    await wait_for_run(engine, task.id, {"waiting"})
    records = list_step_records(engine, task.id)
    assert len([r for r in records if r.step == "diagnose"]) == 3
    assert records[-1].step == "investigation-exhausted"
    assert [c["id"] for c in records[-1].outcome["choices"]] == ["stop"]

    final = answer(runner, engine, task, "stop")
    assert final.workflow_result == "stopped-without-fix"


async def test_bugfix_editing_the_library_does_not_touch_a_running_task(
    rig, engine, project, tmp_path: Path
) -> None:
    """The pinning property, restated for the new definition.

    The packaged `bugfix` is read-only, so the edit happens where an operator's
    edits actually happen: a custom copy of it, saved as a new executable
    revision while a task accepted under the old one is waiting at a gate.
    """
    packaged = (
        resources.files("ompire_daemon.builtin_workflows")
        .joinpath("bugfix.yaml")
        .read_text(encoding="utf-8")
        .replace("name: bugfix", "name: my-bugfix", 1)
    )
    install_test_workflow(engine, packaged)
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _bugfix_task(engine, tmp_path, "bugfix-pinned", workflow="my-bugfix")
    pinned = resolve_task_definition(engine, task).revision
    clone = Path(task.clone_path)
    driver = _feed(
        supervisor,
        task.id,
        clone,
        [("reproducer", 1, _not_reproduced()), ("coder", 1, _no_root_cause())],
    )
    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})
    await driver

    gate_before = list_step_records(engine, task.id)[-1].outcome
    # Edit the library under the running task: a different label on a choice.
    install_test_workflow(
        engine, packaged.replace("label: Stop without a fix", "label: Abandon it")
    )
    with engine.connect() as conn:
        assert resolve_current(conn, "my-bugfix").revision != pinned

    # The waiting question is unchanged, and answering it uses what it asked.
    assert list_step_records(engine, task.id)[-1].outcome == gate_before
    final = answer(runner, engine, task, "stop")
    assert final.workflow_result == "stopped-without-fix"
    assert resolve_task_definition(engine, task).revision == pinned

# --- prompt file mentions (add-spawn-file-mentions) --------------------------


async def test_mention_resolving_in_the_clone_is_delivered_verbatim(
    rig, engine, project, tmp_path: Path
) -> None:
    """Omp parses `@path` out of the `message` field itself, so the daemon
    delivers the literal mention (findings-omp-file-mentions.md)."""
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, prompt="read @notes.md")
    (Path(task.clone_path) / "notes.md").write_text("notes\n")

    _start(runner, engine, task)
    final = await wait_for_run(engine, task.id, {"complete"})

    assert final.workflow_status == "complete"
    assert user_prompts(supervisor, task.id, "main") == ["read @notes.md"]


async def test_mention_missing_from_the_clone_fails_the_step_without_prompting(
    rig, engine, project, tmp_path: Path
) -> None:
    """The base checkout moved between submit and clone. Omp would drop the
    mention silently, so the daemon refuses to send the prompt at all."""
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, prompt="read @gone.md")

    _start(runner, engine, task)
    final = await wait_for_run(engine, task.id, {"failed"})

    assert final.state == "created"
    records = list_step_records(engine, task.id)
    assert records[0].status == "failed"
    assert "@gone.md" in (records[0].error or "")
    assert "does not resolve in the task clone" in (records[0].error or "")
    # Nothing was sent: the agent never saw a prompt with a dead reference.
    assert user_prompts(supervisor, task.id, "main") == []


async def test_a_preamble_at_sign_is_prose_and_never_fails_the_step(
    rig, engine, project, tmp_path: Path
) -> None:
    """Only the operator's own mentions are gated; the standing preamble is
    prose, not a place where `@word` means a file."""
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, preamble="ping @nobody about this", prompt="do it")

    _start(runner, engine, task)
    final = await wait_for_run(engine, task.id, {"complete"})

    assert final.workflow_status == "complete"
    assert user_prompts(supervisor, task.id, "main") == ["ping @nobody about this\n\ndo it"]


# --- per-consumer policy through the engine (ADR-0027) -----------------------

#: A second profile's four-role map, differing from `TEST_ROLES` in every
#: role, so a step bound to it needs a real process replacement rather than an
#: active-pair swap.
OTHER_ROLES = {
    "default": {"model": "testing/other-model", "thinking": "high"},
    "smol": {"model": "testing/other-smol", "thinking": "off"},
    "slow": {"model": "testing/other-slow", "thinking": "xhigh"},
    "plan": {"model": "testing/other-plan", "thinking": "low"},
}


@pytest.fixture
def shared_session_workflow(engine: Engine):
    """Two agent steps in one named session — the `reproduce`/`validate-agent`
    shape, reduced to what a policy handoff needs."""
    return install_test_workflow(engine, SHARED_SESSION_YAML)


async def test_two_steps_share_a_session_under_different_policies(
    engine: Engine, project, tmp_path: Path, rig, shared_session_workflow
) -> None:
    """Both steps keep the one logical session and its conversation while
    running under genuinely different bindings."""
    runner, supervisor, _, _, _ = rig
    task = _make_task(
        engine,
        tmp_path,
        workflow="shared-policy",
        step_profile_names={"second": "other"},
        step_profiles={"other": _role_bindings(OTHER_ROLES)},
    )

    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"complete", "failed"})
    assert get_task(engine, task.id).workflow_status == "complete"

    # One session, both turns: the second step continued the conversation the
    # first one started rather than opening a new one.
    prompts = user_prompts(supervisor, task.id, "shared")
    assert "first turn" in prompts
    assert "second turn" in prompts

    session = get_session(engine, task.id, "shared")
    # The session records the policy it *last* ran under, attributed to the
    # step that applied it.
    assert session.applied_policy is not None
    assert session.applied_policy.consumer_name == "second"
    assert session.applied_policy.policy.active.model == "testing/other-model"
    assert session.applied_policy.policy.slow.model == "testing/other-slow"
    assert session.applied_policy.verified
    # Native identity is preserved across the replacement.
    assert session.omp_session_id == "fake-session-id"


async def test_a_repeated_step_reuses_its_own_accepted_binding(
    engine: Engine, project, tmp_path: Path, rig
) -> None:
    """A loop revisit is the same accepted decision again, not whatever the
    session was last put on by the step in between."""
    install_test_workflow(engine, LOOP_YAML)
    runner, supervisor, _, _, _ = rig
    task = _make_task(
        engine,
        tmp_path,
        workflow="loop-policy",
        step_roles={"fix": "plan"},
    )
    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"complete", "failed"})
    assert get_task(engine, task.id).workflow_status == "complete"

    prompts = user_prompts(supervisor, task.id, "coder")
    # Two visits to the same step, each prompted under its own binding.
    assert "fix 0" in prompts and "fix 1" in prompts
    applied = get_session(engine, task.id, "coder").applied_policy
    assert applied.consumer_name == "fix"
    assert applied.role == "plan"
    assert applied.policy.active.model == TEST_ROLES["plan"]["model"]


async def test_a_failed_handoff_fails_the_step_and_keeps_the_previous_record(
    engine: Engine, project, tmp_path: Path, rig, shared_session_workflow
) -> None:
    """A refused native configuration must not authorize the prompt, and must
    not overwrite what the session is durably known to have run."""
    runner, _, _, _, _ = rig
    task = _make_task(
        engine,
        tmp_path,
        workflow="shared-policy",
        step_profile_names={"second": "other"},
        step_profiles={
            "other": _role_bindings(
                {**OTHER_ROLES, "default": {"model": "testing/unknown-model", "thinking": "low"}}
            )
        },
    )

    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"complete", "failed"})
    assert get_task(engine, task.id).workflow_status == "failed"

    records = list_step_records(engine, task.id)
    assert records[-1].step == "second"
    assert records[-1].status == "failed"
    # The durable record still describes the first step's verified policy: the
    # second never successfully applied one.
    applied = get_session(engine, task.id, "shared").applied_policy
    assert applied.consumer_name == "first"
    assert applied.policy.active.model == TEST_ROLES["default"]["model"]


async def test_recovery_restores_the_session_policy_that_actually_applied(
    engine: Engine, project, tmp_path: Path, rig, shared_session_workflow
) -> None:
    """After a restart the session resumes on what it last ran, not on the
    task's `default` role — the bug a single task-wide policy could not see."""
    runner, supervisor, _, _, _ = rig
    task = _make_task(
        engine,
        tmp_path,
        workflow="shared-policy",
        step_profile_names={"second": "other"},
        step_profiles={"other": _role_bindings(OTHER_ROLES)},
    )
    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"complete", "failed"})

    # A restart: the live children are gone, the registry is not.
    await supervisor.shutdown()
    assert supervisor.get(task.id, "shared") is None

    resumed = _make_task_policy_for_recovery(engine, task.id, "shared")
    assert resumed.policy.active.model == "testing/other-model"
    assert resumed.policy.plan.model == "testing/other-plan"


def _make_task_policy_for_recovery(engine: Engine, task_id: int, session: str):
    """What `recovery._continuation_policy` will hand the resume."""
    from ompire_daemon.recovery import _continuation_policy

    applied = _continuation_policy(engine, get_task(engine, task_id), session)
    assert applied is not None
    return applied


def _role_bindings(roles: dict) -> dict:
    from ompire_daemon.model_config import RoleBinding

    return {
        role: RoleBinding(model=pair["model"], thinking=pair["thinking"])
        for role, pair in roles.items()
    }


# --- format 2: declared results, frozen evidence, answered gates -------------
#
# What these cover is the difference format 2 makes at run time: a negative
# result routes instead of stopping, a consuming step is handed the exact
# attempt it was bound to rather than the newest one, and a human answer is
# committed before the run moves.

F2_YAML = """
format: 2
name: f2-wf
sessions: [qa, coder]
primary: coder
steps:
  - name: reproduce
    kind: agent
    session: qa
    max_visits: 3
    on_exhausted: {step: exhausted}
    outcome:
      results:
        reproduced:
          required: {attempts: string}
        not-reproduced:
          required: {attempts: string}
    prompt:
      parts:
        - text: "reproduce it"
  - name: route
    kind: decision
    evidence:
      repro: {steps: [reproduce]}
    cases:
      - when:
          op: eq
          left: {op: get, value: {op: evidence, name: repro}, keys: [outcome, result]}
          right: {op: literal, value: reproduced}
        next: {step: fix}
    otherwise: {step: decide}
  - name: fix
    kind: agent
    session: coder
    evidence:
      repro: {steps: [reproduce]}
    outcome: null
    prompt:
      separator: ""
      parts:
        - text: "fix what qa saw in attempt "
        - value: {op: get, value: {op: evidence, name: repro}, keys: [seq]}
          format: text
        - text: ": "
        - value: {op: get, value: {op: evidence, name: repro}, keys: [outcome, summary]}
          format: text
  - name: done
    kind: decision
    cases:
      - when: true
        next: {complete: true, result: validated}
    otherwise: {complete: true, result: validated}
  - name: exhausted
    kind: gate
    message:
      parts:
        - text: "out of reproduction attempts"
    choices:
      - id: stop
        label: Stop without a fix
        next: {complete: true, result: stopped-without-fix}
  - name: decide
    kind: gate
    evidence:
      repro: {steps: [reproduce]}
    message:
      separator: ""
      parts:
        - text: "could not reproduce: "
        - value: {op: get, value: {op: evidence, name: repro}, keys: [outcome, summary]}
          format: text
    choices:
      - id: retry
        label: Supply information and retry
        feedback_required: true
        next: {step: reproduce}
      - id: stop
        label: Stop without a fix
        next: {complete: true, result: stopped-without-fix}
"""


@pytest.fixture
def f2_workflow(engine: Engine):
    return install_test_workflow(engine, F2_YAML)


def _result(result: str, summary: str, **artifacts) -> str:
    return json.dumps(
        {
            "version": 2,
            "result": result,
            "summary": summary,
            "artifacts": artifacts or {"attempts": "ran it"},
        }
    )


def answer(
    runner: WorkflowRunner,
    engine: Engine,
    task: Task,
    choice_id: str,
    note: str | None = None,
):
    return runner.answer_gate(
        get_task(engine, task.id),
        resolve_task_definition(engine, task),
        expected_seq=waiting_seq(engine, task.id),
        choice_id=choice_id,
        note=note,
    )


async def test_a_declared_negative_result_routes_instead_of_stopping(
    rig, engine, project, tmp_path: Path, f2_workflow
) -> None:
    """`not-reproduced` is an answer, not an absence.

    Format 1 could only say success/failed and had to be told what that meant
    by a route reading `status`. Here the step declares the name, the run
    finishes the attempt `ok`, and the decision sends it to the gate its
    author chose.
    """
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, workflow="f2-wf", slug="f2-negative")
    writer = asyncio.create_task(
        _write_outcome_when_session_prompted(
            supervisor,
            task.id,
            "qa",
            Path(task.clone_path),
            _result("not-reproduced", "no repro on main", attempts="ran the suite"),
        )
    )
    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})
    await writer

    records = list_step_records(engine, task.id)
    assert [(r.step, r.status) for r in records] == [
        ("reproduce", "ok"),
        ("route", "ok"),
        ("decide", "waiting"),
    ]
    # The producing attempt kept its declared result, not a synthetic failure.
    assert records[0].outcome["result"] == "not-reproduced"
    assert records[0].pause is None
    # And the gate is a real question with the evidence it is asking about.
    snapshot = records[-1].outcome
    assert snapshot["version"] == 2
    assert "no repro on main" in snapshot["message"]
    assert [c["id"] for c in snapshot["choices"]] == ["retry", "stop"]
    assert snapshot["evidence"]["repro"] == {"step": "reproduce", "seq": 1}


async def test_a_result_outside_the_contract_pauses_rather_than_routing(
    rig, engine, project, tmp_path: Path, f2_workflow
) -> None:
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, workflow="f2-wf", slug="f2-undeclared")
    writer = asyncio.create_task(
        _write_outcome_when_session_prompted(
            supervisor,
            task.id,
            "qa",
            Path(task.clone_path),
            _result("mostly-reproduced", "sort of", attempts="tried"),
        )
    )
    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})
    await writer

    record = list_step_records(engine, task.id)[-1]
    assert record.step == "reproduce"
    assert record.status == "waiting"
    assert record.outcome is None  # nothing that could later read as a result
    assert record.pause["reason"] == "missing_outcome"
    assert "is not declared by this step" in record.error


async def test_a_missing_required_artifact_is_not_a_result(
    rig, engine, project, tmp_path: Path, f2_workflow
) -> None:
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, workflow="f2-wf", slug="f2-incomplete")
    writer = asyncio.create_task(
        _write_outcome_when_session_prompted(
            supervisor,
            task.id,
            "qa",
            Path(task.clone_path),
            json.dumps(
                {
                    "version": 2,
                    "result": "reproduced",
                    "summary": "it fails",
                    "artifacts": {},
                }
            ),
        )
    )
    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})
    await writer

    record = list_step_records(engine, task.id)[-1]
    assert record.outcome is None
    assert "requires the artifact 'attempts'" in record.error


async def test_the_prompt_names_the_results_the_step_may_declare(
    rig, engine, project, tmp_path: Path, f2_workflow
) -> None:
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, workflow="f2-wf", slug="f2-instruction")
    writer = asyncio.create_task(
        _write_outcome_when_session_prompted(
            supervisor,
            task.id,
            "qa",
            Path(task.clone_path),
            _result("not-reproduced", "nope", attempts="tried"),
        )
    )
    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})
    await writer

    prompt = user_prompts(supervisor, task.id, "qa")[0]
    assert '"version": 2' in prompt
    assert '"reproduced"' in prompt and '"not-reproduced"' in prompt
    assert "required artifacts: attempts (string)" in prompt


async def test_a_consumer_is_handed_the_attempt_it_was_bound_to(
    rig, engine, project, tmp_path: Path, f2_workflow
) -> None:
    """The frozen-evidence property, end to end.

    `fix` renders the *sequence number* of the reproduction it was given. If
    the binding were re-selected on read rather than frozen at entry, this
    would silently be whatever ran most recently.
    """
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, workflow="f2-wf", slug="f2-handoff")
    writer = asyncio.create_task(
        _write_outcome_when_session_prompted(
            supervisor,
            task.id,
            "qa",
            Path(task.clone_path),
            _result("reproduced", "fails on main", attempts="ran the suite"),
        )
    )
    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"complete"})
    await writer

    task_row = get_task(engine, task.id)
    assert task_row.workflow_result == "validated"
    prompt = user_prompts(supervisor, task.id, "coder")[0]
    assert "fix what qa saw in attempt 1: fails on main" in prompt
    fix_record = next(r for r in list_step_records(engine, task.id) if r.step == "fix")
    assert fix_record.evidence == {
        "version": 1,
        "bindings": {"repro": {"step": "reproduce", "seq": 1}},
    }


async def test_answering_a_gate_records_the_choice_and_takes_its_route(
    rig, engine, project, tmp_path: Path, f2_workflow
) -> None:
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, workflow="f2-wf", slug="f2-answer")
    writer = asyncio.create_task(
        _write_outcome_when_session_prompted(
            supervisor,
            task.id,
            "qa",
            Path(task.clone_path),
            _result("not-reproduced", "nope", attempts="tried"),
        )
    )
    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})
    await writer
    gate_seq = waiting_seq(engine, task.id)

    updated = answer(runner, engine, task, "stop")

    assert updated.workflow_status == "complete"
    assert updated.workflow_result == "stopped-without-fix"
    gate = next(r for r in list_step_records(engine, task.id) if r.seq == gate_seq)
    assert gate.status == "ok"
    decision = gate.outcome["decision"]
    assert decision["choice_id"] == "stop"
    assert decision["label"] == "Stop without a fix"
    assert decision["destination"] == {
        "complete": True,
        "result": "stopped-without-fix",
    }
    assert decision["actor"] == "operator"
    # The question is still readable beside the answer.
    assert gate.outcome["message"].startswith("could not reproduce")
    assert [c["id"] for c in gate.outcome["choices"]] == ["retry", "stop"]


async def test_a_gate_answer_is_refused_twice_and_when_stale(
    rig, engine, project, tmp_path: Path, f2_workflow
) -> None:
    """A duplicate submit and a stale tab look identical from the daemon, and
    both must be refused rather than applied to whatever is waiting now."""
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, workflow="f2-wf", slug="f2-replay")
    writer = asyncio.create_task(
        _write_outcome_when_session_prompted(
            supervisor,
            task.id,
            "qa",
            Path(task.clone_path),
            _result("not-reproduced", "nope", attempts="tried"),
        )
    )
    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})
    await writer
    gate_seq = waiting_seq(engine, task.id)

    answer(runner, engine, task, "stop")
    with pytest.raises((WorkflowWaitConflictError, WorkflowNotWaitingError)):
        runner.answer_gate(
            get_task(engine, task.id),
            resolve_task_definition(engine, task),
            expected_seq=gate_seq,
            choice_id="retry",
            note="second thoughts",
        )
    # Exactly one decision, and no second successor for it.
    records = list_step_records(engine, task.id)
    answered = [r for r in records if r.kind == "gate" and (r.outcome or {}).get("decision")]
    assert len(answered) == 1
    assert answered[0].outcome["decision"]["choice_id"] == "stop"
    assert [r.seq for r in records] == sorted(r.seq for r in records)
    assert records[-1].seq == gate_seq  # nothing opened after the completion


async def test_a_choice_the_gate_does_not_offer_is_refused(
    rig, engine, project, tmp_path: Path, f2_workflow
) -> None:
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, workflow="f2-wf", slug="f2-unknown-choice")
    writer = asyncio.create_task(
        _write_outcome_when_session_prompted(
            supervisor,
            task.id,
            "qa",
            Path(task.clone_path),
            _result("not-reproduced", "nope", attempts="tried"),
        )
    )
    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})
    await writer

    with pytest.raises(WorkflowGateChoiceError, match="not one of this gate's choices"):
        answer(runner, engine, task, "proceed-anyway")
    # Feedback the choice declares as required cannot be skipped.
    with pytest.raises(WorkflowGateChoiceError, match="requires feedback"):
        answer(runner, engine, task, "retry", "   ")
    # Nothing advanced.
    assert get_task(engine, task.id).workflow_status == "waiting"
    assert list_step_records(engine, task.id)[-1].status == "waiting"


async def test_a_retry_choice_spends_a_visit_and_cannot_refill_the_budget(
    rig, engine, project, tmp_path: Path, f2_workflow
) -> None:
    """A human edge is an edge.

    Three reproduction attempts is a lifetime budget for the run, so the third
    retry lands on the exhaustion gate — which offers only stopping — rather
    than on a fourth attempt.
    """
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, workflow="f2-wf", slug="f2-budget")
    clone = Path(task.clone_path)
    negative = _result("not-reproduced", "nope", attempts="tried")

    writer = asyncio.create_task(
        _write_outcome_when_session_prompted(supervisor, task.id, "qa", clone, negative)
    )
    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})
    await writer

    for attempt in (2, 3):
        writer = asyncio.create_task(
            _write_outcome_when_session_prompted(
                supervisor, task.id, "qa", clone, negative, prompt_count=attempt
            )
        )
        answer(runner, engine, task, "retry", f"try harder ({attempt})")
        await wait_for_run(engine, task.id, {"waiting"})
        await writer

    # Budget spent: three `reproduce` attempts, and the next retry is refused
    # a fourth.
    assert len([r for r in list_step_records(engine, task.id) if r.step == "reproduce"]) == 3
    answer(runner, engine, task, "retry", "one more?")
    await wait_for_run(engine, task.id, {"waiting"})
    records = list_step_records(engine, task.id)
    assert len([r for r in records if r.step == "reproduce"]) == 3
    assert records[-1].step == "exhausted"
    assert [c["id"] for c in records[-1].outcome["choices"]] == ["stop"]

    answer(runner, engine, task, "stop")
    final = get_task(engine, task.id)
    assert (final.workflow_status, final.workflow_result) == (
        "complete",
        "stopped-without-fix",
    )


async def test_an_unanswered_gate_is_the_same_question_after_a_restart(
    rig, engine, project, tmp_path: Path, f2_workflow
) -> None:
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, workflow="f2-wf", slug="f2-restart-gate")
    writer = asyncio.create_task(
        _write_outcome_when_session_prompted(
            supervisor,
            task.id,
            "qa",
            Path(task.clone_path),
            _result("not-reproduced", "nope on main", attempts="tried"),
        )
    )
    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})
    await writer
    before = list_step_records(engine, task.id)[-1]

    await runner.shutdown()
    await supervisor.shutdown()
    runner2, supervisor2, tracker2, _hub2 = _restart_rig(engine, tmp_path)
    try:
        await _resume_recorded_sessions(
            engine, supervisor2, tracker2, get_task(engine, task.id)
        )
        _recover(runner2, engine, get_task(engine, task.id))
        await wait_for_run(engine, task.id, {"waiting"})

        after = list_step_records(engine, task.id)[-1]
        # The same row, the same question, no second gate attempt.
        assert after.seq == before.seq
        assert after.outcome == before.outcome
        assert after.outcome.get("decision") is None

        answer(runner2, engine, task, "stop")
        final = get_task(engine, task.id)
        assert (final.workflow_status, final.workflow_result) == (
            "complete",
            "stopped-without-fix",
        )
    finally:
        await runner2.shutdown()
        await supervisor2.shutdown()


async def test_an_answer_committed_before_a_crash_is_not_lost_or_replayed(
    rig, engine, project, tmp_path: Path, f2_workflow
) -> None:
    """The exact interruption the in-memory future could not survive.

    The decision commits, and *then* the process dies before anything is
    scheduled. Recovery must find the successor the answer already opened —
    not re-arm the gate, and not open a second attempt.
    """
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, workflow="f2-wf", slug="f2-crash-after-commit")
    writer = asyncio.create_task(
        _write_outcome_when_session_prompted(
            supervisor,
            task.id,
            "qa",
            Path(task.clone_path),
            _result("not-reproduced", "nope", attempts="tried"),
        )
    )
    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})
    await writer
    gate_seq = waiting_seq(engine, task.id)

    # Commit the answer directly, exactly as `answer_gate` does, and never let
    # the parked run observe it: this is the post-commit / pre-schedule window.
    definition = resolve_task_definition(engine, task).definition
    target = definition.step_named("reproduce")
    resolve_gate(
        engine,
        task.id,
        gate_seq,
        choice_id="retry",
        feedback="here is a hint",
        successor=(target.name, target.kind, target.session, None),
        terminal_result=None,
    )
    await runner.shutdown()
    await supervisor.shutdown()

    records = list_step_records(engine, task.id)
    assert records[-2].seq == gate_seq
    assert records[-2].outcome["decision"]["choice_id"] == "retry"
    assert records[-2].outcome["decision"]["feedback"] == "here is a hint"
    assert (records[-1].step, records[-1].status) == ("reproduce", "running")

    runner2, supervisor2, tracker2, _hub2 = _restart_rig(engine, tmp_path)
    try:
        await _resume_recorded_sessions(
            engine, supervisor2, tracker2, get_task(engine, task.id)
        )
        writer = asyncio.create_task(
            _write_outcome_when_session_prompted(
                supervisor2,
                task.id,
                "qa",
                Path(task.clone_path),
                _result("not-reproduced", "still nope", attempts="tried again"),
                prompt_count=1,
            )
        )
        _recover(runner2, engine, get_task(engine, task.id))
        await wait_for_run(engine, task.id, {"waiting"})
        await writer

        after = list_step_records(engine, task.id)
        # The answered gate was neither re-armed nor answered twice, and the
        # successor it opened was re-driven rather than duplicated.
        assert len([r for r in after if r.seq == gate_seq]) == 1
        assert len([r for r in after if r.step == "reproduce"]) == 2
        assert after[gate_seq - 1].outcome["decision"]["choice_id"] == "retry"
    finally:
        await runner2.shutdown()
        await supervisor2.shutdown()


async def test_missing_required_evidence_pauses_instead_of_prompting(
    engine, project, tmp_path: Path
) -> None:
    """A required handoff that does not exist stops the attempt where it is.

    The attempt is recorded — with the reason — rather than skipped, because
    an attempt that vanished would take the explanation with it.
    """
    from ompire_daemon.workflow_definitions import bindings_from_document
    from ompire_daemon.workflows import missing_required_evidence

    revision = install_test_workflow(engine, F2_YAML)
    step = revision.definition.step_named("fix")
    # `fix` requires a reproduction. With no history there is nothing to bind.
    assert missing_required_evidence(step, bindings_from_document(None)) == ("repro",)
    bound = bindings_from_document(
        {"version": 1, "bindings": {"repro": {"step": "reproduce", "seq": 2}}}
    )
    assert missing_required_evidence(step, bound) == ()
    # An optional selector that matched nothing is not missing.
    decide = revision.definition.step_named("decide")
    assert decide.evidence[0].required is True
    assert missing_required_evidence(decide, bindings_from_document(
        {"version": 1, "bindings": {"repro": None}}
    )) == ("repro",)


# --- format 3: review as a declared step --------------------------------------


REVIEW_YAML = """
format: 3
name: reviewed
sessions: [main]
primary: main
steps:
  - name: work
    kind: agent
    session: main
    outcome: null
    prompt: {parts: [{text: "do it"}]}

  - name: check
    kind: review
    max_visits: 2
    on_exhausted: {step: give-up}
    evidence:
      work: {steps: [work], with_outcome: false}

  - name: route
    kind: decision
    evidence:
      verdict: {steps: [check]}
    cases:
      - when:
          op: eq
          left: {op: get, value: {op: evidence, name: verdict}, keys: [outcome, result]}
          right: {op: literal, value: "approved"}
        next: {complete: true, result: approved}
      - when:
          op: all
          of:
            - op: eq
              left:
                op: get
                value: {op: evidence, name: verdict}
                keys: [outcome, result]
              right: {op: literal, value: "comments"}
            # A correction runs only against the reviewer's *whole* report.
            - op: eq
              left:
                op: get
                value: {op: evidence, name: verdict}
                keys: [outcome, findings_state]
              right: {op: literal, value: "complete"}
        next: {step: correct}
    otherwise: {step: give-up}

  - name: correct
    kind: agent
    session: main
    max_visits: 2
    on_exhausted: {step: give-up}
    outcome: null
    evidence:
      verdict: {steps: [check]}
    prompt:
      separator: ""
      parts:
        - text: "The reviewer said:\\n"
        - value:
            op: get
            value: {op: evidence, name: verdict}
            keys: [outcome, findings]
          format: text

  - name: back-to-review
    kind: decision
    cases:
      - when: true
        next: {step: check}
    otherwise: {step: give-up}

  - name: give-up
    kind: gate
    evidence:
      verdict: {steps: [check], required: false}
    message: {parts: [{text: "Review did not approve."}]}
    choices:
      - id: stop
        label: Stop
        next: {complete: true, result: stopped-unapproved}
"""


async def _run_reviewed(rig, engine, tmp_path, verdicts):
    runner, _supervisor, _tracker, _hub, _scenario = rig
    install_test_workflow(engine, REVIEW_YAML)
    reviews = _StubReviews(engine, verdicts)
    runner.set_operations(reviews, None)
    task = _make_task(engine, tmp_path, workflow="reviewed")
    _start(runner, engine, task)
    return task, reviews


async def test_an_approved_review_routes_on_what_was_recorded(
    rig, engine: Engine, tmp_path: Path
) -> None:
    task, _reviews = await _run_reviewed(
        rig, engine, tmp_path, [{"outcome": "approved", "findings": ""}]
    )
    finished = await wait_for_run(engine, task.id, {"complete", "failed"})
    assert finished.workflow_status == "complete"
    assert finished.workflow_result == "approved"
    records = list_step_records(engine, task.id)
    check = next(r for r in records if r.step == "check")
    assert check.kind == "review"
    assert check.outcome["result"] == "approved"
    assert check.outcome["candidate_id"] == "cand-1"
    assert check.outcome["iteration_seq"] == 1


async def test_comments_reach_a_declared_correction_and_not_a_hidden_turn(
    rig, engine: Engine, tmp_path: Path
) -> None:
    """The findings go to the step the author wrote, as its prompt.

    Nothing pushes them into a session behind the run's back: the correcting
    step is an ordinary agent step reading ordinary evidence, which is what
    makes the loop visible in the flow and bounded by its own budget.
    """
    _runner, supervisor, _tracker, _hub, _scenario = rig
    task, reviews = await _run_reviewed(
        rig,
        engine,
        tmp_path,
        [
            {"outcome": "comments", "findings": "> fix the thing"},
            {"outcome": "approved", "findings": ""},
        ],
    )
    finished = await wait_for_run(engine, task.id, {"complete", "failed"})
    assert finished.workflow_result == "approved"
    prompts = user_prompts(supervisor, task.id, "main")
    assert any("> fix the thing" in text for text in prompts)
    # One correction turn, sent by the workflow — not one from the reviewer
    # and another from the step.
    assert sum("> fix the thing" in text for text in prompts) == 1
    assert reviews.started == [2, 6]


@pytest.mark.parametrize("verdict", ["aborted", "error", "interrupted"])
async def test_a_review_that_did_not_approve_never_reads_as_approval(
    rig, engine: Engine, tmp_path: Path, verdict: str
) -> None:
    task, _reviews = await _run_reviewed(
        rig, engine, tmp_path, [{"outcome": verdict}]
    )
    waiting = await wait_for_run(engine, task.id, {"waiting", "failed", "complete"})
    assert waiting.workflow_status == "waiting"
    assert waiting.workflow_step == "give-up"
    check = next(r for r in list_step_records(engine, task.id) if r.step == "check")
    assert check.outcome["result"] == verdict
    assert check.outcome["findings"] is None


async def test_an_unavailable_reviewer_pauses_instead_of_inventing_a_verdict(
    rig, engine: Engine, tmp_path: Path
) -> None:
    from ompire_daemon.review import ReviewContentError

    task, _reviews = await _run_reviewed(
        rig,
        engine,
        tmp_path,
        [ReviewContentError("there is nothing to review")],
    )
    waiting = await wait_for_run(engine, task.id, {"waiting", "failed", "complete"})
    assert waiting.workflow_status == "waiting"
    record = list_step_records(engine, task.id)[-1]
    assert record.step == "check"
    assert record.outcome is None
    assert record.pause["reason"] == "review_unavailable"
    assert "nothing to review" in record.pause["message"]


async def test_a_recorded_verdict_is_consumed_once_across_a_restart(
    rig, engine: Engine, tmp_path: Path
) -> None:
    """A restart between the verdict and the step's completion re-reads the
    verdict; it never runs a second review of a workspace that moved on."""
    runner, _supervisor, _tracker, _hub, _scenario = rig
    install_test_workflow(engine, REVIEW_YAML)
    reviews = _StubReviews(engine, [{"outcome": "approved", "findings": ""}])
    runner.set_operations(reviews, None)
    task = _make_task(engine, tmp_path, workflow="reviewed")
    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"complete", "failed"})

    # Re-drive the same recorded attempt with an empty verdict queue: a second
    # review would raise IndexError, so reaching the same result proves the
    # recorded one was reused.
    from ompire_daemon.registry.reviews import iteration_for_step

    check = next(r for r in list_step_records(engine, task.id) if r.step == "check")
    assert iteration_for_step(engine, task.id, check.seq) is not None
    assert reviews._verdicts == []
    assert reviews.started == [2]


async def test_capture_step_retains_the_declared_producer_output(
    rig, engine, project, tmp_path: Path
) -> None:
    revision = install_test_workflow(
        engine,
        """
format: 4
name: capture-flow
sessions: [main]
primary: main
steps:
  - name: prepare
    kind: command
    argv: ["true"]
    idempotent: true
  - name: capture
    kind: capture
    evidence:
      producer:
        steps: [prepare]
        required: true
        with_outcome: true
    producer: producer
    paths:
      - parts: [{text: epics/example/PLAN.md}]
    allowlist: [epics]
    next: {step: decide}
  - name: decide
    kind: gate
    evidence:
      captured:
        steps: [capture]
        required: true
    result: {evidence: captured}
    message:
      parts: [{text: inspect result}]
    choices:
      - id: finish
        label: Finish with accepted result
        requires_result_acceptance: true
        next: {complete: true, result: accepted-result}
      - id: stop
        label: Stop
        next: {complete: true, result: stopped}
""",
    )
    runner, _supervisor, _tracker, hub, _scenario = rig
    results = ResultManager(
        runner._config, engine, hub, WorkspaceGuard()
    )
    runner.set_results(results)
    task = _make_task(
        engine, tmp_path, revision.definition.name, slug="capture-flow-task"
    )
    plan = Path(task.clone_path) / "epics" / "example" / "PLAN.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("# Plan\n")

    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})

    records = list_step_records(engine, task.id)
    result = get_result(
        engine,
        records[1].outcome["artifacts"]["result_id"],
    )
    assert records[1].kind == "capture"
    assert result.workflow_seq == records[1].seq
    assert result.manifest["provenance"]["producing_attempt"] == records[0].seq
    assert result.files[0].path == "epics/example/PLAN.md"
    gate = records[2]
    assert gate.outcome["result"] == {
        "evidence": "captured",
        "capture": {"step": "capture", "seq": records[1].seq},
        "result_id": result.id,
        "manifest_id": result.manifest_id,
    }
    await results.capture(
        task.id, paths=["epics/example/PLAN.md"], request_id="manual-result"
    )
    await asyncio.gather(*list(results._jobs))
    unrelated = next(
        entry for entry in list_results(engine, task.id) if entry.request_id == "manual-result"
    )
    results.accept(unrelated, expected_manifest_id=unrelated.manifest_id)
    with pytest.raises(WorkflowGateChoiceError, match="has not been accepted"):
        answer(runner, engine, task, "finish")
    results.accept(result, expected_manifest_id=result.manifest_id)
    answer(runner, engine, task, "finish")
    assert get_task(engine, task.id).workflow_result == "accepted-result"


@pytest.mark.parametrize(
    ("outcome", "files"),
    [
        (
            _result("epic-proposed", "epic ready", root="epics/example"),
            {"epics/example/EPIC.md": "# Epic\n"},
        ),
        (
            _result("change-proposed", "change ready", root="changes/example"),
            {
                "changes/example/SPEC.md": "# Spec\n",
                "changes/example/PLAN.md": "# Plan\n",
            },
        ),
        (
            _result(
                "epic-change-proposed",
                "child ready",
                root="epics/example/changes/child",
                epic_root="epics/example",
            ),
            {
                "epics/example/EPIC.md": "# Epic\n",
                "epics/example/changes/child/SPEC.md": "# Spec\n",
                "epics/example/changes/child/PLAN.md": "# Plan\n",
            },
        ),
    ],
)
async def test_packaged_planning_captures_each_declared_proposal_bundle(
    rig, engine, project, tmp_path: Path, outcome: str, files: dict[str, str]
) -> None:
    runner, supervisor, _tracker, hub, _scenario = rig
    runner.set_results(
        ResultManager(
            runner._config, engine, hub, WorkspaceGuard()
        )
    )
    task = _make_task(
        engine,
        tmp_path,
        workflow="planning",
        slug=f"planning-{json.loads(outcome)['result']}",
    )
    for relative_path, content in files.items():
        path = Path(task.clone_path) / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    writer = asyncio.create_task(
        _write_outcome_when_session_prompted(
            supervisor, task.id, "planner", Path(task.clone_path), outcome
        )
    )
    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})
    await writer

    records = list_step_records(engine, task.id)
    capture = records[-2]
    result = get_result(engine, capture.outcome["artifacts"]["result_id"])
    assert capture.step.startswith("capture-")
    assert {entry.path for entry in result.files} == set(files)
    assert records[-1].step == "result-gate"
    assert records[-1].outcome["result"]["result_id"] == result.id


async def test_packaged_planning_unable_result_routes_feedback_to_a_new_turn(
    rig, engine, project, tmp_path: Path
) -> None:
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, workflow="planning", slug="planning-unable")
    unable = _result("unable", "cannot proceed", reason="missing project context")
    writer = asyncio.create_task(
        _write_outcome_when_session_prompted(
            supervisor, task.id, "planner", Path(task.clone_path), unable
        )
    )
    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})
    await writer

    assert list_step_records(engine, task.id)[-1].step == "unable-gate"
    writer = asyncio.create_task(
        _write_outcome_when_session_prompted(
            supervisor, task.id, "planner", Path(task.clone_path), unable, prompt_count=2
        )
    )
    answer(runner, engine, task, "request-changes", "use the existing epic")
    await wait_for_run(engine, task.id, {"waiting"})
    await writer
    retry = [r for r in list_step_records(engine, task.id) if r.step == "propose"][-1]
    assert retry.evidence["bindings"]["feedback"]["step"] == "unable-gate"


async def test_packaged_planning_stops_at_its_declared_revision_budget(
    rig, engine, project, tmp_path: Path
) -> None:
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, workflow="planning", slug="planning-budget")
    unable = _result("unable", "cannot proceed", reason="missing project context")
    writer = asyncio.create_task(
        _write_outcome_when_session_prompted(
            supervisor, task.id, "planner", Path(task.clone_path), unable
        )
    )
    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})
    await writer

    for prompt_count in (2, 3):
        writer = asyncio.create_task(
            _write_outcome_when_session_prompted(
                supervisor,
                task.id,
                "planner",
                Path(task.clone_path),
                unable,
                prompt_count=prompt_count,
            )
        )
        answer(runner, engine, task, "request-changes", "try again")
        await wait_for_run(engine, task.id, {"waiting"})
        await writer
    answer(runner, engine, task, "request-changes", "one more")
    await wait_for_run(engine, task.id, {"waiting"})
    exhausted = list_step_records(engine, task.id)[-1]
    assert exhausted.step == "planning-exhausted"
    assert [choice["id"] for choice in exhausted.outcome["choices"]] == ["stop"]
