"""Desktop attention notifier (SPEC Decision 4; design D-1/D-2/D-3/D-7).

Architecture: ADR-0012
(docs/adr/0012-derive-attention-centrally-from-session-state.md)

`tier_for` maps every session status to its attention tier in one place
(design D-1: the session state machine itself carries no tier knowledge).
`AttentionNotifier` subscribes to the event hub's `status_changed` stream and,
on each transition into/out of the `notify`/`interrupt` tiers, fires (and ages,
and supersedes) a `notify-send` desktop notification with a single Open
action, and broadcasts `attention` / `attention_cleared` events that drive the
frontend badge (design D-7) independent of whether notifications themselves
are working.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
from dataclasses import dataclass
from typing import Any

from ompire_daemon.events import EventHub

logger = logging.getLogger(__name__)

_TIER_BY_STATUS: dict[str, str] = {
    "starting": "silent",
    "working": "silent",
    "idle": "badge",
    "retrying": "badge",
    "waiting-input": "notify",
    "stalled": "notify",
    # Reserved (SPEC Decision 4): not produced until the review chunk (#12)
    # introduces the `reviewing` state.
    "reviewing": "notify",
    "waiting-approval": "interrupt",
    "failed": "interrupt",
}

_NOTIFYING_TIERS = frozenset({"notify", "interrupt"})
_ATTENTION_TIERS = frozenset({"badge", "notify", "interrupt"})

# Default per-tier prefs match Settings.dc.html / design D-1.
_DEFAULT_PREFS: dict[str, dict[str, bool]] = {
    "interrupt": {"desktop": True, "sound": True, "badge": True},
    "notify": {"desktop": True, "sound": False, "badge": True},
    "badge": {"desktop": False, "sound": False, "badge": True},
    "silent": {"desktop": False, "sound": False, "badge": False},
}

_NOTIFY_SEND_HELP_TIMEOUT = 5.0


def tier_for(status: str) -> str:
    """The SPEC Decision 4 attention tier for a session status; unknown
    statuses default to `silent` (fail closed, never over-notify)."""
    return _TIER_BY_STATUS.get(status, "silent")


@dataclass(frozen=True)
class AttentionEntry:
    tier: str
    status: str
    reason: str
    # The session that raised the entry; None for workflow-gate waits
    # (workflow-engine design D-7: attention stays one entry per task).
    session: str | None = None


# Worst tier wins when a task's sessions disagree (workflow-engine D-7).
_TIER_RANK = {"silent": 0, "badge": 1, "notify": 2, "interrupt": 3}


class AttentionNotifier:
    """One instance per daemon: no per-task state lives on the session
    tracker (design D-1). Not started until `probe()` + `start()` are called
    (app lifespan); `snapshot()`/tier classification work regardless."""

    def __init__(
        self,
        hub: EventHub,
        *,
        bind: str,
        port: int,
        renotify_interval: float,
        enabled: bool,
    ) -> None:
        self._hub = hub
        self._bind = bind
        self._port = port
        self._renotify_interval = renotify_interval
        self._enabled = enabled
        self._capable = False
        self._actions_supported = True
        self._prefs = {tier: dict(channels) for tier, channels in _DEFAULT_PREFS.items()}
        # The published task-level entry (worst tier across sources) and the
        # per-source entries it aggregates: task → source → entry, where the
        # source is the session name, or None for a workflow-gate wait.
        self._entries: dict[int, AttentionEntry] = {}
        self._source_entries: dict[int, dict[str | None, AttentionEntry]] = {}
        self._notify_tasks: dict[int, asyncio.Task] = {}
        self._renotify_timers: dict[int, asyncio.Task] = {}
        self._queue: asyncio.Queue | None = None
        self._run_task: asyncio.Task | None = None

    @property
    def capable(self) -> bool:
        """Whether desktop notifications will actually fire (probed once at
        startup); attention events broadcast regardless."""
        return self._capable

    @property
    def actions_supported(self) -> bool:
        """Whether the notification *server* advertises the `actions`
        capability, i.e. whether the Open button/click will do anything.
        `capable` can be true while this is false — the notification still
        fires, just without a working Open action (confirmed live-dogfooding
        2026-07-21: stock GNOME's notification daemon does not advertise
        `actions` for the legacy `org.freedesktop.Notifications` interface
        that `notify-send` uses — interactive actions are effectively
        reserved for apps going through the XDG Desktop Portal instead;
        that's why a browser's own notifications can have clickable buttons
        while a raw `notify-send` call can't)."""
        return self._actions_supported

    def snapshot(self) -> dict[int, dict[str, Any]]:
        """Active attention entries for the WS snapshot (design: a
        reconnecting client sees them without replaying events)."""
        return {
            task_id: {
                "tier": entry.tier,
                "status": entry.status,
                "reason": entry.reason,
                "session": entry.session,
            }
            for task_id, entry in self._entries.items()
        }

    async def probe(self) -> None:
        """Startup capability probe (Risks/Trade-offs): binary presence,
        `--action` support, and a reachable D-Bus session bus. Never raises —
        any failure logs one actionable warning and leaves the notifier
        degraded to attention-events-only."""
        if not self._enabled:
            logger.info("desktop notifications disabled (notifications_enabled=false)")
            self._capable = False
            return

        binary = shutil.which("notify-send")
        if binary is None:
            logger.warning(
                "notify-send not found on PATH; desktop notifications disabled, "
                "badge counts still work"
            )
            self._capable = False
            return

        if not _dbus_session_bus_reachable():
            logger.warning(
                "no D-Bus session bus reachable for notify-send; desktop notifications "
                "disabled. If this is a `systemctl --user` service, try: "
                "systemctl --user import-environment DBUS_SESSION_BUS_ADDRESS"
            )
            self._capable = False
            return

        try:
            process = await asyncio.create_subprocess_exec(
                binary,
                "--help",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(
                process.communicate(), timeout=_NOTIFY_SEND_HELP_TIMEOUT
            )
        except (OSError, TimeoutError) as exc:
            logger.warning(
                "notify-send capability probe failed (%s); desktop notifications disabled", exc
            )
            self._capable = False
            return

        if b"--action" not in stdout:
            logger.warning(
                "notify-send does not support --action (no Open button); desktop "
                "notifications disabled"
            )
            self._capable = False
            return

        self._capable = True

        # notify-send supporting the `--action` flag only means the *client*
        # tool knows the syntax — it silently downgrades to a non-interactive
        # notification (printing a warning notify-send itself emits, not the
        # daemon) if the connected notification *server* doesn't advertise
        # the capability. Query the server directly rather than assuming.
        server_supports_actions = await _server_supports_actions()
        if server_supports_actions is False:
            logger.warning(
                "the notification server does not advertise the 'actions' capability "
                "(GetCapabilities); desktop notifications will fire without a working "
                "Open action. This is expected on stock GNOME (interactive notification "
                "actions are effectively reserved for apps using the XDG Desktop Portal); "
                "the 'N need you' badge/favicon remain the reliable way to jump to a task."
            )
            self._actions_supported = False

    def start(self) -> None:
        self._queue = self._hub.subscribe()
        self._run_task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Clean shutdown: stop consuming events and cancel every outstanding
        notification subprocess and re-notify timer."""
        if self._run_task is not None:
            self._run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._run_task
            self._run_task = None
        if self._queue is not None:
            self._hub.unsubscribe(self._queue)
            self._queue = None
        for task_id in list(self._notify_tasks):
            self._cancel_notification(task_id)
        for task_id in list(self._renotify_timers):
            self._cancel_renotify(task_id)

    async def _run(self) -> None:
        assert self._queue is not None
        while True:
            event = await self._queue.get()
            if event.type == "status_changed":
                self._on_status_changed(event.payload)
            elif event.type == "workflow_step":
                self._on_workflow_step(event.payload)

    def _on_status_changed(self, payload: dict[str, Any]) -> None:
        task_id = payload.get("task_id")
        session = payload.get("session")
        status = payload.get("to")
        reason = payload.get("reason", "")
        if (
            not isinstance(task_id, int)
            or not isinstance(session, str)
            or not isinstance(status, str)
        ):
            return
        tier = tier_for(status)
        if tier in _ATTENTION_TIERS:
            self._set_source(
                task_id, session, AttentionEntry(tier, status, str(reason), session)
            )
        else:
            self._set_source(task_id, session, None)

    def _on_workflow_step(self, payload: dict[str, Any]) -> None:
        """Gate waits classify `notify` (workflow-engine design D-7), sourced
        under None (no session); the gate finishing clears that source."""
        task_id = payload.get("task_id")
        status = payload.get("status")
        if not isinstance(task_id, int) or not isinstance(status, str):
            return
        if status == "waiting":
            message = payload.get("message")
            self._set_source(
                task_id,
                None,
                AttentionEntry("notify", "waiting", str(message or "workflow gate"), None),
            )
        else:
            self._set_source(task_id, None, None)

    def _set_source(
        self, task_id: int, source: str | None, entry: AttentionEntry | None
    ) -> None:
        """Update one source's entry, then re-aggregate the task's published
        entry (worst tier wins) and fire/clear on change only — a session
        moving beneath the current worst tier must not re-fire the toast."""
        sources = self._source_entries.setdefault(task_id, {})
        if entry is None:
            sources.pop(source, None)
        else:
            sources[source] = entry
        worst = max(sources.values(), key=lambda e: _TIER_RANK[e.tier], default=None)
        if worst is None:
            self._source_entries.pop(task_id, None)
            self._leave(task_id)
            return
        if self._entries.get(task_id) == worst:
            return
        self._enter(task_id, worst)

    def _enter(self, task_id: int, entry: AttentionEntry) -> None:
        # Any change to the published entry supersedes whatever notification
        # was previously active for this task (design: "one active per task").
        self._cancel_notification(task_id)
        self._cancel_renotify(task_id)
        self._entries[task_id] = entry
        self._hub.publish(
            "attention",
            {
                "task_id": task_id,
                "tier": entry.tier,
                "status": entry.status,
                "reason": entry.reason,
                "session": entry.session,
            },
        )
        if not self._capable:
            return
        if self._tier_pref(entry.tier, "desktop"):
            self._fire(task_id, entry.tier, entry.status, entry.reason)
            if self._renotify_interval > 0:
                self._arm_renotify(task_id, entry.tier, entry.status, entry.reason)

    def _leave(self, task_id: int) -> None:
        had_entry = self._entries.pop(task_id, None) is not None
        self._cancel_notification(task_id)
        self._cancel_renotify(task_id)
        if had_entry:
            self._hub.publish("attention_cleared", {"task_id": task_id})

    def clear_task(self, task_id: int) -> None:
        """Drop any attention entry on task cleanup/purge. A discarded session
        emits no further `status_changed` (the tracker ignores late events),
        so without this the entry — e.g. the interrupt-tier `failed` the agent
        exit races in during workshop removal — would demand attention forever.
        """
        self._source_entries.pop(task_id, None)
        self._leave(task_id)

    def _fire(self, task_id: int, tier: str, status: str, reason: str) -> None:
        task = asyncio.create_task(self._notify_send(task_id, tier, status, reason))
        self._notify_tasks[task_id] = task
        task.add_done_callback(
            lambda t: self._notify_tasks.pop(task_id, None)
            if self._notify_tasks.get(task_id) is t
            else None
        )

    def _tier_pref(self, tier: str, channel: str) -> bool:
        return self._prefs.get(tier, {}).get(channel, _DEFAULT_PREFS.get(tier, {}).get(channel, False))

    def apply_settings(self, settings: dict[str, Any]) -> None:
        """Update live prefs and re-arm outstanding re-notify timers.

        A new non-zero interval re-arms every pending timer from now. An
        interval of zero cancels them outright. Desktop-pref changes stop or
        start re-notification for tasks in the affected tier without firing
        immediately."""
        for tier in ("interrupt", "notify", "badge", "silent"):
            for channel in ("desktop", "sound", "badge"):
                self._prefs[tier][channel] = bool(
                    settings.get(f"tier.{tier}.{channel}", _DEFAULT_PREFS[tier][channel])
                )
        self._renotify_interval = float(settings.get("renotify_interval", self._renotify_interval))

        for task_id, entry in list(self._entries.items()):
            if self._renotify_interval <= 0 or not self._tier_pref(entry.tier, "desktop"):
                self._cancel_renotify(task_id)
            else:
                self._cancel_renotify(task_id)
                self._arm_renotify(task_id, entry.tier, entry.status, entry.reason)

    async def _notify_send(self, task_id: int, tier: str, status: str, reason: str) -> None:
        urgency = "critical" if self._tier_pref(tier, "sound") else "normal"
        summary = f"ompire task {task_id}: {status}"

        if not self._actions_supported:
            # The server doesn't advertise `actions` (confirmed via
            # GetCapabilities at startup, common on stock GNOME): firing
            # `--wait --action` here would just have notify-send print its
            # own "Actions are not supported" warning and display plain
            # anyway. Skip straight to a plain, non-blocking notification —
            # still a visible signal, just not clickable.
            try:
                await asyncio.create_subprocess_exec(
                    "notify-send",
                    "--urgency",
                    urgency,
                    summary,
                    reason,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            except OSError as exc:
                logger.warning("failed to launch notify-send for task %d: %s", task_id, exc)
            return

        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                "notify-send",
                "--wait",
                "--urgency",
                urgency,
                # `default` is the action id most notification servers treat
                # as "clicking the notification body itself" (not just a
                # rendered button) — confirmed live-dogfooding 2026-07-20:
                # naming it `open` fired only when a distinct button was
                # clicked, not on a body click, which is how operators
                # naturally interact with a toast.
                "--action",
                "default=Open",
                summary,
                reason,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await process.communicate()
        except asyncio.CancelledError:
            if process is not None and process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
            raise
        except OSError as exc:
            logger.warning("failed to launch notify-send for task %d: %s", task_id, exc)
            return
        action = stdout.strip()
        if action == b"default":
            await self._open_task(task_id)
        elif action:
            logger.info(
                "notify-send for task %d reported unrecognized action %r; not opening",
                task_id,
                action,
            )

    async def _open_task(self, task_id: int) -> None:
        """Invoking Open focuses the task's view (SPEC: launch the task's
        URL; no approve/answer action — that stays in the UI)."""
        url = f"http://{self._bind}:{self._port}/tasks/{task_id}"
        try:
            process = await asyncio.create_subprocess_exec(
                "xdg-open",
                url,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await process.communicate()
        except OSError as exc:
            logger.warning("failed to launch xdg-open for task %d: %s", task_id, exc)
            return
        if process.returncode != 0:
            logger.warning(
                "xdg-open exited %d for task %d (url=%s): %s",
                process.returncode,
                task_id,
                url,
                stderr.decode("utf-8", errors="replace").strip(),
            )

    def _cancel_notification(self, task_id: int) -> None:
        task = self._notify_tasks.pop(task_id, None)
        if task is not None:
            task.cancel()

    def _arm_renotify(self, task_id: int, tier: str, status: str, reason: str) -> None:
        task = asyncio.create_task(self._renotify_loop(task_id, tier, status, reason))
        self._renotify_timers[task_id] = task

    async def _renotify_loop(self, task_id: int, tier: str, status: str, reason: str) -> None:
        """Ages an unanswered attention entry (design D-3): re-fires on
        `renotify_interval` and re-arms itself, until a tier transition
        cancels it (answering, the turn moving, or the child exiting)."""
        await asyncio.sleep(self._renotify_interval)
        # Desktop pref may have been disabled while we slept; don't fire or
        # re-arm if the tier no longer warrants desktop notifications.
        if not self._tier_pref(tier, "desktop"):
            return
        self._fire(task_id, tier, status, reason)
        if self._renotify_interval > 0:
            self._arm_renotify(task_id, tier, status, reason)

    def _cancel_renotify(self, task_id: int) -> None:
        task = self._renotify_timers.pop(task_id, None)
        if task is not None:
            task.cancel()


def _dbus_session_bus_reachable() -> bool:
    if os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        return True
    return os.path.exists(f"/run/user/{os.getuid()}/bus")


_GET_CAPABILITIES_TIMEOUT = 5.0


async def _server_supports_actions() -> bool | None:
    """Queries the real notification server's `GetCapabilities` directly
    (via `gdbus`, present alongside D-Bus on essentially any Linux desktop),
    rather than inferring support from the `notify-send` client tool.
    Returns `None` (indeterminate — e.g. `gdbus` missing, or no notification
    service registered at all, common in headless/CI environments) when the
    query itself can't be completed; the caller treats that as "assume
    supported" so this never disables a working setup it can't inspect."""
    try:
        process = await asyncio.create_subprocess_exec(
            "gdbus",
            "call",
            "--session",
            "--dest",
            "org.freedesktop.Notifications",
            "--object-path",
            "/org/freedesktop/Notifications",
            "--method",
            "org.freedesktop.Notifications.GetCapabilities",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(
            process.communicate(), timeout=_GET_CAPABILITIES_TIMEOUT
        )
    except (OSError, TimeoutError):
        return None
    if process.returncode != 0:
        return None
    # gdbus prints the tuple-of-strings result as e.g.
    # (['body', 'body-markup', 'actions', ...],) — a quoted substring match
    # is sufficient and avoids depending on a GVariant parser.
    return b"'actions'" in stdout
