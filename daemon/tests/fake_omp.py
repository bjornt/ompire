"""Fake omp: speaks rpc-ui NDJSON frames recorded from the spike's
experiment2 transcript (design D-8). Tests pass this script directly as the
agent argv, skipping `workshop exec` entirely.

Usage: python -u fake_omp.py [scenario]

Scenarios:
  happy             ready, then answer requests (default)
  silent            never emit ready; swallow stdin until killed
  crash             exit 1 with "No models available" on stderr before ready
  exit-after-ready  emit ready then exit 7
  ignore-term       emit ready, then ignore SIGTERM and hang until killed
                    (AgentHandle.terminate()'s SIGKILL-fallback path)
  get-state-fails   like happy, but get_state responds success: false

`get_state` requests get the response shape verified against omp 16.5.2
(see the add-session-states change's findings-omp-verification.md):
`isStreaming` / `queuedMessageCount` at the top level of `data`.
`get_session_stats` returns token/cost `data`; `steer` / `follow_up` /
`abort_and_prompt` are acked `success: true` (or `success: false` when their
message is `fail`). Any other request type gets the recorded `success: false`
"Unknown command" response; `prompt` requests get the recorded ack + event
burst, with these
magic messages:
  die      exit 23 without answering
  fail     respond success: false, error "boom"
  garbage  emit an unparseable line and an extra push event before the burst
  big      include a ~200 KB event frame in the burst (>64 KiB stream limit)
  queue    after the burst, get_state reports queuedMessageCount: 1
  no-end   emit the burst without the trailing agent_end frame
  ask      an `ask` tool_execution_start (single-select, recommended,
           allows-other question) -> extension_ui_request -> waits for the
           daemon's reply frame -> tool_execution_end -> burst tail
  approve  an approval-gate extension_ui_request (options exactly
           ["Approve", "Deny"], no ask tool in flight) -> waits for the
           daemon's reply frame -> burst tail
  ask-cancel  like `ask`, but the `ask` execution ends immediately without
           ever waiting for a reply (models the agent/tool cancelling the
           ask on its own)
  auto-retry       agent_start -> auto_retry_start -> auto_retry_end -> a
           normal burst tail (models a retry that completes and the turn
           finishing normally). `auto_retry_*` field shapes (`attempt` /
           `maxAttempts` / `delayMs` / `errorMessage` for start; `success` /
           `attempt` / `finalError` for end) are confirmed against the omp
           source (`extensibility/shared-events.ts`'s `AutoRetryStartEvent`
           / `AutoRetryEndEvent`, forwarded verbatim by rpc-mode.ts) — see
           the `omp-rpc-field-assumptions` memory note.
  auto-retry-hang  agent_start -> auto_retry_start, then nothing further
           (models a retry stuck in flight, for exit-during-retry tests)

The `ask`/`approve` reply frame shape (`extension_ui_response`, echoing the
request `id`, with a single `value` string field) is confirmed against the
omp source during dogfooding 2026-07-20 (see the `omp-rpc-field-assumptions`
memory note) and echoed into the assistant's reply text so tests can assert
on what the daemon sent.

While a `extension_ui_response` reply is outstanding, other requests (e.g. an
operator interrupt's `abort_and_prompt`) are still answered normally — the
main loop tracks "waiting for this ui reply" as state rather than blocking a
single read, so it can interleave both (real omp's rpc reader is not blocked
on the ask tool call either).
"""

from __future__ import annotations

import json
import sys


def emit(frame: dict) -> None:
    print(json.dumps(frame), flush=True)


def burst(prompt_id: str, message: str, big: bool = False, end: bool = True) -> None:
    """The recorded post-prompt sequence: push events interleave with the
    ack on the same stream (spike finding)."""
    emit(
        {
            "type": "extension_ui_request",
            "id": "1535befcedd1a5c6",
            "method": "setWidget",
            "widgetKey": "autoresearch",
        }
    )
    emit({"id": prompt_id, "type": "response", "command": "prompt", "success": True})
    emit({"type": "agent_start"})
    emit({"type": "turn_start"})
    user_message = {"role": "user", "content": [{"type": "text", "text": message}]}
    emit({"type": "message_start", "message": user_message})
    emit({"type": "message_end", "message": user_message})
    assistant_message = {"role": "assistant", "content": [{"type": "text", "text": "hello"}]}
    emit({"type": "message_start", "message": {"role": "assistant", "content": []}})
    if big:
        emit({"type": "tool_output", "data": "x" * 200_000})
    emit({"type": "message_end", "message": assistant_message})
    emit({"type": "turn_end", "message": assistant_message})
    if end:
        emit({"type": "agent_end", "messages": [user_message, assistant_message]})


def ask_start(prompt_id: str, message: str) -> dict:
    """Emit up through the `ask` question's `extension_ui_request` and return
    the state main() needs to finish the burst once the reply arrives.

    Shape and ordering confirmed against real omp during dogfooding
    2026-07-20 (see the `omp-rpc-field-assumptions` memory note):
    `extension_ui_request` arrives *before* `tool_execution_start` for `ask`
    (the reverse of the design's original assumption — the daemon handles
    this by provisionally classifying the request, then upgrading it in
    place once the structured args arrive). `tool_execution_start` uses
    `toolCallId` / `toolName` / `args.questions[].question` /
    `options[].label` (no separate `value`) / `recommended` as an *index*;
    the `extension_ui_request` mirrors it as flat display strings with the
    recommendation and free-text-other affordance baked into the option
    text itself, rather than as separate flags."""
    emit({"id": prompt_id, "type": "response", "command": "prompt", "success": True})
    emit({"type": "agent_start"})
    emit({"type": "turn_start"})
    user_message = {"role": "user", "content": [{"type": "text", "text": message}]}
    emit({"type": "message_start", "message": user_message})
    emit({"type": "message_end", "message": user_message})
    emit(
        {
            "type": "extension_ui_request",
            "id": "ask-ui-1",
            "method": "select",
            "title": "Apply the same lock ordering to the dhcpd6 loop?",
            "options": ["Yes, both loops (Recommended)", "v4 only", "Other (type your own)"],
        }
    )
    tool_call_id = "toolu_ask1"
    emit(
        {
            "type": "tool_execution_start",
            "toolCallId": tool_call_id,
            "toolName": "ask",
            "args": {
                "questions": [
                    {
                        "id": "demo",
                        "question": "Apply the same lock ordering to the dhcpd6 loop?",
                        "options": [
                            {"label": "Yes, both loops", "description": "Widen the fix"},
                            {"label": "v4 only", "description": "Match the reproducer"},
                        ],
                        "recommended": 0,
                    }
                ]
            },
            "intent": "Demonstrating ask tool",
        }
    )
    return {"kind": "ask", "ui_id": "ask-ui-1", "tool_call_id": tool_call_id, "user_message": user_message}


def ask_finish(pending_ui: dict, reply: dict) -> None:
    emit({"type": "tool_execution_end", "toolCallId": pending_ui["tool_call_id"]})
    answer_text = f"got answer: {reply}"
    assistant_message = {"role": "assistant", "content": [{"type": "text", "text": answer_text}]}
    emit({"type": "message_start", "message": {"role": "assistant", "content": []}})
    emit({"type": "message_end", "message": assistant_message})
    emit({"type": "turn_end", "message": assistant_message})
    emit({"type": "agent_end", "messages": [pending_ui["user_message"], assistant_message]})


def approval_start(prompt_id: str, message: str) -> dict:
    """Emit up through the approval gate's `extension_ui_request` (no `ask`
    tool in flight, options exactly `["Approve", "Deny"]`).

    Confirmed against the omp source during dogfooding 2026-07-20 (see the
    `omp-rpc-field-assumptions` memory note): the approval gate uses the same
    `method: "select"` dialog as `ask` (`extensibility/extensions/wrapper.ts`'s
    `uiContext.select(prompt, ["Approve", "Deny"])`), answered with
    `{"value": "Approve"}` / `{"value": "Deny"}` — an exact string match, not
    a boolean `confirmed` field."""
    emit({"id": prompt_id, "type": "response", "command": "prompt", "success": True})
    emit({"type": "agent_start"})
    emit({"type": "turn_start"})
    user_message = {"role": "user", "content": [{"type": "text", "text": message}]}
    emit({"type": "message_start", "message": user_message})
    emit({"type": "message_end", "message": user_message})
    emit(
        {
            "type": "extension_ui_request",
            "id": "approval-ui-1",
            "method": "select",
            "options": ["Approve", "Deny"],
        }
    )
    return {"kind": "approval", "ui_id": "approval-ui-1", "user_message": user_message}


def approval_finish(pending_ui: dict, reply: dict) -> None:
    answer_text = f"got answer: {reply}"
    assistant_message = {"role": "assistant", "content": [{"type": "text", "text": answer_text}]}
    emit({"type": "message_start", "message": {"role": "assistant", "content": []}})
    emit({"type": "message_end", "message": assistant_message})
    emit({"type": "turn_end", "message": assistant_message})
    emit({"type": "agent_end", "messages": [pending_ui["user_message"], assistant_message]})


def get_state_response(request_id: str, queued: int, message_count: int) -> dict:
    """The response shape verified against omp 16.5.2: isStreaming and
    queuedMessageCount at the top level of `data`."""
    return {
        "id": request_id,
        "type": "response",
        "command": "get_state",
        "success": True,
        "data": {
            "isStreaming": False,
            "isCompacting": False,
            "queuedMessageCount": queued,
            "messageCount": message_count,
            "sessionId": "fake-session-id",
        },
    }


def handle_generic_request(request: dict, scenario: str, queued: int, message_count: int) -> None:
    """Answer a non-`prompt`, non-ui-reply request. Used both from the main
    dispatch loop and while a `extension_ui_request` reply is outstanding
    (real omp's rpc reader is not blocked on the ask tool call either)."""
    request_id = request.get("id", "")
    if request.get("type") == "get_state":
        if scenario == "get-state-fails":
            emit(
                {
                    "id": request_id,
                    "type": "response",
                    "command": "get_state",
                    "success": False,
                    "error": "state unavailable",
                }
            )
        else:
            emit(get_state_response(request_id, queued, message_count))
        return
    if request.get("type") == "get_session_stats":
        emit(
            {
                "id": request_id,
                "type": "response",
                "command": "get_session_stats",
                "success": True,
                "data": {
                    "inputTokens": 1200,
                    "outputTokens": 340,
                    "totalCostUsd": 0.0123,
                    "messageCount": message_count,
                },
            }
        )
        return
    # Composer actions: acked success, mirroring omp's `message` field.
    if request.get("type") in ("steer", "follow_up", "abort_and_prompt"):
        command = request.get("type")
        if request.get("message", "") == "fail":
            emit(
                {"id": request_id, "type": "response", "command": command, "success": False, "error": "boom"}
            )
            return
        emit({"id": request_id, "type": "response", "command": command, "success": True})
        return
    emit(
        {
            "id": request_id,
            "type": "response",
            "command": request.get("type"),
            "success": False,
            "error": f"Unknown command: {request.get('type')}",
        }
    )


def main() -> None:
    scenario = sys.argv[1] if len(sys.argv) > 1 else "happy"

    if scenario == "crash":
        print("Error: No models available", file=sys.stderr, flush=True)
        sys.exit(1)
    if scenario == "silent":
        for _ in sys.stdin:
            pass
        return

    emit({"type": "ready"})
    if scenario == "exit-after-ready":
        sys.exit(7)
    if scenario == "ignore-term":
        # Models a wedged child that doesn't honor SIGTERM, for
        # AgentHandle.terminate()'s SIGKILL-fallback path.
        import signal

        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        for _ in sys.stdin:
            pass
        return

    queued = 0
    message_count = 0
    # Set while an `ask`/approval burst is waiting on its `extension_ui_response`
    # (design D-5's "ask.timeout=0, waits indefinitely"): other requests keep
    # being answered in the meantime, matching real omp's non-blocking reader.
    pending_ui: dict | None = None
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        request = json.loads(line)

        if pending_ui is not None:
            if request.get("type") == "extension_ui_response" and request.get("id") == pending_ui["ui_id"]:
                if pending_ui["kind"] == "ask":
                    ask_finish(pending_ui, request)
                else:
                    approval_finish(pending_ui, request)
                pending_ui = None
                continue
            handle_generic_request(request, scenario, queued, message_count)
            continue

        if request.get("type") != "prompt":
            handle_generic_request(request, scenario, queued, message_count)
            continue

        request_id = request.get("id", "")
        message = request.get("message", "")
        if message == "die":
            sys.exit(23)
        if message == "ask":
            message_count += 2
            pending_ui = ask_start(request_id, message)
            continue
        if message == "approve":
            message_count += 2
            pending_ui = approval_start(request_id, message)
            continue
        if message == "ask-cancel":
            # The `ask` execution ends on its own without an operator answer
            # ever arriving (design D-6: tool_execution_end must still clear
            # the pending question and return the session to working).
            message_count += 2
            ask_finish(ask_start(request_id, message), {"cancelled": True})
            continue
        if message == "auto-retry":
            message_count += 2
            emit({"id": request_id, "type": "response", "command": "prompt", "success": True})
            emit({"type": "agent_start"})
            emit(
                {
                    "type": "auto_retry_start",
                    "attempt": 1,
                    "maxAttempts": 5,
                    "delayMs": 40000,
                    "errorMessage": "HTTP 429 from gateway",
                }
            )
            emit({"type": "auto_retry_end", "success": True, "attempt": 1})
            emit({"type": "turn_start"})
            user_message = {"role": "user", "content": [{"type": "text", "text": message}]}
            emit({"type": "message_start", "message": user_message})
            emit({"type": "message_end", "message": user_message})
            assistant_message = {
                "role": "assistant",
                "content": [{"type": "text", "text": "retried ok"}],
            }
            emit({"type": "message_start", "message": {"role": "assistant", "content": []}})
            emit({"type": "message_end", "message": assistant_message})
            emit({"type": "turn_end", "message": assistant_message})
            emit({"type": "agent_end", "messages": [user_message, assistant_message]})
            continue
        if message == "auto-retry-hang":
            # Emits the retry-start frame and nothing further, for
            # exit-during-retry tests.
            message_count += 2
            emit({"id": request_id, "type": "response", "command": "prompt", "success": True})
            emit({"type": "agent_start"})
            emit(
                {
                    "type": "auto_retry_start",
                    "attempt": 1,
                    "maxAttempts": 5,
                    "delayMs": 40000,
                    "errorMessage": "HTTP 429 from gateway",
                }
            )
            continue
        if message == "bad-ask":
            # A malformed `extension_ui_request` (missing the required `id`)
            # exercises the interpreted-subset containment path.
            message_count += 2
            emit({"id": request_id, "type": "response", "command": "prompt", "success": True})
            emit({"type": "agent_start"})
            emit({"type": "extension_ui_request"})
            emit({"type": "agent_end"})
            continue
        if message == "fail":
            emit(
                {
                    "id": request_id,
                    "type": "response",
                    "command": "prompt",
                    "success": False,
                    "error": "boom",
                }
            )
            continue
        if message == "garbage":
            print("this is not json", flush=True)
            emit({"type": "note", "text": "still alive"})
        if message == "queue":
            queued = 1
        message_count += 2
        burst(request_id, message, big=message == "big", end=message != "no-end")


if __name__ == "__main__":
    main()
