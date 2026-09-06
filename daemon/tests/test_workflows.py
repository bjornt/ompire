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
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from ompire_daemon import agent as agent_module
from ompire_daemon.agent import AgentSupervisor
from ompire_daemon.config import Config
from ompire_daemon.db import db_path_for, ensure_db_dir, make_engine
from ompire_daemon.events import EventHub
from ompire_daemon.migrate import upgrade_head
from ompire_daemon.registry.projects import create_project
from ompire_daemon.registry.sessions import get_session
from ompire_daemon.registry.tasks import Task, create_task, get_task
from ompire_daemon.registry.workflows import (
    WorkflowWaitConflictError,
    list_step_records,
)
from ompire_daemon.sessions import SessionTracker
from ompire_daemon.taskdefinition import (
    TaskDefinitionUnavailableError,
    resolve_task_definition,
)
from ompire_daemon.workflows import (
    COMPLETE,
    UnknownWorkflowNameError,
    WorkflowNotWaitingError,
    WorkflowRunner,
    catalog_names,
    current_revision,
)
from tests.conftest import (
    TEST_ROLES,
    fake_argv_builder,
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
    workflow: str = "single-step",
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
    return WorkflowRunner(engine, config, hub, supervisor, tracker), supervisor, tracker, hub


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


# --- the catalog and the pinned revision (ADR-0028) ---------------------------


def test_the_packaged_definitions_are_the_catalog() -> None:
    assert catalog_names() == ("bugfix", "single-step")
    revision = current_revision("single-step")
    assert revision.revision.startswith("sha256:")
    assert revision.definition.primary == "main"
    with pytest.raises(UnknownWorkflowNameError):
        current_revision("no-such-workflow")


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
    workflows_module.reset_catalog()
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
    workflows_module.reset_catalog()
    with pytest.raises(workflows_module.PackagedWorkflowError, match="missing"):
        workflows_module.load_packaged_workflows()


def test_launch_rejects_an_uninstalled_workflow(engine: Engine, project) -> None:
    """The installed catalog is the only source of valid workflow names, and a
    launch names one directly (ADR-0026)."""
    from ompire_daemon.launch import LaunchInputError, LaunchRequest, resolve_launch

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

    A task is accepted under one definition; the installed definition is then
    edited. The run still sends the prompt it was accepted with, still resolves
    to its own revision, and the *new* revision is what a new launch would get.
    Both revisions stay retained and readable at once.
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
    assert current_revision("editable").revision == edited.revision

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

    # And the task survives its workflow *name* leaving the catalog entirely —
    # a later release that drops a definition. Nothing resolves by name.
    from ompire_daemon.workflows import uninstall_definition

    uninstall_definition("editable")
    assert "editable" not in catalog_names()
    clear_cache()
    still = resolve_task_definition(engine, get_task(engine, task.id))
    assert still.revision == original.revision
    assert still.definition.primary == "main"


def test_a_task_whose_revision_is_unavailable_is_refused_not_substituted(
    engine: Engine, project, tmp_path: Path
) -> None:
    """A damaged or absent revision blocks *this* task and says why. It never
    falls back to the catalog's current definition of the same name."""
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
    from ompire_daemon.registry.tasks import list_tasks, task_payload

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
    from ompire_daemon.registry.tasks import create_task as _create

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


def test_a_retained_revision_is_readable_and_a_damaged_one_is_classified(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    """Read-only inspection by content identity: the operator can see exactly
    what a task accepted, and an unreadable document is reported rather than
    executed to answer a read."""
    from sqlalchemy import text as sa_text

    from ompire_daemon.registry.workflow_definitions import clear_cache

    revision = current_revision("bugfix").revision
    response = client.get(f"/api/workflows/revisions/{revision}", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["name"] == "bugfix"
    assert body["format"] == 1
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


# --- the bugfix built-in (design D-1/D-2/D-6) ---------------------------------


def _bugfix_outcome(status: str, summary: str, **artifacts: str) -> str:
    outcome: dict = {"version": 1, "status": status, "summary": summary}
    if artifacts:
        outcome["artifacts"] = artifacts
    return json.dumps(outcome)


async def test_bugfix_happy_path_script_validates(rig, engine, project, tmp_path: Path) -> None:
    """reproduce → triage → fix → script validation → complete, with the
    agent-validation step skipped and the escalate gate never reached."""
    runner, supervisor, tracker, _hub, _scenario = rig
    task = _make_task(
        engine, tmp_path, workflow="bugfix", preamble="PRE", prompt="bug: off by one"
    )

    async def drive() -> None:
        await _write_outcome_when_session_prompted(
            supervisor, task.id, "reproducer", Path(task.clone_path),
            _bugfix_outcome(
                "success", "reproduced: index error on empty input",
                repro_command="bash .ompire/repro.sh",
                expected_behavior="empty list", observed_behavior="IndexError",
            ),
        )
        await _write_outcome_when_session_prompted(
            supervisor, task.id, "coder", Path(task.clone_path),
            _bugfix_outcome("success", "guarded the empty case"),
        )

    driver = asyncio.create_task(drive())
    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"complete"})
    await driver

    records = list_step_records(engine, task.id)
    assert [(r.step, r.kind, r.status) for r in records] == [
        ("reproduce", "agent", "ok"),
        ("triage", "decision", "ok"),
        ("fix", "agent", "ok"),
        ("route-validate", "decision", "ok"),
        ("validate-script", "command", "ok"),
        ("validate-agent", "agent", "ok"),
        ("check", "decision", "ok"),
    ]
    assert records[1].outcome == {"route": "fix"}
    assert records[3].outcome == {"route": "validate-script"}
    assert records[4].outcome is not None and records[4].outcome["exit_code"] == 0
    # The agent validation was deliberately inert: no prompt, no outcome, no
    # pause, and the coder's self-report was NOT read as its outcome.
    assert records[5].outcome is None and records[5].prompted_at is None
    assert records[5].pause is None
    assert "deliberately not prompted" in (records[5].error or "")
    assert records[6].outcome == {"route": COMPLETE}

    repro_prompt = user_prompts(supervisor, task.id, "reproducer")[0]
    assert repro_prompt.startswith("PRE\n\n")
    assert "bug: off by one" in repro_prompt and ".ompire/repro.sh" in repro_prompt
    assert "do not commit" in repro_prompt
    fix_prompt = user_prompts(supervisor, task.id, "coder")[0]
    assert "reproduced: index error on empty input" in fix_prompt
    assert "bash .ompire/repro.sh" in fix_prompt
    # The ship flow squashes the branch tip's tree: the coder must commit.
    assert "Commit your change" in fix_prompt and "never push" in fix_prompt
    # No engine-reserved session was ever spawned.
    assert supervisor.get(task.id, "judge") is None
    assert tracker.get(task.id, "coder") is not None


async def test_bugfix_unreproducible_bug_escalates_before_fix(
    rig, engine, project, tmp_path: Path
) -> None:
    """A declared `failed` reproduction is a real result and follows the
    definition's own route — it is not missing evidence, so it never pauses."""
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, workflow="bugfix", prompt="bug: flaky test")

    driver = asyncio.create_task(
        _write_outcome_when_session_prompted(
            supervisor, task.id, "reproducer", Path(task.clone_path),
            _bugfix_outcome("failed", "cannot reproduce: no failing input found"),
        )
    )
    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})
    await driver

    records = list_step_records(engine, task.id)
    assert [r.step for r in records] == ["reproduce", "triage", "escalate"]
    assert records[1].outcome == {"route": "escalate"}
    gate = records[2]
    assert gate.kind == "gate" and gate.status == "waiting"
    assert gate.pause is None
    assert "could not be reproduced" in gate.outcome["message"]
    # The coder was never spawned.
    assert supervisor.get(task.id, "coder") is None

    resume(runner, engine, task.id)
    await wait_for_run(engine, task.id, {"complete"})


async def test_bugfix_rejection_loops_then_escalates(
    rig, engine, project, tmp_path: Path, fake_workshop_cli: Path
) -> None:
    """The reproducer script keeps failing: fix is re-prompted with the
    validation report, and the run escalates after the third attempt."""
    fake_workshop_cli.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *"config get ask.timeout"*) echo 0 ;;\n'
        '  *"--mode rpc-ui"*) exit 1 ;;\n'
        '  *repro.sh*) echo "still broken"; exit 1 ;;\n'
        '  *) exit 0 ;;\n'
        "esac\n"
    )
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, workflow="bugfix", prompt="bug: crash")

    async def drive() -> None:
        await _write_outcome_when_session_prompted(
            supervisor, task.id, "reproducer", Path(task.clone_path),
            _bugfix_outcome("success", "reproduced", repro_command="bash .ompire/repro.sh"),
        )
        for attempt in (1, 2, 3):
            await _write_outcome_when_session_prompted(
                supervisor, task.id, "coder", Path(task.clone_path),
                _bugfix_outcome("success", f"fix attempt {attempt}"),
                prompt_count=attempt,
            )

    driver = asyncio.create_task(drive())
    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})
    await driver

    records = list_step_records(engine, task.id)
    fixes = [r for r in records if r.step == "fix"]
    assert len(fixes) == 3
    checks = [r for r in records if r.step == "check"]
    assert [r.outcome for r in checks] == [
        {"route": "fix"}, {"route": "fix"}, {"route": "fix"},
    ]
    # The third `check` still routes to `fix`; the *engine* stops it, because
    # the bound is counted before an attempt opens rather than trusted to a
    # route predicate.
    gate = records[-1]
    assert gate.step == "escalate" and gate.kind == "gate" and gate.status == "waiting"
    assert "3 times" in gate.outcome["message"]
    assert "still broken" in gate.outcome["message"]

    coder_prompts = user_prompts(supervisor, task.id, "coder")
    assert len(coder_prompts) == 3
    assert "did NOT validate" not in coder_prompts[0]
    for prompt in coder_prompts[1:]:
        assert "did NOT validate" in prompt
        assert "still broken" in prompt

    resume(runner, engine, task.id)
    await wait_for_run(engine, task.id, {"complete"})


async def test_bugfix_agent_validation_when_no_script(rig, engine, project, tmp_path: Path) -> None:
    """No repro_command artifact: validation is a turn on the reproducer
    session (which keeps its reproduction context), not the command step."""
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, workflow="bugfix", prompt="bug: colors wrong")

    async def drive() -> None:
        await _write_outcome_when_session_prompted(
            supervisor, task.id, "reproducer", Path(task.clone_path),
            _bugfix_outcome("success", "reproduced visually", expected_behavior="blue"),
        )
        await _write_outcome_when_session_prompted(
            supervisor, task.id, "coder", Path(task.clone_path),
            _bugfix_outcome("success", "fixed the palette"),
        )
        await _write_outcome_when_session_prompted(
            supervisor, task.id, "reproducer", Path(task.clone_path),
            _bugfix_outcome("success", "verified: now blue"),
            prompt_count=2,
        )

    driver = asyncio.create_task(drive())
    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"complete"})
    await driver

    records = list_step_records(engine, task.id)
    assert [r.step for r in records] == [
        "reproduce", "triage", "fix", "route-validate", "validate-agent", "check",
    ]
    assert records[3].outcome == {"route": "validate-agent"}
    assert records[4].outcome is not None
    assert records[4].outcome["summary"] == "verified: now blue"
    assert records[5].outcome == {"route": COMPLETE}
    validate_prompt = user_prompts(supervisor, task.id, "reproducer")[1]
    assert "Validate the fix" in validate_prompt


async def test_bugfix_pauses_on_a_missing_reproduce_outcome(
    rig, engine, project, tmp_path: Path
) -> None:
    """The reproducer idles without an outcome file. Nothing classifies the
    step for it: the run stops with the reason, and the coder is never
    spawned on evidence that does not exist."""
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, workflow="bugfix", prompt="bug: crash")

    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})

    records = list_step_records(engine, task.id)
    assert len(records) == 1
    reproduce = records[0]
    assert reproduce.step == "reproduce" and reproduce.status == "waiting"
    assert reproduce.outcome is None
    assert reproduce.pause["reason"] == "missing_outcome"
    assert supervisor.get(task.id, "coder") is None
    assert supervisor.get(task.id, "judge") is None

    # Retrying gives the reproducer another attempt in its own session.
    driver = asyncio.create_task(
        _write_outcome_when_session_prompted(
            supervisor, task.id, "reproducer", Path(task.clone_path),
            _bugfix_outcome("failed", "still cannot reproduce"),
            prompt_count=2,
        )
    )
    retry(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})
    await driver

    records = list_step_records(engine, task.id)
    assert [(r.step, r.status) for r in records] == [
        ("reproduce", "failed"),
        ("reproduce", "ok"),
        ("triage", "ok"),
        ("escalate", "waiting"),
    ]


async def test_bugfix_restart_mid_loop_nudges_and_continues(
    rig, engine, project, tmp_path: Path, fake_workshop_cli: Path
) -> None:
    """Restart with a fix turn in flight: the coder is resumed and nudged, the
    same attempt is re-driven rather than replaced, and the run completes."""
    fake_workshop_cli.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *"config get ask.timeout"*) echo 0 ;;\n'
        '  *"--mode rpc-ui"*) exit 1 ;;\n'
        '  *repro.sh*) exit 1 ;;\n'  # first validation rejects
        '  *) exit 0 ;;\n'
        "esac\n"
    )
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, workflow="bugfix", prompt="bug: crash")

    async def drive_first_half() -> None:
        await _write_outcome_when_session_prompted(
            supervisor, task.id, "reproducer", Path(task.clone_path),
            _bugfix_outcome("success", "reproduced", repro_command="bash .ompire/repro.sh"),
        )
        await _write_outcome_when_session_prompted(
            supervisor, task.id, "coder", Path(task.clone_path),
            _bugfix_outcome("success", "first fix"),
        )

    driver = asyncio.create_task(drive_first_half())
    _start(runner, engine, task)
    # Wait until the loop is back at fix #2 with its prompt durably sent.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        records = list_step_records(engine, task.id)
        fixes = [r for r in records if r.step == "fix"]
        if len(fixes) == 2 and fixes[-1].prompted_at is not None:
            break
        await asyncio.sleep(0.02)
    else:
        raise RuntimeError("second fix prompt never sent")
    await driver

    await runner.shutdown()
    await supervisor.shutdown()
    # After the restart the reproducer script passes (the "fix" worked).
    fake_workshop_cli.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *"config get ask.timeout"*) echo 0 ;;\n'
        '  *"--mode rpc-ui"*) exit 1 ;;\n'
        '  *) exit 0 ;;\n'
        "esac\n"
    )
    runner2, supervisor2, tracker2, _hub2 = _restart_rig(engine, tmp_path)
    await _resume_recorded_sessions(engine, supervisor2, tracker2, task)

    async def drive_second_half() -> None:
        # The nudge re-prompts the coder; the fix outcome lands mid-turn.
        await _write_outcome_when_session_prompted(
            supervisor2, task.id, "coder", Path(task.clone_path),
            _bugfix_outcome("success", "second fix"),
        )

    driver2 = asyncio.create_task(drive_second_half())
    _recover(runner2, engine, get_task(engine, task.id))
    await wait_for_run(engine, task.id, {"complete"})
    await driver2

    coder_prompts = user_prompts(supervisor2, task.id, "coder")
    assert coder_prompts[0].startswith("The daemon restarted")
    assert "outcome.json" in coder_prompts[0]  # outcome-bearing nudge
    records = list_step_records(engine, task.id)
    fixes = [r for r in records if r.step == "fix"]
    # Two work attempts, not three: the restart re-drove the open one.
    assert [r.status for r in fixes] == ["ok", "ok"]
    assert records[-1].outcome == {"route": COMPLETE}
    await runner2.shutdown()
    await supervisor2.shutdown()


async def test_bugfix_escalate_gate_survives_restart(
    rig, engine, project, tmp_path: Path
) -> None:
    """A run parked at the escalate gate re-arms after a restart with the same
    message and resumes to completion."""
    runner, supervisor, _tracker, _hub, _scenario = rig
    task = _make_task(engine, tmp_path, workflow="bugfix", prompt="bug: flaky")

    driver = asyncio.create_task(
        _write_outcome_when_session_prompted(
            supervisor, task.id, "reproducer", Path(task.clone_path),
            _bugfix_outcome("failed", "not reproducible"),
        )
    )
    _start(runner, engine, task)
    await wait_for_run(engine, task.id, {"waiting"})
    await driver

    await runner.shutdown()
    await supervisor.shutdown()
    runner2, supervisor2, tracker2, _hub2 = _restart_rig(engine, tmp_path)
    await _resume_recorded_sessions(engine, supervisor2, tracker2, task)
    _recover(runner2, engine, get_task(engine, task.id))
    await asyncio.sleep(0.3)

    assert get_task(engine, task.id).workflow_status == "waiting"
    gates = [r for r in list_step_records(engine, task.id) if r.kind == "gate"]
    assert len(gates) == 1 and "could not be reproduced" in gates[0].outcome["message"]

    resume(runner2, engine, task.id)
    await wait_for_run(engine, task.id, {"complete"})
    await runner2.shutdown()
    await supervisor2.shutdown()


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
    from ompire_daemon.registry.model_profiles import RoleBinding

    return {
        role: RoleBinding(model=pair["model"], thinking=pair["thinking"])
        for role, pair in roles.items()
    }
