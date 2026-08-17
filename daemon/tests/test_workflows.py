"""Workflow-engine tests: the runner driving `AgentSupervisor` with the fake
omp (same monkeypatch pattern as test_sessions.py), fake workshop CLI for
command steps, and test-registered workflows for multi-step coverage.

Covers (tasks.md 2.7): single-step parity (prompt bytes, empty-prompt idle),
outcome written/missing/malformed, command exit codes and infra failure,
decision routing + escalation gate, gate resume, and a multi-step run with
two named sessions.
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
from ompire_daemon.db import db_path_for, make_engine
from ompire_daemon.events import EventHub
from ompire_daemon.migrate import upgrade_head
from ompire_daemon.registry.projects import create_project
from ompire_daemon.registry.tasks import Task, create_task, get_task
from ompire_daemon.registry.templates import (
    Template,
    UnknownWorkflowError,
    create_template,
)
from ompire_daemon.registry.workflows import list_step_records
from ompire_daemon.sessions import SessionTracker
from ompire_daemon.workflows import (
    AgentStep,
    CommandStep,
    DecisionStep,
    GateStep,
    Workflow,
    WorkflowDefinitionError,
    WorkflowNotWaitingError,
    WorkflowRunner,
    register_workflow,
    registered_workflows,
    unregister_workflow,
)

from tests.test_rpc import fake_omp_argv

DEBOUNCE = 0.1


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_path_for(data_dir)
    upgrade_head(db_path)
    return make_engine(db_path)


@pytest.fixture
def project(engine: Engine, tmp_path: Path):  # noqa: ANN001, ANN201
    return create_project(
        engine,
        name="demo",
        title="Demo",
        upstream_url="https://example.com/demo.git",
        checkout_path=str(tmp_path / "checkout"),
        default_checkout_root=tmp_path,
    )


def _make_template(engine: Engine, workflow: str = "single-step", preamble: str = "") -> Template:
    return create_template(
        engine,
        name=f"tpl-{workflow}",
        project_name="demo",
        branch_pattern="ompire/<slug>",
        workflow=workflow,
        preamble=preamble,
    )


def _make_task(
    engine: Engine, tmp_path: Path, template: Template, prompt: str = "do it"
) -> Task:
    clone_path = tmp_path / "tasks" / f"task-{template.name}"
    clone_path.mkdir(parents=True, exist_ok=True)
    return create_task(
        engine,
        project_name="demo",
        template_name=template.name,
        slug=f"task-{template.name}",
        branch="ompire/task",
        clone_path=str(clone_path),
        prompt=prompt,
        workflow_name=template.workflow,
    )


@pytest.fixture
def rig(engine: Engine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):  # noqa: ANN001, ANN201
    """Runner + supervisor + tracker wired to fake omp with a fast debounce.
    `scenario` lets a test switch the fake omp behavior before the run."""
    scenario = {"name": "happy"}
    monkeypatch.setattr(
        agent_module,
        "build_agent_argv",
        lambda clone, env, resume=None, model=None, thinking=None: fake_omp_argv(scenario["name"]),
    )

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
    return runner, supervisor, tracker, hub, scenario


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


# --- registration validation (2.1) ------------------------------------------


def test_workflow_definition_validation() -> None:
    with pytest.raises(WorkflowDefinitionError, match="duplicate step"):
        Workflow(
            name="bad",
            sessions=("main",),
            steps=(
                AgentStep(name="a", session="main", prompt=lambda ctx: "x"),
                AgentStep(name="a", session="main", prompt=lambda ctx: "y"),
            ),
        )
    with pytest.raises(WorkflowDefinitionError, match="undeclared session"):
        Workflow(
            name="bad2",
            sessions=("main",),
            steps=(AgentStep(name="a", session="ghost", prompt=lambda ctx: "x"),),
        )
    with pytest.raises(WorkflowDefinitionError, match="primary"):
        Workflow(
            name="bad3",
            sessions=("main",),
            steps=(AgentStep(name="a", session="main", prompt=lambda ctx: "x"),),
            primary="ghost",
        )


def test_template_validation_uses_engine_registry(engine: Engine, project) -> None:  # noqa: ANN001
    assert "single-step" in registered_workflows()
    with pytest.raises(UnknownWorkflowError):
        _make_template(engine, workflow="no-such-workflow")


# --- single-step parity (2.4, D-10) ------------------------------------------


async def test_single_step_delivers_preamble_plus_prompt(rig, engine, project, tmp_path: Path) -> None:  # noqa: ANN001
    runner, supervisor, tracker, hub, _scenario = rig
    template = _make_template(engine, preamble="PRE")
    task = _make_task(engine, tmp_path, template, prompt="do it")

    runner.start_run(task, template)
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
    from ompire_daemon.registry.sessions import get_session

    session_row = get_session(engine, task.id, "main")
    assert session_row is not None and session_row.omp_session_id == "fake-session-id"


async def test_single_step_empty_prompt_sends_nothing(rig, engine, project, tmp_path: Path) -> None:  # noqa: ANN001
    runner, supervisor, tracker, hub, _scenario = rig
    template = _make_template(engine, preamble="PRE")
    task = _make_task(engine, tmp_path, template, prompt="")

    runner.start_run(task, template)
    await wait_for_run(engine, task.id, {"complete"})

    assert user_prompts(supervisor, task.id, "main") == []
    info = tracker.get(task.id, "main")
    assert info is not None and info.status == "idle"
    assert "no prompt" in info.reason
    assert list_step_records(engine, task.id)[0].prompted_at is None


async def test_run_fails_when_session_spawn_fails(rig, engine, project, tmp_path: Path) -> None:  # noqa: ANN001
    runner, supervisor, tracker, hub, scenario = rig
    scenario["name"] = "crash"
    template = _make_template(engine)
    task = _make_task(engine, tmp_path, template)

    runner.start_run(task, template)
    final = await wait_for_run(engine, task.id, {"failed"})

    assert final.workflow_status == "failed"
    assert final.state == "created"  # workflow failure is not workspace failure
    assert final.error
    info = tracker.get(task.id, "main")
    assert info is not None and info.status == "failed"
    records = list_step_records(engine, task.id)
    assert records[0].status == "failed"
    assert records[0].error


# --- outcome convention (2.4, D-3) -------------------------------------------

OUTCOME_WORKFLOW = Workflow(
    name="outcome-wf",
    sessions=("main",),
    steps=(
        AgentStep(
            name="work",
            session="main",
            prompt=lambda ctx: ctx.task.prompt,
            expects_outcome=True,
        ),
    ),
)


@pytest.fixture
def outcome_workflow():
    register_workflow(OUTCOME_WORKFLOW)
    yield OUTCOME_WORKFLOW
    unregister_workflow(OUTCOME_WORKFLOW.name)


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


async def test_outcome_written(rig, engine, project, tmp_path: Path, outcome_workflow) -> None:  # noqa: ANN001
    runner, supervisor, tracker, hub, _scenario = rig
    template = _make_template(engine, workflow="outcome-wf")
    task = _make_task(engine, tmp_path, template)
    # A stale file from "an earlier step" must be unlinked before prompting.
    (Path(task.clone_path) / ".ompire").mkdir(parents=True)
    (Path(task.clone_path) / ".ompire" / "outcome.json").write_text('{"version": 1, "status": "failed", "summary": "stale"}')

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
    runner.start_run(task, template)
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


async def test_outcome_missing_is_data_not_failure(rig, engine, project, tmp_path: Path, outcome_workflow) -> None:  # noqa: ANN001
    runner, supervisor, tracker, hub, _scenario = rig
    template = _make_template(engine, workflow="outcome-wf")
    task = _make_task(engine, tmp_path, template)

    runner.start_run(task, template)
    await wait_for_run(engine, task.id, {"complete"})

    record = list_step_records(engine, task.id)[0]
    assert record.status == "ok"
    assert record.outcome is None
    assert record.error and "no outcome file" in record.error


async def test_outcome_malformed_recorded_as_null(rig, engine, project, tmp_path: Path, outcome_workflow) -> None:  # noqa: ANN001
    runner, supervisor, tracker, hub, _scenario = rig
    template = _make_template(engine, workflow="outcome-wf")
    task = _make_task(engine, tmp_path, template)

    writer = asyncio.create_task(
        _write_outcome_when_prompted(
            supervisor, task.id, Path(task.clone_path), "not json {"
        )
    )
    runner.start_run(task, template)
    await wait_for_run(engine, task.id, {"complete"})
    await writer

    record = list_step_records(engine, task.id)[0]
    assert record.status == "ok"
    assert record.outcome is None
    assert record.error and "JSON" in record.error


# --- command and decision steps (2.5) ----------------------------------------

BRANCH_WORKFLOW = Workflow(
    name="branch-wf",
    sessions=("main",),
    steps=(
        CommandStep(name="probe", argv=("probe-cmd",), timeout=5),
        DecisionStep(
            name="route",
            route=lambda ctx: "fix"
            if (ctx.outcome("probe") or {}).get("exit_code") == 0
            else "bail",
        ),
        # Fall-through is linear (design D-2): the gate sits between the
        # decision and `fix`, so routing to `fix` skips it, and resuming the
        # gate continues at `fix`.
        GateStep(name="bail", message=lambda ctx: "probe failed; operator call"),
        AgentStep(name="fix", session="main", prompt=lambda ctx: "fix it"),
    ),
)


@pytest.fixture
def branch_workflow(fake_workshop_cli: Path):  # noqa: ANN001
    """Register the branching workflow; the autouse fake workshop exits 0 for
    any command, so `probe-cmd` records exit_code 0 by default."""
    register_workflow(BRANCH_WORKFLOW)
    yield BRANCH_WORKFLOW
    unregister_workflow(BRANCH_WORKFLOW.name)


async def test_command_and_decision_routing(rig, engine, project, tmp_path: Path, branch_workflow) -> None:  # noqa: ANN001
    runner, supervisor, tracker, hub, _scenario = rig
    template = _make_template(engine, workflow="branch-wf")
    task = _make_task(engine, tmp_path, template)

    runner.start_run(task, template)
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
    the decision then routes on it (here: to the `bail` gate)."""
    fake_workshop_cli.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *"config get ask.timeout"*) echo 0 ;;\n'
        '  *"--mode rpc-ui"*) exit 1 ;;\n'
        '  *probe-cmd*) echo "probe output"; exit 3 ;;\n'
        '  *) exit 0 ;;\n'
        "esac\n"
    )
    runner, supervisor, tracker, hub, _scenario = rig
    template = _make_template(engine, workflow="branch-wf")
    task = _make_task(engine, tmp_path, template)

    runner.start_run(task, template)
    await wait_for_run(engine, task.id, {"waiting"})

    records = list_step_records(engine, task.id)
    assert records[0].step == "probe"
    assert records[0].status == "ok"
    assert records[0].outcome["exit_code"] == 3
    assert "probe output" in records[0].outcome["output"]
    assert records[1].outcome == {"route": "bail"}
    # The run parked at the declared gate with its message.
    gate = records[2]
    assert gate.kind == "gate" and gate.status == "waiting"
    assert gate.outcome == {"message": "probe failed; operator call"}

    runner.resume_gate(task.id, note="looks fine")
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
    workflow = Workflow(
        name="slow-cmd-wf",
        sessions=("main",),
        steps=(CommandStep(name="slow", argv=("slow-cmd",), timeout=0.2),),
    )
    register_workflow(workflow)
    try:
        runner, supervisor, tracker, hub, _scenario = rig
        template = _make_template(engine, workflow="slow-cmd-wf")
        task = _make_task(engine, tmp_path, template)

        runner.start_run(task, template)
        final = await wait_for_run(engine, task.id, {"failed"})

        assert final.state == "created"
        records = list_step_records(engine, task.id)
        assert records[0].status == "failed"
        assert "timed out" in (records[0].error or "")
    finally:
        unregister_workflow(workflow.name)


async def test_decision_unresolvable_escalates_to_gate(rig, engine, project, tmp_path: Path) -> None:  # noqa: ANN001
    def raise_route(ctx) -> str:  # noqa: ANN001, ANN202
        raise RuntimeError("missing repro outcome")

    workflow = Workflow(
        name="raise-wf",
        sessions=("main",),
        steps=(
            DecisionStep(name="triage", route=raise_route),
            CommandStep(name="after", argv=("true",), timeout=5),
        ),
    )
    register_workflow(workflow)
    try:
        runner, supervisor, tracker, hub, _scenario = rig
        template = _make_template(engine, workflow="raise-wf")
        task = _make_task(engine, tmp_path, template)

        runner.start_run(task, template)
        await wait_for_run(engine, task.id, {"waiting"})

        records = list_step_records(engine, task.id)
        # Decision finishes ok with the escalation note; the synthesized gate
        # parks under the decision's own name.
        assert records[0].step == "triage" and records[0].status == "ok"
        assert "missing repro outcome" in (records[0].error or "")
        gate = records[1]
        assert gate.kind == "gate" and gate.step == "triage" and gate.status == "waiting"
        assert "missing repro outcome" in gate.outcome["message"]

        # Resuming continues at the step declared after the decision.
        runner.resume_gate(task.id, note=None)
        await wait_for_run(engine, task.id, {"complete"})
        steps = [r.step for r in list_step_records(engine, task.id)]
        assert steps == ["triage", "triage", "after"]
    finally:
        unregister_workflow(workflow.name)


async def test_resume_gate_rejects_non_waiting_run(rig, engine, project, tmp_path: Path) -> None:  # noqa: ANN001
    runner, supervisor, tracker, hub, _scenario = rig
    template = _make_template(engine)
    task = _make_task(engine, tmp_path, template)

    with pytest.raises(WorkflowNotWaitingError):
        runner.resume_gate(task.id, note=None)

    runner.start_run(task, template)
    await wait_for_run(engine, task.id, {"complete"})
    with pytest.raises(WorkflowNotWaitingError):
        runner.resume_gate(task.id, note=None)


# --- multi-step, multi-session (2.7) -----------------------------------------

TWO_SESSION_WORKFLOW = Workflow(
    name="two-session-wf",
    sessions=("coder", "reviewer"),
    primary="reviewer",
    steps=(
        AgentStep(name="implement", session="coder", prompt=lambda ctx: "implement it"),
        AgentStep(
            name="inspect",
            session="reviewer",
            prompt=lambda ctx: f"records so far: {len(ctx.records())}",
        ),
    ),
)


async def test_multi_step_run_with_two_named_sessions(rig, engine, project, tmp_path: Path) -> None:  # noqa: ANN001
    register_workflow(TWO_SESSION_WORKFLOW)
    try:
        runner, supervisor, tracker, hub, _scenario = rig
        template = _make_template(engine, workflow="two-session-wf")
        task = _make_task(engine, tmp_path, template)

        runner.start_run(task, template)
        await wait_for_run(engine, task.id, {"complete"})

        # Each session ran exactly its own step, on its own child process.
        # At the second step's prompt time, history holds the first step's
        # finished record (its own record is appended after prompt build).
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
        assert TWO_SESSION_WORKFLOW.primary == "reviewer"
    finally:
        unregister_workflow(TWO_SESSION_WORKFLOW.name)


# --- restart recovery (5.3, design D-6) ---------------------------------------


async def test_interrupted_agent_step_is_nudged_once(
    rig, engine, project, tmp_path: Path, outcome_workflow, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Restart mid-turn: the interrupted record closes failed, the resumed
    session gets ONE resume nudge (not the full prompt), and the step
    completes from the following turn boundary."""
    runner, supervisor, tracker, hub, scenario = rig
    scenario["name"] = "no-end"  # burst without agent_end: the turn never ends
    template = _make_template(engine, workflow="outcome-wf")
    task = _make_task(engine, tmp_path, template)

    runner.start_run(task, template)
    # Wait until the prompt is durably marked sent (the crash window the
    # nudge path exists for).
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        records = list_step_records(engine, task.id)
        if records and records[0].prompted_at is not None:
            break
        await asyncio.sleep(0.02)
    assert records[0].prompted_at is not None

    # Simulate the restart: fresh in-memory machinery over the same DB.
    await runner.shutdown()
    await supervisor.shutdown()
    scenario["name"] = "happy"
    hub2 = EventHub()
    tracker2 = SessionTracker(hub2, idle_debounce=DEBOUNCE, stall_threshold=300)
    config2 = Config(
        data_dir=tmp_path / "data",
        task_dir_root=tmp_path / "tasks",
        checkout_root=tmp_path / "proj",
        session_idle_debounce=DEBOUNCE,
        spawn_step_timeout=10,
    )
    supervisor2 = AgentSupervisor(config2, hub2, tracker2)
    runner2 = WorkflowRunner(engine, config2, hub2, supervisor2, tracker2)

    # Recovery resumes the recorded session, then re-drives the run.
    from ompire_daemon.registry.sessions import list_resumable_sessions

    sessions = list_resumable_sessions(engine, task.id)
    assert len(sessions) == 1
    tracker2.recovering(task.id, "main")
    await supervisor2.start(task.id, "main", task.clone_path, resume=sessions[0].omp_session_id)
    tracker2.session_recovered(task.id, "main")
    runner2.recover_run(get_task(engine, task.id), template)

    await wait_for_run(engine, task.id, {"complete"})

    prompts = user_prompts(supervisor2, task.id, "main")
    assert len(prompts) == 1
    assert prompts[0].startswith("The daemon restarted")
    assert "outcome.json" in prompts[0]  # outcome-bearing: instruction re-stated
    records = list_step_records(engine, task.id)
    assert [(r.step, r.status) for r in records] == [("work", "failed"), ("work", "ok")]
    assert "interrupted by daemon restart" in (records[0].error or "")


async def test_unsent_agent_step_sends_fresh_after_restart(
    rig, engine, project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Crash after the step record was created but before the prompt went
    out: recovery sends the step prompt, not a nudge."""
    runner, supervisor, tracker, hub, _scenario = rig
    template = _make_template(engine)
    task = _make_task(engine, tmp_path, template)

    runner.start_run(task, template)
    # Wait for the record to exist, then "restart" before the prompt lands.
    # (Racy by nature; prompted_at NULL is what matters, and the nudge path
    # is separately covered — so force the record state deterministically.)
    await wait_for_run(engine, task.id, {"complete"})
    await runner.shutdown()
    await supervisor.shutdown()

    # Reset the persisted history to the pre-prompt state: one running record
    # with no prompted_at.
    from ompire_daemon.registry.workflows import append_step_record, finish_step_record

    records = list_step_records(engine, task.id)
    finish_step_record(engine, task.id, records[0].seq, status="ok")
    # Undo the completion to re-enter `running` at a fresh unsent step.
    from ompire_daemon.registry.workflows import set_run_status

    append_step_record(engine, task.id, step="work", kind="agent", session="main")
    set_run_status(engine, task.id, "running", "work")

    hub2 = EventHub()
    tracker2 = SessionTracker(hub2, idle_debounce=DEBOUNCE, stall_threshold=300)
    config2 = Config(
        data_dir=tmp_path / "data",
        task_dir_root=tmp_path / "tasks",
        checkout_root=tmp_path / "proj",
        session_idle_debounce=DEBOUNCE,
        spawn_step_timeout=10,
    )
    supervisor2 = AgentSupervisor(config2, hub2, tracker2)
    runner2 = WorkflowRunner(engine, config2, hub2, supervisor2, tracker2)
    runner2.recover_run(get_task(engine, task.id), template)

    await wait_for_run(engine, task.id, {"complete"})
    prompts = user_prompts(supervisor2, task.id, "main")
    assert prompts == ["do it"]  # the full step prompt, not the nudge


async def test_gate_survives_restart(rig, engine, project, tmp_path: Path) -> None:  # noqa: ANN001
    """A run waiting at a gate re-arms after a restart: same record, same
    message, resumable."""
    workflow = Workflow(
        name="gate-wf",
        sessions=("main",),
        steps=(
            GateStep(name="approve", message=lambda ctx: "ship it?"),
            CommandStep(name="after", argv=("true",), timeout=5),
        ),
    )
    register_workflow(workflow)
    try:
        runner, supervisor, tracker, hub, _scenario = rig
        template = _make_template(engine, workflow="gate-wf")
        task = _make_task(engine, tmp_path, template)

        runner.start_run(task, template)
        await wait_for_run(engine, task.id, {"waiting"})
        assert list_step_records(engine, task.id)[0].outcome == {"message": "ship it?"}

        # Restart: fresh runner over the same DB re-arms the same record.
        await runner.shutdown()
        hub2 = EventHub()
        tracker2 = SessionTracker(hub2, idle_debounce=DEBOUNCE, stall_threshold=300)
        config2 = Config(
            data_dir=tmp_path / "data",
            task_dir_root=tmp_path / "tasks",
            checkout_root=tmp_path / "proj",
            session_idle_debounce=DEBOUNCE,
            spawn_step_timeout=10,
        )
        supervisor2 = AgentSupervisor(config2, hub2, tracker2)
        runner2 = WorkflowRunner(engine, config2, hub2, supervisor2, tracker2)
        events: list[dict] = []
        queue = hub2.subscribe()

        async def collect() -> None:
            while True:
                event = await queue.get()
                if event.type == "workflow_step":
                    events.append(event.payload)

        collector = asyncio.create_task(collect())
        runner2.recover_run(get_task(engine, task.id), template)
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

        runner2.resume_gate(task.id, note="yes")
        await wait_for_run(engine, task.id, {"complete"})
        collector.cancel()
        records = list_step_records(engine, task.id)
        assert records[0].status == "ok"
        assert records[0].outcome == {"message": "ship it?", "note": "yes"}
        assert [r.step for r in records] == ["approve", "after"]
    finally:
        unregister_workflow("gate-wf")


# --- REST surface (2.6) -------------------------------------------------------


def test_workflow_resume_endpoint_404_and_409(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.post(
        "/api/tasks/999/workflow/resume", headers=auth_headers, json={}
    )
    assert response.status_code == 404

    r = client.post(
        "/api/projects",
        headers=auth_headers,
        json={
            "name": "demo",
            "title": "Demo",
            "upstream_url": "https://example.com/demo.git",
            "checkout_path": "/tmp/nonexistent",
        },
    )
    assert r.status_code == 201, r.text
    r = client.post(
        "/api/templates",
        headers=auth_headers,
        json={"name": "demo", "project_name": "demo"},
    )
    assert r.status_code == 201, r.text
    from ompire_daemon.registry.tasks import create_task as _create

    task = _create(
        client.app.state.engine,
        project_name="demo",
        template_name="demo",
        slug="idle-task",
        branch="ompire/idle-task",
        clone_path="/tmp/nonexistent-clone",
        prompt="x",
    )
    response = client.post(
        f"/api/tasks/{task.id}/workflow/resume", headers=auth_headers, json={}
    )
    assert response.status_code == 409
