"""Desktop attention notifier (SPEC Decision 4; design D-1/D-2/D-3/D-7).

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
        self._entries: dict[int, AttentionEntry] = {}
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
            task_id: {"tier": entry.tier, "status": entry.status, "reason": entry.reason}
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

    def _on_status_changed(self, payload: dict[str, Any]) -> None:
        task_id = payload.get("task_id")
        status = payload.get("to")
        reason = payload.get("reason", "")
        if not isinstance(task_id, int) or not isinstance(status, str):
            return
        tier = tier_for(status)
        if tier in _NOTIFYING_TIERS:
            self._enter(task_id, tier, status, str(reason))
        else:
            self._leave(task_id)

    def _enter(self, task_id: int, tier: str, status: str, reason: str) -> None:
        # Any transition into notify/interrupt supersedes whatever notification
        # was previously active for this task (design: "one active per task").
        self._cancel_notification(task_id)
        self._cancel_renotify(task_id)
        self._entries[task_id] = AttentionEntry(tier=tier, status=status, reason=reason)
        self._hub.publish(
            "attention", {"task_id": task_id, "tier": tier, "status": status, "reason": reason}
        )
        if self._capable:
            self._fire(task_id, tier, status, reason)
            self._arm_renotify(task_id, tier, status, reason)

    def _leave(self, task_id: int) -> None:
        had_entry = self._entries.pop(task_id, None) is not None
        self._cancel_notification(task_id)
        self._cancel_renotify(task_id)
        if had_entry:
            self._hub.publish("attention_cleared", {"task_id": task_id})

    def _fire(self, task_id: int, tier: str, status: str, reason: str) -> None:
        task = asyncio.create_task(self._notify_send(task_id, tier, status, reason))
        self._notify_tasks[task_id] = task
        task.add_done_callback(
            lambda t: self._notify_tasks.pop(task_id, None)
            if self._notify_tasks.get(task_id) is t
            else None
        )

    async def _notify_send(self, task_id: int, tier: str, status: str, reason: str) -> None:
        urgency = "critical" if tier == "interrupt" else "normal"
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
        self._fire(task_id, tier, status, reason)
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
