"""Per-session status state machine (SPEC Decision 4, core subset + D4
ask/approval extension).

`SessionTracker` owns `{status, reason, since}` per `(task_id, session)` —
a task runs one omp child per workflow-declared named session
(workflow-engine design D-1). It is fed by (a) lifecycle calls from the
supervisor and workflow engine (spawned, exited, start failures) and (b) a
subscriber queue on each agent's event fan-out for `agent_start` /
`agent_end` and, since the `ask-approvals` capability, `tool_execution_start`
/ `tool_execution_end` / `extension_ui_request` frames (design D-1). Every
transition goes through one guarded method that re-checks the current status,
so races (exit during the idle debounce, late frames after discard) resolve
deterministically: exit wins.

Status is in-memory only and independent of live handles — `failed` outlives
the child's deregistration; entries are dropped on task cleanup/purge. A
`SessionTracker` itself does not survive a daemon restart (it's a fresh
object), but the crash-recovery capability re-establishes status for
recovered sessions at startup: `recovering` seeds a never-tracked session
`starting`, and `session_recovered`/`recovery_failed` drive it to `idle` or
`failed` once the resumed agent is ready or fails to start.

The `reviewing` status is driven by the `ReviewManager`, not by agent frames:
it enters only from `idle` on the task's *primary* session when the daemon
opens an llmvet review, and leaves to `idle` on approval/abort/error or to
`working` when a comment prompt is looped back to the agent. Like other
session statuses, it does not survive a daemon restart; a crash mid-review is
recovered via the git ref the review manager leaves in the clone.

A pending `ask`/approval question rides alongside `SessionInfo` in a parallel
`_pending` map (design D-1: "parallel map, not enriched rows"), so the
transition machinery and the pending-question machinery stay decoupled.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ompire_daemon.events import EventHub

if TYPE_CHECKING:
    from ompire_daemon.agent import AgentHandle

logger = logging.getLogger(__name__)

SESSION_STATUSES = (
    "starting",
    "working",
    "idle",
    "failed",
    "waiting-input",
    "waiting-approval",
    "stalled",
    "retrying",
    "reviewing",
)

DEFAULT_STALL_THRESHOLD = 300.0

_APPROVAL_OPTIONS = ["Approve", "Deny"]


async def wait_for_idle(
    hub: EventHub, task_id: int, session: str, timeout: float
) -> None:
    """Wait for the session's debounced idle transition (the turn boundary,
    per the session-states capability), watching `status_changed` events on
    the hub. Generalized from the ship draft's wait (workflow-engine design
    D-2: agent steps complete here). Raises TimeoutError on expiry."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    queue = hub.subscribe()
    try:
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            try:
                event = await asyncio.wait_for(queue.get(), timeout=remaining)
            except TimeoutError:
                raise TimeoutError from None
            if event.type != "status_changed":
                continue
            payload = event.payload
            if (
                payload.get("task_id") == task_id
                and payload.get("session") == session
                and payload.get("to") == "idle"
            ):
                return
    finally:
        hub.unsubscribe(queue)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class SessionInfo:
    status: str
    reason: str
    since: str


@dataclass
class PendingOption:
    value: str
    label: str
    description: str | None = None


@dataclass
class PendingAskQuestion:
    prompt: str
    options: list[PendingOption]
    multi: bool
    recommended: str | None
    allowsOther: bool


@dataclass
class PendingQuestion:
    """Normalized pending-question payload (design D-4). `kind` is `"ask"` or
    `"approval"`; `questions` is populated for asks (a fallback question from
    a failed approval cross-check carries an empty list — the frame's own
    text is not parsed, see D-2)."""

    id: str
    kind: str
    questions: list[PendingAskQuestion] = field(default_factory=list)


#: Exact suffix omp's `ask` tool appends to the recommended option's label
#: before sending it as a `select` dialog (`RECOMMENDED_SUFFIX` in
#: packages/coding-agent/src/tools/ask.ts) — confirmed against the omp source
#: during dogfooding 2026-07-20. The reply's `value` must echo this back
#: verbatim for the recommended option, so `PendingOption.value` bakes it in
#: rather than the plain label (which stays in `.label` for display).
_RECOMMENDED_SUFFIX = " (Recommended)"


def _build_ask_pending(request_id: str, args: dict[str, Any]) -> PendingQuestion:
    """Build the normalized payload from the `ask` tool's stashed
    `tool_execution_start` args (design D-2/D-3): opaque args in, typed
    structure out, so the frontend renders from one reviewed shape.

    Field names and shape confirmed against the omp source during dogfooding
    2026-07-20 (see the `omp-rpc-field-assumptions` memory note):
    `args.questions[].question` (not `prompt`), `.options[].label` with no
    separate `value` — the wire reply's `value` must be the exact label omp's
    `select` dialog showed (`stripRecommendedSuffix`/`choice` comparison in
    `ask.ts`), so the recommended option's `value` carries the
    `_RECOMMENDED_SUFFIX` while `label` stays plain for display.
    `.recommended` is an *index* into `options` (not a value). There is no
    `multi` or `allowsOther` key on this single-select example — `multi`
    defaults false (the wire protocol's `select` reply carries only one
    `value` regardless: real multi-select isn't exposed over rpc-ui mode, see
    `askSingleQuestion`'s plain-select branch); `allowsOther` defaults true,
    since the corresponding `extension_ui_request.options` always carried a
    synthesized "Other (type your own)" entry even though no flag said so —
    and arbitrary free text is accepted as `value` with no membership check
    against the offered options, so no separate multi-step flow is needed."""
    questions: list[PendingAskQuestion] = []
    raw_questions = args.get("questions") if isinstance(args, dict) else None
    for raw in raw_questions if isinstance(raw_questions, list) else []:
        if not isinstance(raw, dict):
            continue
        raw_options = raw.get("options")
        recommended_index = raw.get("recommended")
        options = []
        for i, o in enumerate(raw_options if isinstance(raw_options, list) else []):
            if not isinstance(o, dict):
                continue
            label = str(o.get("label", ""))
            is_recommended = isinstance(recommended_index, int) and i == recommended_index
            value = f"{label}{_RECOMMENDED_SUFFIX}" if is_recommended else label
            options.append(
                PendingOption(
                    value=value,
                    label=label,
                    description=o.get("description") if isinstance(o.get("description"), str) else None,
                )
            )
        recommended = (
            options[recommended_index].value
            if isinstance(recommended_index, int) and 0 <= recommended_index < len(options)
            else None
        )
        questions.append(
            PendingAskQuestion(
                prompt=str(raw.get("question", "")),
                options=options,
                multi=bool(raw.get("multi", False)),
                recommended=recommended,
                allowsOther=bool(raw.get("allowsOther", raw.get("allowOther", True))),
            )
        )
    return PendingQuestion(id=request_id, kind="ask", questions=questions)


class SessionTracker:
    def __init__(
        self, hub: EventHub, idle_debounce: float, stall_threshold: float = DEFAULT_STALL_THRESHOLD
    ) -> None:
        self._hub = hub
        self._idle_debounce = idle_debounce
        self._stall_threshold = stall_threshold
        # Everything is keyed (task_id, session) — a task runs one omp child
        # per named session (workflow-engine design D-1).
        self._sessions: dict[tuple[int, str], SessionInfo] = {}
        self._watchers: dict[tuple[int, str], asyncio.Task] = {}
        self._debounces: dict[tuple[int, str], asyncio.Task] = {}
        self._watchdogs: dict[tuple[int, str], asyncio.Task] = {}
        self._last_frame_at: dict[tuple[int, str], float] = {}
        self._operator_stops: set[tuple[int, str]] = set()
        self._pending: dict[tuple[int, str], PendingQuestion] = {}
        # In-flight tool executions per session: toolCallId -> tool name.
        self._inflight_tools: dict[tuple[int, str], dict[str, str]] = {}
        # The current in-flight `ask` execution per session (at most one,
        # since `ask` declares `concurrency: "exclusive"`): (toolCallId, args).
        self._inflight_asks: dict[tuple[int, str], tuple[str, dict[str, Any]]] = {}
        # Turn-boundary/idle-entry observers (design D-5: "hooking the
        # existing debounce path or subscribing to the watcher") — the
        # advisory sampler registers here so the tracker itself stays a pure
        # classifier with no advisory/tier knowledge (mirrors design D-1).
        self._turn_end_hooks: list[Callable[[int, str, AgentHandle], Awaitable[None]]] = []
        self._idle_entered_hooks: list[Callable[[int, str, AgentHandle], Awaitable[None]]] = []

    def set_stall_threshold(self, seconds: float) -> None:
        """Update the stall watchdog threshold. Applies only to timers armed
        after this call; timers already sleeping keep their original deadline
        (design D-2)."""
        self._stall_threshold = seconds

    def add_turn_end_hook(self, hook: Callable[[int, str, AgentHandle], Awaitable[None]]) -> None:
        self._turn_end_hooks.append(hook)

    def add_idle_entered_hook(self, hook: Callable[[int, str, AgentHandle], Awaitable[None]]) -> None:
        self._idle_entered_hooks.append(hook)

    def _fire_hooks(
        self,
        hooks: list[Callable[[int, str, AgentHandle], Awaitable[None]]],
        task_id: int,
        session: str,
        handle: AgentHandle,
    ) -> None:
        for hook in hooks:
            task = asyncio.create_task(hook(task_id, session, handle))
            task.add_done_callback(self._log_hook_failure)

    @staticmethod
    def _log_hook_failure(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.error("session tracker observer hook failed", exc_info=task.exception())

    def get(self, task_id: int, session: str) -> SessionInfo | None:
        return self._sessions.get((task_id, session))

    def pending(self, task_id: int, session: str) -> PendingQuestion | None:
        return self._pending.get((task_id, session))

    def task_sessions(self, task_id: int) -> dict[str, SessionInfo]:
        """All tracked sessions of one task (session name → info)."""
        return {
            session: info
            for (tid, session), info in self._sessions.items()
            if tid == task_id
        }

    def snapshot(self) -> dict[int, dict[str, dict[str, Any]]]:
        """Current statuses for the WS snapshot's `sessions` map, nested
        task → session (workflow-engine design D-7), each entry carrying the
        pending question (if any) under `question`."""
        result: dict[int, dict[str, dict[str, Any]]] = {}
        for (task_id, session), info in self._sessions.items():
            entry = asdict(info)
            pending = self._pending.get((task_id, session))
            if pending is not None:
                entry["question"] = asdict(pending)
            result.setdefault(task_id, {})[session] = entry
        return result

    # --- lifecycle transitions (supervisor / spawn pipeline / stop path) ------

    def agent_spawning(self, task_id: int, session: str) -> None:
        """The agent child is being spawned; covers the ready handshake too."""
        self._transition(task_id, session, "starting", "agent spawned")

    def watch(self, task_id: int, session: str, handle: AgentHandle) -> None:
        """Subscribe to the agent's fan-out and drive frame transitions."""
        key = (task_id, session)
        self.unwatch(task_id, session)
        queue = handle.subscribe()
        watcher = asyncio.create_task(self._watch_events(task_id, session, handle, queue))
        self._watchers[key] = watcher
        watcher.add_done_callback(lambda t: self._pop_if_current(self._watchers, key, t))

    def agent_exited(self, task_id: int, session: str, exit_code: int) -> None:
        """Any child exit lands `failed` (design D-2); the reason distinguishes
        an operator stop from a crash. Exit wins any pending idle debounce,
        any pending stall watchdog/retry (design D-4), and any pending
        question (D-6: discarded, not resolved — `failed` wins)."""
        key = (task_id, session)
        self._cancel_debounce(key)
        self._cancel_watchdog(key)
        self.clear_pending(task_id, session, broadcast=False)
        self._inflight_tools.pop(key, None)
        if key in self._operator_stops:
            self._operator_stops.discard(key)
            reason = "stopped by operator"
        else:
            reason = f"process exited with code {exit_code}"
        self._transition(task_id, session, "failed", reason)

    def expect_operator_stop(self, task_id: int, session: str) -> None:
        self._operator_stops.add((task_id, session))

    def clear_operator_stop(self, task_id: int, session: str) -> None:
        self._operator_stops.discard((task_id, session))

    def session_start_failed(self, task_id: int, session: str, reason: str) -> None:
        """The session's agent could not be started (workflow-engine lazy
        spawn, or a spawn-pipeline failure before/around it)."""
        self._transition(task_id, session, "failed", reason)

    def prompt_skipped(self, task_id: int, session: str) -> None:
        """Empty stored prompt: ready → idle instead of hanging in starting."""
        self._transition(
            task_id, session, "idle", "ready, no prompt to send", allow_from={"starting"}
        )

    def recovering(self, task_id: int, session: str) -> None:
        """Startup recovery seeds a never-tracked session straight to
        `starting` (crash-recovery capability, design D-4/6.2): a fresh
        `SessionTracker` after a restart has no entry for this session yet,
        so this is the ordinary "never-tracked session can start" path
        `_transition` already allows. Painted before the first snapshot; the
        recovery job later drives it to `idle` (`session_recovered`) or
        `failed` (`recovery_failed`)."""
        self._transition(task_id, session, "starting", "recovering after daemon restart")

    def session_recovered(self, task_id: int, session: str) -> None:
        """A resumed agent reached ready (crash-recovery capability, design
        D-4/7.2): the in-flight turn is lost but the session is not, so land
        `idle` straight from the recovery `starting` seed rather than hanging
        (mirrors `prompt_skipped`)."""
        self._transition(
            task_id, session, "idle", "resumed after daemon restart", allow_from={"starting"}
        )
    def recovery_failed(self, task_id: int, session: str, reason: str) -> None:
        """The resumed agent could not be started (crash-recovery capability,
        design D-4/7.2)."""
        self._transition(task_id, session, "failed", reason)

    # --- review transitions (review capability) ------------------------------

    def review_opened(self, task_id: int, session: str, reason: str) -> None:
        """The daemon opened an llmvet review for the task's primary session."""
        self._transition(task_id, session, "reviewing", reason, allow_from={"idle"})

    def review_closed(self, task_id: int, session: str, reason: str) -> None:
        """The review finished (approved/aborted/error); return to `idle`."""
        self._transition(task_id, session, "idle", reason, allow_from={"reviewing"})

    def discard(self, task_id: int) -> None:
        """Drop every session entry on task cleanup/purge; late events cannot
        resurrect them. Task-scoped on purpose: cleanup tears down the whole
        task, all sessions included."""
        for key in [key for key in self._sessions if key[0] == task_id]:
            self._cancel_debounce(key)
            self._cancel_watchdog(key)
            self.unwatch(*key)
            self._operator_stops.discard(key)
            self._sessions.pop(key, None)
            self._pending.pop(key, None)
            self._inflight_tools.pop(key, None)
            self._inflight_asks.pop(key, None)
            self._last_frame_at.pop(key, None)

    def unwatch(self, task_id: int, session: str) -> None:
        watcher = self._watchers.pop((task_id, session), None)
        if watcher is not None:
            watcher.cancel()

    def clear_pending(
        self, task_id: int, session: str, *, broadcast: bool = True
    ) -> PendingQuestion | None:
        """Clear a session's pending question (design D-6): the `ask`
        execution ending, a turn boundary, an interrupt, or an exit.
        `broadcast=False` suppresses `question_resolved` when a process exit
        is about to broadcast `failed` instead (exit wins, no separate
        resolve)."""
        key = (task_id, session)
        pending = self._pending.pop(key, None)
        self._inflight_asks.pop(key, None)
        if pending is not None and broadcast:
            self._hub.publish(
                "question_resolved",
                {"task_id": task_id, "session": session, "question_id": pending.id},
            )
        return pending

    def answer_pending(self, task_id: int, session: str) -> PendingQuestion | None:
        """The operator answered the pending question (design D-5): clear it
        and return the session to `working` — called by the answer endpoint
        after it sends the reply. Distinct from `clear_pending` (which the
        frame watcher also uses for turn-movement clears that don't imply
        `working`, e.g. exit) because only an answer names its own reason and
        transition."""
        pending = self.clear_pending(task_id, session)
        if pending is not None:
            self._transition(
                task_id,
                session,
                "working",
                "operator answered the pending question",
                allow_from={"waiting-input", "waiting-approval"},
            )
        return pending

    # --- internals ------------------------------------------------------------

    def _transition(
        self,
        task_id: int,
        session: str,
        to: str,
        reason: str,
        *,
        allow_from: set[str] | None = None,
    ) -> None:
        """The single guarded transition: re-checks current status so competing
        transitions (exit vs. debounce vs. frames) can't clobber each other."""
        key = (task_id, session)
        current = self._sessions.get(key)
        from_status = current.status if current is not None else None
        if current is None and to != "starting":
            return  # discarded or never-tracked: late events are ignored
        if from_status == "failed":
            # Terminal within a process — nothing revives a `failed` entry in
            # place. Startup recovery (crash-recovery capability) doesn't
            # need to: a daemon restart means a brand-new `SessionTracker`
            # with no entry for the session yet, so `recovering()` seeds a
            # fresh `starting` entry instead of reviving this one.
            return
        if allow_from is not None and from_status not in allow_from:
            return
        info = SessionInfo(status=to, reason=reason, since=_now_iso())
        self._sessions[key] = info
        self._hub.publish(
            "status_changed",
            {
                "task_id": task_id,
                "session": session,
                "from": from_status,
                "to": to,
                "reason": reason,
            },
        )

    async def _watch_events(
        self, task_id: int, session: str, handle: AgentHandle, queue: asyncio.Queue
    ) -> None:
        # Local import: agent.py type-imports this module, so a module-level
        # import here would be circular.
        from ompire_daemon.agent import EVENT_STREAM_END

        key = (task_id, session)
        try:
            while True:
                event = await queue.get()
                if event is EVENT_STREAM_END:
                    return
                self._last_frame_at[key] = time.monotonic()
                # Any interpreted frame recovers a `stalled` session (design
                # D-4): `agent_start` and `auto_retry_*` already transition
                # out of `stalled` on their own via `allow_from`, so this only
                # needs to cover the other frame types.
                if event.type not in ("agent_start", "auto_retry_start", "auto_retry_end"):
                    self._recover_from_stall(task_id, session, event.type)
                if event.type == "agent_start":
                    self._cancel_debounce(key)
                    self._cancel_watchdog(key)
                    # A turn starting abandons any question left over from the
                    # previous one (design D-6); tool tracking resets too.
                    self.clear_pending(task_id, session)
                    self._inflight_tools.pop(key, None)
                    self._transition(task_id, session, "working", "agent_start frame")
                    self._arm_watchdog(key)
                elif event.type == "agent_end":
                    # Turn end cancels the stall watchdog (design D-4): the
                    # debounce, not the watchdog, owns the quiet period now.
                    self._cancel_watchdog(key)
                    self.clear_pending(task_id, session)
                    self._inflight_tools.pop(key, None)
                    self._start_debounce(key, handle)
                    # Turn-boundary observer hook (design D-5): the advisory
                    # sampler piggybacks here rather than a poll loop.
                    self._fire_hooks(self._turn_end_hooks, task_id, session, handle)
                elif event.type == "tool_execution_start":
                    self._on_tool_execution_start(task_id, session, event.payload)
                    self._sync_watchdog(key)
                elif event.type == "tool_execution_end":
                    self._on_tool_execution_end(task_id, session, event.payload)
                    self._sync_watchdog(key)
                elif event.type == "extension_ui_request":
                    self._on_extension_ui_request(task_id, session, event.payload)
                    self._sync_watchdog(key)
                elif event.type == "auto_retry_start":
                    self._on_auto_retry_start(task_id, session, event.payload)
                    self._cancel_watchdog(key)
                elif event.type == "auto_retry_end":
                    self._on_auto_retry_end(task_id, session, event.payload)
                    self._sync_watchdog(key)
        finally:
            handle.unsubscribe(queue)

    def _on_tool_execution_start(
        self, task_id: int, session: str, payload: dict[str, Any]
    ) -> None:
        # Field names confirmed against real omp during dogfooding 2026-07-20
        # (see the `omp-rpc-field-assumptions` memory note): `toolCallId` /
        # `toolName`, not `toolUseId` / `name`.
        key = (task_id, session)
        tool_call_id = payload.get("toolCallId")
        name = payload.get("toolName")
        if not isinstance(tool_call_id, str) or not isinstance(name, str):
            return
        self._inflight_tools.setdefault(key, {})[tool_call_id] = name
        if name != "ask":
            return
        args = payload.get("args")
        args = args if isinstance(args, dict) else {}
        self._inflight_asks[key] = (tool_call_id, args)
        # Also confirmed by dogfooding: real omp emits `extension_ui_request`
        # *before* `tool_execution_start` for `ask` — the reverse of what the
        # design assumed. If the request already arrived, it was provisionally
        # classified as a bare question (no `ask` was in flight yet, see
        # `_on_extension_ui_request`); upgrade that same pending id in place
        # now that the structured args are available, rather than requiring
        # the (already-passed) in-flight-ask ordering.
        pending = self._pending.get(key)
        if pending is not None and pending.kind == "ask" and not pending.questions:
            upgraded = _build_ask_pending(pending.id, args)
            self._pending[key] = upgraded
            self._hub.publish(
                "question_posted",
                {"task_id": task_id, "session": session, "question": asdict(upgraded)},
            )

    def _on_tool_execution_end(
        self, task_id: int, session: str, payload: dict[str, Any]
    ) -> None:
        key = (task_id, session)
        tool_call_id = payload.get("toolCallId")
        tools = self._inflight_tools.get(key)
        if tools is not None and isinstance(tool_call_id, str):
            tools.pop(tool_call_id, None)
        ask = self._inflight_asks.get(key)
        if ask is not None and ask[0] == tool_call_id:
            # The `ask` execution ended (answered, cancelled, or abandoned
            # some other way): the pending question is done and the turn
            # resumes (design D-6 state table: "Cleared to working on answer
            # / ask tool_execution_end / interrupt"). A no-op if the answer
            # endpoint already transitioned this via `answer_pending`.
            self.clear_pending(task_id, session)
            self._transition(
                task_id,
                session,
                "working",
                "ask tool_execution_end",
                allow_from={"waiting-input"},
            )

    def _on_extension_ui_request(
        self, task_id: int, session: str, payload: dict[str, Any]
    ) -> None:
        """Classify a pending `extension_ui_request` per SPEC D4 (design D-2)
        and enter the matching waiting state. Only applies mid-turn: a
        request outside `working` is logged and ignored (design D-6).

        Confirmed against the omp source during dogfooding 2026-07-20 (see the
        `omp-rpc-field-assumptions` memory note): both `ask` and the
        `tools.approval: prompt` gate use `method: "select"`
        (`extension-ui-controller`/`rpc-mode.ts`'s `select()`); every other
        method (`confirm`, `input`, `editor`, `notify`, `setStatus`,
        `setWidget`, `setTitle`, `set_editor_text`, `open_url`, `cancel`) is
        either fire-and-forget or unrelated to ask/approval, so only
        `method: "select"` is classified — anything else passes through
        opaquely without entering a waiting state, avoiding a phantom pending
        question no one can actually clear.

        Real omp emits this frame *before* `tool_execution_start` for `ask`,
        so the common case for a genuine ask is a provisional classification
        here (no ask is in flight *yet*, cross-check against
        `["Approve", "Deny"]` fails since the options are the ask's own
        choices) followed moments later by `_on_tool_execution_start`
        upgrading this same pending id in place."""
        key = (task_id, session)
        current = self._sessions.get(key)
        if current is None or current.status != "working":
            logger.info(
                "extension_ui_request for task %d session %s outside a turn (status=%s); ignored",
                task_id,
                session,
                current.status if current is not None else None,
            )
            return
        request_id = payload.get("id")
        if not isinstance(request_id, str):
            logger.warning(
                "extension_ui_request missing id for task %d session %s; ignored",
                task_id,
                session,
            )
            return
        if payload.get("method") != "select":
            return  # not an ask/approval dialog (setWidget, notify, confirm, ...)

        ask = self._inflight_asks.get(key)
        if ask is not None:
            _, args = ask
            pending = _build_ask_pending(request_id, args)
            self._enter_waiting(
                task_id, session, pending, "waiting-input", f"pending question {request_id!r}"
            )
            return

        options = payload.get("options")
        if options == _APPROVAL_OPTIONS:
            pending = PendingQuestion(id=request_id, kind="approval")
            self._enter_waiting(
                task_id, session, pending, "waiting-approval", f"pending approval {request_id!r}"
            )
            return

        logger.info(
            "extension_ui_request for task %d session %s has no ask in flight yet and options %r "
            "don't match an approval gate; provisionally classified as a question, upgraded if a "
            "tool_execution_start(ask) follows",
            task_id,
            session,
            options,
        )
        pending = PendingQuestion(id=request_id, kind="ask")
        self._enter_waiting(
            task_id,
            session,
            pending,
            "waiting-input",
            f"pending question {request_id!r} (fallback)",
        )

    def _enter_waiting(
        self,
        task_id: int,
        session: str,
        pending: PendingQuestion,
        status: str,
        reason: str,
    ) -> None:
        self._pending[(task_id, session)] = pending
        self._transition(task_id, session, status, reason, allow_from={"working"})
        self._hub.publish(
            "question_posted",
            {"task_id": task_id, "session": session, "question": asdict(pending)},
        )

    def _on_auto_retry_start(self, task_id: int, session: str, payload: dict[str, Any]) -> None:
        """`auto_retry_start`'s field shape is confirmed against the omp
        source (`extensibility/shared-events.ts`'s `AutoRetryStartEvent`,
        forwarded verbatim by `rpc-mode.ts`'s `session.subscribe(event =>
        output(event))` — see the `omp-rpc-field-assumptions` memory note):
        `attempt` / `maxAttempts` / `delayMs` / `errorMessage` (`errorId`
        optional). Still read defensively (a live daemon should not crash on
        a future omp shape change) and fall back to a generic reason when a
        field is missing or the wrong type."""
        attempt = payload.get("attempt")
        max_attempts = payload.get("maxAttempts")
        error_message = payload.get("errorMessage")
        if (
            isinstance(attempt, int)
            and isinstance(max_attempts, int)
            and isinstance(error_message, str)
            and error_message
        ):
            reason = f"auto_retry_start: {error_message} (attempt {attempt}/{max_attempts})"
        else:
            reason = "auto_retry_start frame"
        self._transition(
            task_id, session, "retrying", reason, allow_from={"working", "stalled"}
        )

    def _on_auto_retry_end(self, task_id: int, session: str, payload: dict[str, Any]) -> None:
        """`auto_retry_end`'s field shape is confirmed the same way:
        `success` / `attempt` / `finalError` (optional) / `recoveredErrors`
        (optional)."""
        success = payload.get("success")
        final_error = payload.get("finalError")
        if success is False and isinstance(final_error, str) and final_error:
            reason = f"auto_retry_end: gave up — {final_error}"
        else:
            reason = "auto_retry_end frame"
        self._transition(task_id, session, "working", reason, allow_from={"retrying"})

    def _recover_from_stall(self, task_id: int, session: str, event_type: str) -> None:
        """Any interpreted frame recovers a `stalled` session to `working`
        (design D-4), reason naming the frame that arrived."""
        current = self._sessions.get((task_id, session))
        if current is not None and current.status == "stalled":
            self._transition(
                task_id, session, "working", f"{event_type} frame", allow_from={"stalled"}
            )

    def _arm_watchdog(self, key: tuple[int, str]) -> None:
        """(Re)arm the stall watchdog, mirroring `_start_debounce` (design
        D-4): armed on entering/staying `working`, cancelled otherwise.
        The threshold value is captured at arm time so a later settings
        change does not alter in-flight deadlines."""
        self._cancel_watchdog(key)
        threshold = self._stall_threshold
        task = asyncio.create_task(self._watchdog_fire(key, threshold))
        self._watchdogs[key] = task
        task.add_done_callback(lambda t: self._pop_if_current(self._watchdogs, key, t))

    def _cancel_watchdog(self, key: tuple[int, str]) -> None:
        pending = self._watchdogs.pop(key, None)
        if pending is not None:
            pending.cancel()

    def _sync_watchdog(self, key: tuple[int, str]) -> None:
        """Re-arm if a frame left the session `working`; cancel otherwise
        (e.g. a frame that entered `waiting-input`/`waiting-approval`)."""
        current = self._sessions.get(key)
        if current is not None and current.status == "working":
            self._arm_watchdog(key)
        else:
            self._cancel_watchdog(key)

    async def _watchdog_fire(self, key: tuple[int, str], threshold: float) -> None:
        await asyncio.sleep(threshold)
        task_id, session = key
        self._transition(
            task_id,
            session,
            "stalled",
            f"no frames for {threshold:.0f}s",
            allow_from={"working"},
        )

    def _start_debounce(self, key: tuple[int, str], handle: AgentHandle) -> None:
        self._cancel_debounce(key)
        task_id, session = key
        task = asyncio.create_task(self._debounced_idle(task_id, session, handle))
        self._debounces[key] = task
        task.add_done_callback(lambda t: self._pop_if_current(self._debounces, key, t))

    def _cancel_debounce(self, key: tuple[int, str]) -> None:
        pending = self._debounces.pop(key, None)
        if pending is not None:
            pending.cancel()

    async def _debounced_idle(self, task_id: int, session: str, handle: AgentHandle) -> None:
        """Turn-boundary rule (design D-3): wait, then check the agent's queue;
        only a quiet, empty-queue result yields idle."""
        key = (task_id, session)
        await asyncio.sleep(self._idle_debounce)
        reason = f"agent_end, queue empty after {self._idle_debounce}s"
        try:
            response = await handle.request("get_state")
        except Exception as exc:  # noqa: BLE001 — any failure degrades to debounce-only
            logger.warning(
                "get_state failed for task %d session %s; falling back to debounce-only idle: %s",
                task_id,
                session,
                exc,
            )
            reason = f"agent_end, {self._idle_debounce}s quiet (state check failed)"
        else:
            data = response.get("data")
            data = data if isinstance(data, dict) else {}
            queued = data.get("queuedMessageCount") or 0
            if data.get("isStreaming") or queued > 0:
                # Not a real turn boundary: stay working, but surface why.
                self._transition(
                    task_id,
                    session,
                    "working",
                    f"agent_end, but {queued} queued message(s)"
                    if queued
                    else "agent_end, but still streaming",
                    allow_from={"working"},
                )
                # Genuinely still working (design D-4): the stall watchdog,
                # cancelled on turn end, resumes owning the quiet period.
                self._arm_watchdog(key)
                return
        self._transition(task_id, session, "idle", reason, allow_from={"working"})
        current = self._sessions.get(key)
        if current is not None and current.status == "idle":
            # Idle-entry observer hook (design D-6): the advisory sampler's
            # "maybe waiting for a reply" heuristic runs from here.
            self._fire_hooks(self._idle_entered_hooks, task_id, session, handle)

    @staticmethod
    def _pop_if_current(
        registry: dict[tuple[int, str], asyncio.Task], key: tuple[int, str], task: asyncio.Task
    ) -> None:
        if registry.get(key) is task:
            registry.pop(key, None)
        if not task.cancelled() and task.exception() is not None:
            logger.error("session tracker task for %s failed", key, exc_info=task.exception())
