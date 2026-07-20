"""Per-session status state machine (SPEC Decision 4, core subset + D4
ask/approval extension).

`SessionTracker` owns `{status, reason, since}` per task, fed by (a) lifecycle
calls from the supervisor and spawn pipeline (spawned, exited, step failures)
and (b) a subscriber queue on each agent's event fan-out for `agent_start` /
`agent_end` and, since the `ask-approvals` capability, `tool_execution_start`
/ `tool_execution_end` / `extension_ui_request` frames (design D-1). Every
transition goes through one guarded method that re-checks the current status,
so races (exit during the idle debounce, late frames after discard) resolve
deterministically: exit wins.

Status is in-memory only and independent of live handles — `failed` outlives
the child's deregistration; entries are dropped on task cleanup/purge.
Persistence across daemon restarts is a later chunk.

A pending `ask`/approval question rides alongside `SessionInfo` in a parallel
`_pending` map (design D-1: "parallel map, not enriched rows"), so the
transition machinery and the pending-question machinery stay decoupled.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, TYPE_CHECKING

from ompire_daemon.events import EventHub

if TYPE_CHECKING:
    from ompire_daemon.agent import AgentHandle

logger = logging.getLogger(__name__)

SESSION_STATUSES = ("starting", "working", "idle", "failed", "waiting-input", "waiting-approval")

_APPROVAL_OPTIONS = ["Approve", "Deny"]


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
    def __init__(self, hub: EventHub, idle_debounce: float) -> None:
        self._hub = hub
        self._idle_debounce = idle_debounce
        self._sessions: dict[int, SessionInfo] = {}
        self._watchers: dict[int, asyncio.Task] = {}
        self._debounces: dict[int, asyncio.Task] = {}
        self._operator_stops: set[int] = set()
        self._pending: dict[int, PendingQuestion] = {}
        # In-flight tool executions per task: toolCallId -> tool name.
        self._inflight_tools: dict[int, dict[str, str]] = {}
        # The current in-flight `ask` execution per task (at most one, since
        # `ask` declares `concurrency: "exclusive"`): (toolCallId, args).
        self._inflight_asks: dict[int, tuple[str, dict[str, Any]]] = {}

    def get(self, task_id: int) -> SessionInfo | None:
        return self._sessions.get(task_id)

    def pending(self, task_id: int) -> PendingQuestion | None:
        return self._pending.get(task_id)

    def snapshot(self) -> dict[int, dict[str, Any]]:
        """Current statuses for the WS snapshot's `sessions` map (design D-4),
        each entry carrying the pending question (if any) under `question`."""
        result: dict[int, dict[str, Any]] = {}
        for task_id, info in self._sessions.items():
            entry = asdict(info)
            pending = self._pending.get(task_id)
            if pending is not None:
                entry["question"] = asdict(pending)
            result[task_id] = entry
        return result

    # --- lifecycle transitions (supervisor / spawn pipeline / stop path) ------

    def agent_spawning(self, task_id: int) -> None:
        """The agent child is being spawned; covers the ready handshake too."""
        self._transition(task_id, "starting", "agent spawned")

    def watch(self, task_id: int, handle: AgentHandle) -> None:
        """Subscribe to the agent's fan-out and drive frame transitions."""
        self.unwatch(task_id)
        queue = handle.subscribe()
        watcher = asyncio.create_task(self._watch_events(task_id, handle, queue))
        self._watchers[task_id] = watcher
        watcher.add_done_callback(lambda t: self._pop_if_current(self._watchers, task_id, t))

    def agent_exited(self, task_id: int, exit_code: int) -> None:
        """Any child exit lands `failed` (design D-2); the reason distinguishes
        an operator stop from a crash. Exit wins any pending idle debounce and
        any pending question (D-6: discarded, not resolved — `failed` wins)."""
        self._cancel_debounce(task_id)
        self.clear_pending(task_id, broadcast=False)
        self._inflight_tools.pop(task_id, None)
        if task_id in self._operator_stops:
            self._operator_stops.discard(task_id)
            reason = "stopped by operator"
        else:
            reason = f"process exited with code {exit_code}"
        self._transition(task_id, "failed", reason)

    def expect_operator_stop(self, task_id: int) -> None:
        self._operator_stops.add(task_id)

    def clear_operator_stop(self, task_id: int) -> None:
        self._operator_stops.discard(task_id)

    def spawn_step_failed(self, task_id: int, reason: str) -> None:
        """An agent/prompt spawn step failed before or despite the child."""
        self._transition(task_id, "failed", reason)

    def prompt_skipped(self, task_id: int) -> None:
        """Empty stored prompt: ready → idle instead of hanging in starting."""
        self._transition(task_id, "idle", "ready, no prompt to send", allow_from={"starting"})

    def discard(self, task_id: int) -> None:
        """Drop the entry on task cleanup/purge; late events cannot resurrect it."""
        self._cancel_debounce(task_id)
        self.unwatch(task_id)
        self._operator_stops.discard(task_id)
        self._sessions.pop(task_id, None)
        self._pending.pop(task_id, None)
        self._inflight_tools.pop(task_id, None)
        self._inflight_asks.pop(task_id, None)

    def unwatch(self, task_id: int) -> None:
        watcher = self._watchers.pop(task_id, None)
        if watcher is not None:
            watcher.cancel()

    def clear_pending(self, task_id: int, *, broadcast: bool = True) -> PendingQuestion | None:
        """Clear a task's pending question (design D-6): the `ask` execution
        ending, a turn boundary, an interrupt, or an exit. `broadcast=False`
        suppresses `question_resolved` when a process exit is about to
        broadcast `failed` instead (exit wins, no separate resolve)."""
        pending = self._pending.pop(task_id, None)
        self._inflight_asks.pop(task_id, None)
        if pending is not None and broadcast:
            self._hub.publish("question_resolved", {"task_id": task_id, "question_id": pending.id})
        return pending

    def answer_pending(self, task_id: int) -> PendingQuestion | None:
        """The operator answered the pending question (design D-5): clear it
        and return the session to `working` — called by the answer endpoint
        after it sends the reply. Distinct from `clear_pending` (which the
        frame watcher also uses for turn-movement clears that don't imply
        `working`, e.g. exit) because only an answer names its own reason and
        transition."""
        pending = self.clear_pending(task_id)
        if pending is not None:
            self._transition(
                task_id,
                "working",
                "operator answered the pending question",
                allow_from={"waiting-input", "waiting-approval"},
            )
        return pending

    # --- internals ------------------------------------------------------------

    def _transition(
        self, task_id: int, to: str, reason: str, *, allow_from: set[str] | None = None
    ) -> None:
        """The single guarded transition: re-checks current status so competing
        transitions (exit vs. debounce vs. frames) can't clobber each other."""
        current = self._sessions.get(task_id)
        from_status = current.status if current is not None else None
        if current is None and to != "starting":
            return  # discarded or never-tracked: late events are ignored
        if from_status == "failed":
            return  # terminal until cleanup (no restart path this chunk)
        if allow_from is not None and from_status not in allow_from:
            return
        info = SessionInfo(status=to, reason=reason, since=_now_iso())
        self._sessions[task_id] = info
        self._hub.publish(
            "status_changed",
            {"task_id": task_id, "from": from_status, "to": to, "reason": reason},
        )

    async def _watch_events(self, task_id: int, handle: AgentHandle, queue: asyncio.Queue) -> None:
        # Local import: agent.py type-imports this module, so a module-level
        # import here would be circular.
        from ompire_daemon.agent import EVENT_STREAM_END

        try:
            while True:
                event = await queue.get()
                if event is EVENT_STREAM_END:
                    return
                if event.type == "agent_start":
                    self._cancel_debounce(task_id)
                    # A turn starting abandons any question left over from the
                    # previous one (design D-6); tool tracking resets too.
                    self.clear_pending(task_id)
                    self._inflight_tools.pop(task_id, None)
                    self._transition(task_id, "working", "agent_start frame")
                elif event.type == "agent_end":
                    self.clear_pending(task_id)
                    self._inflight_tools.pop(task_id, None)
                    self._start_debounce(task_id, handle)
                elif event.type == "tool_execution_start":
                    self._on_tool_execution_start(task_id, event.payload)
                elif event.type == "tool_execution_end":
                    self._on_tool_execution_end(task_id, event.payload)
                elif event.type == "extension_ui_request":
                    self._on_extension_ui_request(task_id, event.payload)
        finally:
            handle.unsubscribe(queue)

    def _on_tool_execution_start(self, task_id: int, payload: dict[str, Any]) -> None:
        # Field names confirmed against real omp during dogfooding 2026-07-20
        # (see the `omp-rpc-field-assumptions` memory note): `toolCallId` /
        # `toolName`, not `toolUseId` / `name`.
        tool_call_id = payload.get("toolCallId")
        name = payload.get("toolName")
        if not isinstance(tool_call_id, str) or not isinstance(name, str):
            return
        self._inflight_tools.setdefault(task_id, {})[tool_call_id] = name
        if name != "ask":
            return
        args = payload.get("args")
        args = args if isinstance(args, dict) else {}
        self._inflight_asks[task_id] = (tool_call_id, args)
        # Also confirmed by dogfooding: real omp emits `extension_ui_request`
        # *before* `tool_execution_start` for `ask` — the reverse of what the
        # design assumed. If the request already arrived, it was provisionally
        # classified as a bare question (no `ask` was in flight yet, see
        # `_on_extension_ui_request`); upgrade that same pending id in place
        # now that the structured args are available, rather than requiring
        # the (already-passed) in-flight-ask ordering.
        pending = self._pending.get(task_id)
        if pending is not None and pending.kind == "ask" and not pending.questions:
            upgraded = _build_ask_pending(pending.id, args)
            self._pending[task_id] = upgraded
            self._hub.publish("question_posted", {"task_id": task_id, "question": asdict(upgraded)})

    def _on_tool_execution_end(self, task_id: int, payload: dict[str, Any]) -> None:
        tool_call_id = payload.get("toolCallId")
        tools = self._inflight_tools.get(task_id)
        if tools is not None and isinstance(tool_call_id, str):
            tools.pop(tool_call_id, None)
        ask = self._inflight_asks.get(task_id)
        if ask is not None and ask[0] == tool_call_id:
            # The `ask` execution ended (answered, cancelled, or abandoned
            # some other way): the pending question is done and the turn
            # resumes (design D-6 state table: "Cleared to working on answer
            # / ask tool_execution_end / interrupt"). A no-op if the answer
            # endpoint already transitioned this via `answer_pending`.
            self.clear_pending(task_id)
            self._transition(
                task_id, "working", "ask tool_execution_end", allow_from={"waiting-input"}
            )

    def _on_extension_ui_request(self, task_id: int, payload: dict[str, Any]) -> None:
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
        current = self._sessions.get(task_id)
        if current is None or current.status != "working":
            logger.info(
                "extension_ui_request for task %d outside a turn (status=%s); ignored",
                task_id,
                current.status if current is not None else None,
            )
            return
        request_id = payload.get("id")
        if not isinstance(request_id, str):
            logger.warning("extension_ui_request missing id for task %d; ignored", task_id)
            return
        if payload.get("method") != "select":
            return  # not an ask/approval dialog (setWidget, notify, confirm, ...)

        ask = self._inflight_asks.get(task_id)
        if ask is not None:
            _, args = ask
            pending = _build_ask_pending(request_id, args)
            self._enter_waiting(task_id, pending, "waiting-input", f"pending question {request_id!r}")
            return

        options = payload.get("options")
        if options == _APPROVAL_OPTIONS:
            pending = PendingQuestion(id=request_id, kind="approval")
            self._enter_waiting(
                task_id, pending, "waiting-approval", f"pending approval {request_id!r}"
            )
            return

        logger.info(
            "extension_ui_request for task %d has no ask in flight yet and options %r don't match "
            "an approval gate; provisionally classified as a question, upgraded if a "
            "tool_execution_start(ask) follows",
            task_id,
            options,
        )
        pending = PendingQuestion(id=request_id, kind="ask")
        self._enter_waiting(
            task_id, pending, "waiting-input", f"pending question {request_id!r} (fallback)"
        )

    def _enter_waiting(
        self, task_id: int, pending: PendingQuestion, status: str, reason: str
    ) -> None:
        self._pending[task_id] = pending
        self._transition(task_id, status, reason, allow_from={"working"})
        self._hub.publish("question_posted", {"task_id": task_id, "question": asdict(pending)})

    def _start_debounce(self, task_id: int, handle: AgentHandle) -> None:
        self._cancel_debounce(task_id)
        task = asyncio.create_task(self._debounced_idle(task_id, handle))
        self._debounces[task_id] = task
        task.add_done_callback(lambda t: self._pop_if_current(self._debounces, task_id, t))

    def _cancel_debounce(self, task_id: int) -> None:
        pending = self._debounces.pop(task_id, None)
        if pending is not None:
            pending.cancel()

    async def _debounced_idle(self, task_id: int, handle: AgentHandle) -> None:
        """Turn-boundary rule (design D-3): wait, then check the agent's queue;
        only a quiet, empty-queue result yields idle."""
        await asyncio.sleep(self._idle_debounce)
        reason = f"agent_end, queue empty after {self._idle_debounce}s"
        try:
            response = await handle.request("get_state")
        except Exception as exc:  # noqa: BLE001 — any failure degrades to debounce-only
            logger.warning(
                "get_state failed for task %d; falling back to debounce-only idle: %s",
                task_id,
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
                    "working",
                    f"agent_end, but {queued} queued message(s)"
                    if queued
                    else "agent_end, but still streaming",
                    allow_from={"working"},
                )
                return
        self._transition(task_id, "idle", reason, allow_from={"working"})

    @staticmethod
    def _pop_if_current(registry: dict[int, asyncio.Task], task_id: int, task: asyncio.Task) -> None:
        if registry.get(task_id) is task:
            registry.pop(task_id, None)
        if not task.cancelled() and task.exception() is not None:
            logger.error("session tracker task for %d failed", task_id, exc_info=task.exception())
