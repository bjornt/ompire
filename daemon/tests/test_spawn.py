"""Spawn pipeline tests, driven against a real fixture git repo, a fake
my-workshop script, and the fake omp behind the fake workshop CLI (no real
containers)."""

from __future__ import annotations

import subprocess
import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from ompire_daemon.agent import AgentSupervisor
from ompire_daemon.config import Config
from ompire_daemon.events import Event, EventHub
from ompire_daemon.registry.projects import create_project
from ompire_daemon.registry.tasks import (
    clone_path_for,
    create_task,
    get_task,
)
from ompire_daemon.registry.templates import create_template
from ompire_daemon.sessions import SessionTracker
from ompire_daemon.spawn import run_spawn_pipeline

from tests.conftest import FAKE_WORKSHOP_SCRIPT


@pytest.fixture
def fake_my_workshop(tmp_path: Path):
    """Factory for fake my-workshop scripts; returns the script path."""

    def make(body: str) -> str:
        script = tmp_path / "fake-my-workshop"
        script.write_text(f"#!/bin/sh\n{body}\n")
        script.chmod(0o755)
        return str(script)

    return make


@pytest.fixture
def pipeline(app):
    """A hub + tracker + supervisor trio sharing the app's config, plus a
    runner that drives the pipeline with them."""
    config: Config = app.state.config
    hub = EventHub()
    tracker = SessionTracker(hub, idle_debounce=0.1)
    supervisor = AgentSupervisor(config, hub, tracker)

    async def run(engine, task_id: int, run_config: Config | None = None, **overrides):  # noqa: ANN001
        await run_spawn_pipeline(
            engine, hub, run_config or config, task_id, supervisor, tracker, **overrides
        )

    return run, hub, tracker, supervisor


def _make_project(engine, checkout: Path):  # noqa: ANN001, ANN202
    return create_project(
        engine,
        name="demo",
        title="Demo",
        upstream_url="https://example.com/demo.git",
        checkout_path=str(checkout),
        default_checkout_root=checkout.parent,
    )


def _make_template(
    engine,  # noqa: ANN001
    checkout: Path,
    *,
    base_branch: str = "main",
    preamble: str = "",
    model: str | None = None,
    thinking: str | None = None,
):
    _make_project(engine, checkout)
    return create_template(
        engine,
        name="demo",
        project_name="demo",
        base_branch=base_branch,
        branch_pattern="ompire/<slug>",
        preamble=preamble,
        model=model,
        thinking=thinking,
    )


def _make_task(engine, config: Config, *, prompt: str = "fix the bug"):  # noqa: ANN001, ANN202
    clone_path = clone_path_for(config.task_dir_root, "demo", "fix-bug")
    return create_task(
        engine,
        project_name="demo",
        template_name="demo",
        slug="fix-bug",
        branch="ompire/fix-bug",
        clone_path=str(clone_path),
        prompt=prompt,
    )


def _drain(queue) -> list[Event]:  # noqa: ANN001
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


async def test_successful_pipeline(app, git_checkout: Path, fake_my_workshop, pipeline) -> None:  # noqa: ANN001
    engine = app.state.engine
    run, hub, tracker, supervisor = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop('echo "ws-demo-fix-bug" > .workshop.lock'),),
    )
    queue = hub.subscribe()

    _make_template(engine, git_checkout)
    task = _make_task(engine, config)

    await run(engine, task.id, config)

    clone = Path(task.clone_path)
    assert (clone / ".git").is_dir()
    head = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert head == "ompire/fix-bug"

    refreshed = get_task(engine, task.id)
    assert refreshed.state == "created"
    assert refreshed.spawn_completed_at is not None
    assert refreshed.workshop_id == "ws-demo-fix-bug"
    # Session id captured after ready (crash-recovery capability, design D-2).
    assert refreshed.session_id == "fake-session-id"

    # The agent is live with the stored prompt delivered.
    assert supervisor.get(task.id) is not None
    assert tracker.get(task.id) is not None

    spawn_steps = [e.payload for e in _drain(queue) if e.type == "spawn_step"]
    assert [(p["step"], p["status"]) for p in spawn_steps] == [
        ("fetch", "started"),
        ("fetch", "ok"),
        ("clone", "started"),
        ("clone", "ok"),
        ("branch", "started"),
        ("branch", "ok"),
        ("workshop", "started"),
        ("workshop", "ok"),
        ("agent", "started"),
        ("agent", "ok"),
        ("prompt", "started"),
        ("prompt", "ok"),
    ]
    await supervisor.stop(task.id)


async def test_empty_prompt_skips_prompt_step(
    app, git_checkout: Path, fake_my_workshop, pipeline  # noqa: ANN001
) -> None:
    engine = app.state.engine
    run, hub, tracker, supervisor = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop('echo "ws-x" > .workshop.lock'),),
    )
    queue = hub.subscribe()

    _make_template(engine, git_checkout)
    task = _make_task(engine, config, prompt="")

    await run(engine, task.id, config)

    refreshed = get_task(engine, task.id)
    assert refreshed.state == "created"
    assert refreshed.spawn_completed_at is not None

    steps = [p["step"] for e in _drain(queue) if e.type == "spawn_step" for p in [e.payload]]
    assert "prompt" not in steps
    assert "agent" in steps
    # Promptless task idles after ready instead of hanging in starting.
    assert tracker.get(task.id).status == "idle"
    assert tracker.get(task.id).reason == "ready, no prompt to send"
    await supervisor.stop(task.id)


async def test_agent_step_failure_marks_task_failed(
    app, git_checkout: Path, fake_my_workshop, fake_workshop_cli, pipeline  # noqa: ANN001
) -> None:
    engine = app.state.engine
    run, hub, tracker, _ = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop('echo "ws-x" > .workshop.lock'),),
    )
    # The rpc-ui spawn now crashes before ready with stderr.
    fake_workshop_cli.write_text(FAKE_WORKSHOP_SCRIPT.replace(" happy", " crash"))
    queue = hub.subscribe()

    _make_template(engine, git_checkout)
    task = _make_task(engine, config)

    await run(engine, task.id, config)

    refreshed = get_task(engine, task.id)
    assert refreshed.state == "failed"
    assert "agent" in (refreshed.error or "")
    assert "No models available" in (refreshed.error or "")

    drained = _drain(queue)
    failed_steps = [
        e.payload for e in drained if e.type == "spawn_step" and e.payload["status"] == "failed"
    ]
    assert failed_steps and failed_steps[0]["step"] == "agent"
    assert "No models available" in failed_steps[0]["stderr"]
    # The session lands failed too (design D-2).
    assert tracker.get(task.id).status == "failed"
    assert tracker.get(task.id).reason == "spawn step 'agent' failed"


async def test_session_id_capture_failure_leaves_it_null_and_spawn_succeeds(
    app, git_checkout: Path, fake_my_workshop, fake_workshop_cli, pipeline  # noqa: ANN001
) -> None:
    engine = app.state.engine
    run, hub, tracker, supervisor = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop('echo "ws-x" > .workshop.lock'),),
    )
    # The agent starts fine, but `get_state` (used to capture the session id)
    # answers success: false — capture must degrade gracefully, not fail the
    # spawn (design D-2).
    fake_workshop_cli.write_text(FAKE_WORKSHOP_SCRIPT.replace(" happy", " get-state-fails"))
    queue = hub.subscribe()

    _make_template(engine, git_checkout)
    task = _make_task(engine, config)

    await run(engine, task.id, config)

    refreshed = get_task(engine, task.id)
    assert refreshed.state == "created"
    assert refreshed.spawn_completed_at is not None
    assert refreshed.session_id is None

    spawn_steps = [
        (e.payload["step"], e.payload["status"])
        for e in _drain(queue)
        if e.type == "spawn_step"
    ]
    assert ("agent", "ok") in spawn_steps
    assert ("prompt", "ok") in spawn_steps
    await supervisor.stop(task.id)


async def test_prompt_step_failure_marks_task_failed(
    app, git_checkout: Path, fake_my_workshop, pipeline  # noqa: ANN001
) -> None:
    engine = app.state.engine
    run, hub, tracker, supervisor = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop('echo "ws-x" > .workshop.lock'),),
    )
    _make_template(engine, git_checkout)
    # The magic "fail" prompt makes fake omp answer success: false, "boom".
    task = _make_task(engine, config, prompt="fail")

    await run(engine, task.id, config)

    refreshed = get_task(engine, task.id)
    assert refreshed.state == "failed"
    assert "prompt" in (refreshed.error or "")
    assert "boom" in (refreshed.error or "")
    assert tracker.get(task.id).status == "failed"
    await supervisor.stop(task.id)


async def test_step_failure_captures_stderr(app, git_checkout: Path, pipeline) -> None:  # noqa: ANN001
    engine = app.state.engine
    run, hub, _, _ = pipeline
    queue = hub.subscribe()

    _make_template(engine, git_checkout, base_branch="no-such-branch")
    task = _make_task(engine, app.state.config)

    await run(engine, task.id)

    refreshed = get_task(engine, task.id)
    assert refreshed.state == "failed"
    assert refreshed.error is not None and "branch" in refreshed.error

    drained = _drain(queue)
    failed_steps = [
        e.payload for e in drained if e.type == "spawn_step" and e.payload["status"] == "failed"
    ]
    assert len(failed_steps) == 1
    assert failed_steps[0]["step"] == "branch"
    assert failed_steps[0]["stderr"]
    task_updates = [e.payload for e in drained if e.type == "task_updated"]
    assert task_updates[-1]["state"] == "failed"


async def test_leftover_directory_fails(app, git_checkout: Path, pipeline) -> None:  # noqa: ANN001
    engine = app.state.engine
    run, hub, _, _ = pipeline
    queue = hub.subscribe()

    _make_template(engine, git_checkout)
    task = _make_task(engine, app.state.config)
    Path(task.clone_path).mkdir(parents=True)

    await run(engine, task.id)

    refreshed = get_task(engine, task.id)
    assert refreshed.state == "failed"
    assert "already exists" in (refreshed.error or "")
    failed_steps = [
        e.payload for e in _drain(queue) if e.type == "spawn_step" and e.payload["status"] == "failed"
    ]
    assert failed_steps and failed_steps[0]["step"] == "clone"


async def test_workshop_step_failure(app, git_checkout: Path, fake_my_workshop, pipeline) -> None:  # noqa: ANN001
    engine = app.state.engine
    run, hub, _, _ = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop('echo "launch exploded" >&2; exit 3'),),
    )
    queue = hub.subscribe()

    _make_template(engine, git_checkout)
    task = _make_task(engine, config)

    await run(engine, task.id, config)

    refreshed = get_task(engine, task.id)
    assert refreshed.state == "failed"
    assert refreshed.workshop_id is None
    assert "workshop" in (refreshed.error or "")
    assert "launch exploded" in (refreshed.error or "")

    failed_steps = [
        e.payload for e in _drain(queue) if e.type == "spawn_step" and e.payload["status"] == "failed"
    ]
    assert failed_steps and failed_steps[0]["step"] == "workshop"
    assert "launch exploded" in failed_steps[0]["stderr"]


async def test_workshop_step_timeout(app, git_checkout: Path, fake_my_workshop, pipeline) -> None:  # noqa: ANN001
    engine = app.state.engine
    run, _, _, _ = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop("sleep 5"),),
        workshop_step_timeout=1,
    )

    _make_template(engine, git_checkout)
    task = _make_task(engine, config)

    await run(engine, task.id, config)

    refreshed = get_task(engine, task.id)
    assert refreshed.state == "failed"
    assert "timed out after 1s" in (refreshed.error or "")


async def test_workshop_lock_missing_after_success(
    app, git_checkout: Path, fake_my_workshop, pipeline  # noqa: ANN001
) -> None:
    engine = app.state.engine
    run, hub, _, _ = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop("exit 0"),),
    )
    queue = hub.subscribe()

    _make_template(engine, git_checkout)
    task = _make_task(engine, config)

    await run(engine, task.id, config)

    refreshed = get_task(engine, task.id)
    assert refreshed.state == "failed"
    assert refreshed.workshop_id is None
    assert ".workshop.lock" in (refreshed.error or "")

    failed_steps = [
        e.payload for e in _drain(queue) if e.type == "spawn_step" and e.payload["status"] == "failed"
    ]
    assert failed_steps and failed_steps[0]["step"] == "workshop"


async def test_workshop_tool_missing(app, git_checkout: Path, pipeline) -> None:  # noqa: ANN001
    engine = app.state.engine
    run, _, _, _ = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=("/no/such/my-workshop",),
    )

    _make_template(engine, git_checkout)
    task = _make_task(engine, config)

    await run(engine, task.id, config)

    refreshed = get_task(engine, task.id)
    assert refreshed.state == "failed"
    assert "cannot exec" in (refreshed.error or "")


# --- Template-driven spawn (templates capability) ---------------------------


async def test_template_missing_at_pipeline_start_fails_before_git(
    app, git_checkout: Path, fake_my_workshop, pipeline  # noqa: ANN001
) -> None:
    engine = app.state.engine
    run, hub, tracker, _ = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop('echo "ws-x" > .workshop.lock'),),
    )
    queue = hub.subscribe()

    _make_template(engine, git_checkout)
    task = _make_task(engine, config)
    # The template disappears between the 202 and pipeline start (raw delete:
    # the REST guard blocks this once the task row exists — the race needs
    # the row gone underneath a live task, e.g. a DB restore).
    from ompire_daemon.db import templates as templates_table

    with engine.begin() as conn:
        conn.execute(templates_table.delete().where(templates_table.c.name == "demo"))

    await run(engine, task.id, config)

    refreshed = get_task(engine, task.id)
    assert refreshed.state == "failed"
    assert "template" in (refreshed.error or "")
    assert "'demo'" in (refreshed.error or "")
    # No clone ever happened: the failure lands before any git command, and
    # no spawn_step events are published.
    assert not Path(task.clone_path).exists()
    drained = _drain(queue)
    assert not [e for e in drained if e.type == "spawn_step"]
    # No session entry was ever seeded (the agent step never ran).
    assert tracker.get(task.id) is None


async def test_preamble_prepended_to_prompt(
    app, git_checkout: Path, fake_my_workshop, pipeline  # noqa: ANN001
) -> None:
    engine = app.state.engine
    run, _, _, supervisor = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop('echo "ws-x" > .workshop.lock'),),
    )

    _make_template(engine, git_checkout, preamble="You are on team omega.")
    task = _make_task(engine, config, prompt="fix the bug")

    await run(engine, task.id, config)

    refreshed = get_task(engine, task.id)
    assert refreshed.state == "created"

    # The fake omp echoes the prompt back in its `message_start` user event,
    # fanned out on the agent handle's ring buffer (not the hub). Subscribe
    # after the agent starts: the prompt's ack completes the step while the
    # echoed frames are still in flight, so drain the live queue instead of
    # racing the ring buffer.
    handle = supervisor.get(task.id)
    assert handle is not None
    queue = handle.subscribe()
    async with asyncio.timeout(5):
        while True:
            event = await queue.get()
            if (
                event.type == "message_start"
                and event.payload.get("message", {}).get("role") == "user"
            ):
                text = event.payload["message"]["content"][0]["text"]
                break
    assert text == "You are on team omega.\n\nfix the bug"
    await supervisor.stop(task.id)


async def test_empty_preamble_sends_prompt_unchanged(
    app, git_checkout: Path, fake_my_workshop, pipeline  # noqa: ANN001
) -> None:
    engine = app.state.engine
    run, _, _, supervisor = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop('echo "ws-x" > .workshop.lock'),),
    )

    _make_template(engine, git_checkout, preamble="")
    task = _make_task(engine, config, prompt="fix the bug")

    await run(engine, task.id, config)

    handle = supervisor.get(task.id)
    assert handle is not None
    queue = handle.subscribe()
    async with asyncio.timeout(5):
        while True:
            event = await queue.get()
            if (
                event.type == "message_start"
                and event.payload.get("message", {}).get("role") == "user"
            ):
                text = event.payload["message"]["content"][0]["text"]
                break
    assert text == "fix the bug"
    await supervisor.stop(task.id)


async def test_preamble_alone_never_prompts(
    app, git_checkout: Path, fake_my_workshop, pipeline  # noqa: ANN001
) -> None:
    engine = app.state.engine
    run, hub, _, supervisor = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop('echo "ws-x" > .workshop.lock'),),
    )
    queue = hub.subscribe()

    _make_template(engine, git_checkout, preamble="You are on team omega.")
    task = _make_task(engine, config, prompt="")

    await run(engine, task.id, config)

    refreshed = get_task(engine, task.id)
    assert refreshed.state == "created"
    steps = [p["step"] for e in _drain(queue) if e.type == "spawn_step" for p in [e.payload]]
    assert "prompt" not in steps
    await supervisor.stop(task.id)


def _argv_capturing_workshop(fake_workshop_cli: Path, argv_file: Path) -> None:  # noqa: ANN001
    """Like the conftest fake, but records the rpc-ui spawn argv to a file."""
    from tests.conftest import FAKE_OMP

    fake_workshop_cli.write_text(
        f"""#!/bin/sh
case "$*" in
  *"config get ask.timeout"*) echo 0 ;;
  *"--mode rpc-ui"*)
    printf '%s\\n' "$*" > {argv_file}
    exec {__import__("sys").executable} -u {FAKE_OMP} happy ;;
  *) exit 0 ;;
esac
"""
    )


async def test_template_model_thinking_on_agent_argv(
    app, git_checkout: Path, fake_my_workshop, fake_workshop_cli, tmp_path: Path, pipeline  # noqa: ANN001
) -> None:
    engine = app.state.engine
    run, _, _, supervisor = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop('echo "ws-x" > .workshop.lock'),),
    )
    argv_file = tmp_path / "argv.txt"
    _argv_capturing_workshop(fake_workshop_cli, argv_file)

    _make_template(engine, git_checkout, model="fable-5", thinking="medium")
    task = _make_task(engine, config)

    await run(engine, task.id, config)

    argv = argv_file.read_text()
    assert "--model fable-5" in argv
    assert "--thinking medium" in argv
    await supervisor.stop(task.id)


async def test_overrides_beat_template_values(
    app, git_checkout: Path, fake_my_workshop, fake_workshop_cli, tmp_path: Path, pipeline  # noqa: ANN001
) -> None:
    engine = app.state.engine
    run, _, _, supervisor = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop('echo "ws-x" > .workshop.lock'),),
    )
    argv_file = tmp_path / "argv.txt"
    _argv_capturing_workshop(fake_workshop_cli, argv_file)

    _make_template(engine, git_checkout, model="fable-5", thinking="medium")
    task = _make_task(engine, config)

    await run(engine, task.id, config, model_override="zephyr-9", thinking_override="high")

    argv = argv_file.read_text()
    assert "--model zephyr-9" in argv
    assert "--thinking high" in argv
    assert "fable-5" not in argv
    assert "medium" not in argv
    await supervisor.stop(task.id)


async def test_template_defaults_omit_flags(
    app, git_checkout: Path, fake_my_workshop, fake_workshop_cli, tmp_path: Path, pipeline  # noqa: ANN001
) -> None:
    engine = app.state.engine
    run, _, _, supervisor = pipeline
    config: Config = replace(
        app.state.config,
        my_workshop_command=(fake_my_workshop('echo "ws-x" > .workshop.lock'),),
    )
    argv_file = tmp_path / "argv.txt"
    _argv_capturing_workshop(fake_workshop_cli, argv_file)

    _make_template(engine, git_checkout)  # null model/thinking
    task = _make_task(engine, config)

    await run(engine, task.id, config)

    argv = argv_file.read_text()
    assert "--model" not in argv
    assert "--thinking" not in argv
    await supervisor.stop(task.id)
