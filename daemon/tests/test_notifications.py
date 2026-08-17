"""AttentionNotifier tests: tier map, notify-send firing/degradation, one
active notification per task, re-notify aging, and the Open action — all
driven against a fake `notify-send` script on PATH (never a real one)."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from ompire_daemon.events import EventHub
from ompire_daemon.notifications import AttentionNotifier, tier_for

RENOTIFY = 0.15


def test_tier_map() -> None:
    assert tier_for("starting") == "silent"
    assert tier_for("working") == "silent"
    assert tier_for("idle") == "badge"
    assert tier_for("retrying") == "badge"
    assert tier_for("waiting-input") == "notify"
    assert tier_for("stalled") == "notify"
    assert tier_for("reviewing") == "notify"
    assert tier_for("waiting-approval") == "interrupt"
    assert tier_for("failed") == "interrupt"
    assert tier_for("some-unknown-status") == "silent"


def _write_script(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


@pytest.fixture(autouse=True)
def fake_dbus_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    # Capability probe requires a reachable session bus; fake one so the
    # probe reaches the notify-send capability check in every test here.
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/tmp/fake-bus")


@pytest.fixture
def fake_gdbus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Installs a fake `gdbus` on PATH simulating the notification server's
    `GetCapabilities` response, so the probe's real-capability check (added
    after live dogfooding found stock GNOME doesn't advertise `actions`) can
    be exercised without a real notification server."""
    bin_dir = tmp_path / "gdbus-bin"
    bin_dir.mkdir()
    script = bin_dir / "gdbus"

    def configure(*, actions_supported: bool) -> None:
        caps = "'body', 'body-markup', 'actions'" if actions_supported else "'body', 'body-markup'"
        _write_script(script, f"echo \"([{caps}],)\"\nexit 0")

    configure(actions_supported=True)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return configure


@pytest.fixture
def fake_notify_send(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Installs a fake `notify-send` on PATH that logs every invocation (one
    line per call, space-joined args) to `calls.log`, and returns a factory to
    configure whether it echoes `default` (simulating a body click invoking
    the `default` action id)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls_log = tmp_path / "calls.log"
    script = bin_dir / "notify-send"

    def configure(*, echo_open: bool = False) -> Path:
        action = 'echo "default"' if echo_open else "true"
        _write_script(
            script,
            f"""
if [ "$1" = "--help" ]; then
  echo "Usage: notify-send [OPTION...] SUMMARY [BODY]"
  echo "  --action=KEY=LABEL      Specify an action"
  exit 0
fi
echo "$@" >> {calls_log}
{action}
exit 0
""",
        )
        return calls_log

    configure(echo_open=False)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return configure, calls_log


@pytest.fixture
def fake_xdg_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    bin_dir = tmp_path / "bin2"
    bin_dir.mkdir()
    calls_log = tmp_path / "xdg-calls.log"
    script = bin_dir / "xdg-open"
    _write_script(script, f'echo "$@" >> {calls_log}\nexit 0')
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return calls_log


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def _make_notifier(hub: EventHub, **overrides) -> AttentionNotifier:
    kwargs = {
        "bind": "127.0.0.1",
        "port": 4173,
        "renotify_interval": RENOTIFY,
        "enabled": True,
    }
    kwargs.update(overrides)
    notifier = AttentionNotifier(hub, **kwargs)
    await notifier.probe()
    notifier.start()
    return notifier


async def test_notify_tier_fires_a_notification(fake_notify_send) -> None:
    _, calls_log = fake_notify_send
    hub = EventHub()
    notifier = await _make_notifier(hub)
    try:
        hub.publish(
            "status_changed",
            {"task_id": 1, "session": "main", "from": "working", "to": "waiting-input", "reason": "pending question"},
        )
        await _wait_until(lambda: calls_log.exists() and calls_log.read_text())
        call = calls_log.read_text()
        assert "--urgency normal" in call
        assert "--action default=Open" in call
    finally:
        await notifier.stop()


async def test_interrupt_tier_fires_critical_urgency(fake_notify_send) -> None:
    _, calls_log = fake_notify_send
    hub = EventHub()
    notifier = await _make_notifier(hub)
    try:
        hub.publish(
            "status_changed",
            {"task_id": 1, "session": "main", "from": "working", "to": "failed", "reason": "process exited with code 1"},
        )
        await _wait_until(lambda: calls_log.exists() and calls_log.read_text())
        assert "--urgency critical" in calls_log.read_text()
    finally:
        await notifier.stop()


async def test_silent_and_badge_tiers_fire_nothing(fake_notify_send) -> None:
    _, calls_log = fake_notify_send
    hub = EventHub()
    notifier = await _make_notifier(hub)
    try:
        for to in ("starting", "working", "idle", "retrying"):
            hub.publish(
                "status_changed",
                {"task_id": 1, "session": "main", "from": None, "to": to, "reason": "x"},
            )
        await asyncio.sleep(0.1)
        assert not calls_log.exists() or calls_log.read_text() == ""
    finally:
        await notifier.stop()


async def test_open_action_launches_the_task_url(fake_notify_send, fake_xdg_open) -> None:
    configure, _ = fake_notify_send
    configure(echo_open=True)
    hub = EventHub()
    notifier = await _make_notifier(hub, port=9999, bind="0.0.0.0")
    try:
        hub.publish(
            "status_changed",
            {"task_id": 42, "session": "main", "from": "working", "to": "waiting-approval", "reason": "sudo gate"},
        )
        await _wait_until(lambda: fake_xdg_open.exists() and fake_xdg_open.read_text())
        assert fake_xdg_open.read_text().strip() == "http://0.0.0.0:9999/tasks/42"
    finally:
        await notifier.stop()


async def test_tier_change_supersedes_prior_notification(fake_notify_send) -> None:
    _, calls_log = fake_notify_send
    hub = EventHub()
    notifier = await _make_notifier(hub)
    try:
        hub.publish(
            "status_changed",
            {"task_id": 1, "session": "main", "from": "working", "to": "stalled", "reason": "no frames for 300s"},
        )
        await _wait_until(lambda: calls_log.exists() and calls_log.read_text())
        hub.publish(
            "status_changed",
            {"task_id": 1, "session": "main", "from": "stalled", "to": "failed", "reason": "process exited with code 1"},
        )
        await _wait_until(lambda: calls_log.read_text().count("\n") >= 2)
        calls = calls_log.read_text().splitlines()
        assert "--urgency normal" in calls[0]
        assert "--urgency critical" in calls[-1]
        # Exactly one active entry for the task even after the supersede.
        assert notifier.snapshot() == {1: {"tier": "interrupt", "status": "failed", "reason": "process exited with code 1", "session": "main"}}
    finally:
        await notifier.stop()


async def test_renotify_ages_unanswered_attention(fake_notify_send) -> None:
    _, calls_log = fake_notify_send
    hub = EventHub()
    notifier = await _make_notifier(hub)
    try:
        hub.publish(
            "status_changed",
            {"task_id": 1, "session": "main", "from": "working", "to": "waiting-input", "reason": "pending question"},
        )
        await _wait_until(lambda: calls_log.exists() and len(calls_log.read_text().splitlines()) >= 1)
        await _wait_until(
            lambda: len(calls_log.read_text().splitlines()) >= 2, timeout=RENOTIFY * 10
        )
        # Still the same tier/status/reason on the re-fire.
        lines = calls_log.read_text().splitlines()
        assert lines[0] == lines[1]
    finally:
        await notifier.stop()


async def test_answering_cancels_renotify(fake_notify_send) -> None:
    _, calls_log = fake_notify_send
    hub = EventHub()
    notifier = await _make_notifier(hub)
    try:
        hub.publish(
            "status_changed",
            {"task_id": 1, "session": "main", "from": "working", "to": "waiting-input", "reason": "pending question"},
        )
        await _wait_until(lambda: calls_log.exists() and calls_log.read_text())
        hub.publish(
            "status_changed",
            {"task_id": 1, "session": "main", "from": "waiting-input", "to": "working", "reason": "operator answered"},
        )
        first_count = len(calls_log.read_text().splitlines())
        await asyncio.sleep(RENOTIFY * 3)
        assert len(calls_log.read_text().splitlines()) == first_count
        assert notifier.snapshot() == {}
    finally:
        await notifier.stop()


async def test_attention_events_broadcast(fake_notify_send) -> None:
    hub = EventHub()
    queue = hub.subscribe()
    notifier = await _make_notifier(hub)
    try:
        hub.publish(
            "status_changed",
            {"task_id": 7, "session": "main", "from": "working", "to": "waiting-approval", "reason": "sudo gate"},
        )
        async with asyncio.timeout(5):
            while True:
                event = await queue.get()
                if event.type == "attention":
                    break
        assert event.payload == {
            "task_id": 7,
            "tier": "interrupt",
            "status": "waiting-approval",
            "reason": "sudo gate",
            "session": "main",
        }

        hub.publish(
            "status_changed",
            {"task_id": 7, "session": "main", "from": "waiting-approval", "to": "working", "reason": "operator answered"},
        )
        async with asyncio.timeout(5):
            while True:
                event = await queue.get()
                if event.type == "attention_cleared":
                    break
        assert event.payload == {"task_id": 7}
    finally:
        await notifier.stop()


async def test_missing_notify_send_degrades_to_badges(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/nonexistent-empty-path")
    hub = EventHub()
    queue = hub.subscribe()
    notifier = await _make_notifier(hub)
    try:
        assert notifier.capable is False
        hub.publish(
            "status_changed",
            {"task_id": 1, "session": "main", "from": "working", "to": "failed", "reason": "process exited with code 1"},
        )
        async with asyncio.timeout(5):
            while True:
                event = await queue.get()
                if event.type == "attention":
                    break
        assert event.payload["task_id"] == 1
        assert notifier.snapshot() == {
            1: {"tier": "interrupt", "status": "failed", "reason": "process exited with code 1", "session": "main"}
        }
    finally:
        await notifier.stop()


async def test_server_without_actions_capability_fires_plain_notification(
    fake_notify_send, fake_gdbus
) -> None:
    """Confirmed live-dogfooding 2026-07-21: stock GNOME's notification
    server doesn't advertise `actions` for the legacy interface `notify-send`
    uses (interactive actions are effectively reserved for XDG-portal apps),
    so `notify-send --wait --action ...` silently displays non-interactively.
    The probe now detects this via a direct `GetCapabilities` query and skips
    straight to a plain notification instead."""
    fake_gdbus(actions_supported=False)
    _, calls_log = fake_notify_send
    hub = EventHub()
    notifier = await _make_notifier(hub)
    try:
        assert notifier.capable is True
        assert notifier.actions_supported is False

        hub.publish(
            "status_changed",
            {"task_id": 1, "session": "main", "from": "working", "to": "waiting-input", "reason": "pending question"},
        )
        await _wait_until(lambda: calls_log.exists() and calls_log.read_text())
        call = calls_log.read_text()
        assert "--action" not in call
        assert "--wait" not in call
        assert "--urgency normal" in call
    finally:
        await notifier.stop()


async def test_disabled_by_config_degrades_to_badges(fake_notify_send) -> None:
    _, calls_log = fake_notify_send
    hub = EventHub()
    notifier = await _make_notifier(hub, enabled=False)
    try:
        assert notifier.capable is False
        hub.publish(
            "status_changed",
            {"task_id": 1, "session": "main", "from": "working", "to": "waiting-approval", "reason": "sudo gate"},
        )
        await asyncio.sleep(0.1)
        assert not calls_log.exists() or calls_log.read_text() == ""
        assert notifier.snapshot() == {
            1: {"tier": "interrupt", "status": "waiting-approval", "reason": "sudo gate", "session": "main"}
        }
    finally:
        await notifier.stop()


async def test_clear_task_drops_entry_and_broadcasts(fake_notify_send) -> None:
    """Cleanup/purge path (merge-poll dogfood finding): a discarded session
    emits no further status_changed, so the notifier needs the explicit
    clear — otherwise the entry outlives the task forever."""
    hub = EventHub()
    queue = hub.subscribe()
    notifier = await _make_notifier(hub)
    try:
        hub.publish(
            "status_changed",
            {"task_id": 3, "session": "main", "from": "idle", "to": "failed", "reason": "process exited with code 1"},
        )
        await _wait_until(lambda: 3 in notifier.snapshot())

        notifier.clear_task(3)

        assert 3 not in notifier.snapshot()
        async with asyncio.timeout(5):
            while True:
                event = await queue.get()
                if event.type == "attention_cleared":
                    assert event.payload == {"task_id": 3}
                    return
    finally:
        await notifier.stop()


async def test_workflow_gate_wait_raises_notify_attention(fake_notify_send) -> None:
    """A `workflow_step` gate wait is a task-level attention source with no
    session (workflow-engine D-7): notify tier, `session: None`, and the
    gate's message as the reason."""
    _, calls_log = fake_notify_send
    hub = EventHub()
    queue = hub.subscribe()
    notifier = await _make_notifier(hub)
    try:
        hub.publish(
            "workflow_step",
            {
                "task_id": 5,
                "step": "review-gate",
                "kind": "gate",
                "session": None,
                "status": "waiting",
                "message": "waiting for gate approval",
            },
        )
        async with asyncio.timeout(5):
            while True:
                event = await queue.get()
                if event.type == "attention":
                    break
        assert event.payload == {
            "task_id": 5,
            "tier": "notify",
            "status": "waiting",
            "reason": "waiting for gate approval",
            "session": None,
        }
        assert notifier.snapshot() == {
            5: {
                "tier": "notify",
                "status": "waiting",
                "reason": "waiting for gate approval",
                "session": None,
            }
        }
        # Notify tier fires a real desktop notification too.
        await _wait_until(lambda: calls_log.exists() and calls_log.read_text())
        assert "--urgency normal" in calls_log.read_text()
    finally:
        await notifier.stop()


async def test_workflow_gate_resolution_clears_attention(fake_notify_send) -> None:
    """The gate finishing (`ok`/`failed`) clears the None-sourced gate entry."""
    hub = EventHub()
    queue = hub.subscribe()
    notifier = await _make_notifier(hub)
    try:
        hub.publish(
            "workflow_step",
            {
                "task_id": 5,
                "step": "review-gate",
                "kind": "gate",
                "session": None,
                "status": "waiting",
                "message": "waiting for gate approval",
            },
        )
        await _wait_until(lambda: 5 in notifier.snapshot())

        hub.publish(
            "workflow_step",
            {"task_id": 5, "step": "review-gate", "kind": "gate", "session": None, "status": "ok"},
        )
        async with asyncio.timeout(5):
            while True:
                event = await queue.get()
                if event.type == "attention_cleared":
                    break
        assert event.payload == {"task_id": 5}
        assert notifier.snapshot() == {}
    finally:
        await notifier.stop()


async def test_cross_session_worst_tier_wins(fake_notify_send) -> None:
    """One published entry per task = the worst tier across the task's
    session sources (workflow-engine D-7): an interrupt-tier failure in one
    session supersedes a notify-tier wait in another; a lower-tier source
    changing underneath the published entry must not re-fire; and when the
    worst source clears, the worst remaining source becomes the entry."""
    _, calls_log = fake_notify_send
    hub = EventHub()
    # Long re-notify interval: this test asserts exact notification counts.
    notifier = await _make_notifier(hub, renotify_interval=60)
    try:
        hub.publish(
            "status_changed",
            {"task_id": 1, "session": "main", "from": "working", "to": "waiting-input", "reason": "pending question"},
        )
        await _wait_until(lambda: calls_log.exists() and len(calls_log.read_text().splitlines()) >= 1)
        assert notifier.snapshot() == {
            1: {"tier": "notify", "status": "waiting-input", "reason": "pending question", "session": "main"}
        }

        # A second session failing (interrupt) supersedes the published entry.
        hub.publish(
            "status_changed",
            {"task_id": 1, "session": "review", "from": "working", "to": "failed", "reason": "process exited with code 1"},
        )
        await _wait_until(lambda: len(calls_log.read_text().splitlines()) >= 2)
        calls = calls_log.read_text().splitlines()
        assert "--urgency normal" in calls[0]
        assert "--urgency critical" in calls[-1]
        assert notifier.snapshot() == {
            1: {"tier": "interrupt", "status": "failed", "reason": "process exited with code 1", "session": "review"}
        }

        # The lower-tier source moving underneath (still notify tier) must
        # NOT re-fire: the published interrupt entry is unchanged.
        hub.publish(
            "status_changed",
            {"task_id": 1, "session": "main", "from": "waiting-input", "to": "stalled", "reason": "no frames for 300s"},
        )
        await asyncio.sleep(0.1)
        assert len(calls_log.read_text().splitlines()) == 2
        assert notifier.snapshot()[1]["status"] == "failed"

        # The interrupt source superseded away (retried back to working): the
        # worst remaining source — main's notify-tier stall — is published.
        hub.publish(
            "status_changed",
            {"task_id": 1, "session": "review", "from": "failed", "to": "working", "reason": "operator retried"},
        )
        await _wait_until(lambda: len(calls_log.read_text().splitlines()) >= 3)
        assert "--urgency normal" in calls_log.read_text().splitlines()[-1]
        assert notifier.snapshot() == {
            1: {"tier": "notify", "status": "stalled", "reason": "no frames for 300s", "session": "main"}
        }
    finally:
        await notifier.stop()
