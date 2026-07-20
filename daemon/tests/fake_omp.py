"""Fake omp: speaks rpc-ui NDJSON frames recorded from the spike's
experiment2 transcript (design D-8). Tests pass this script directly as the
agent argv, skipping `workshop exec` entirely.

Usage: python -u fake_omp.py [scenario]

Scenarios:
  happy             ready, then answer requests (default)
  silent            never emit ready; swallow stdin until killed
  crash             exit 1 with "No models available" on stderr before ready
  exit-after-ready  emit ready then exit 7
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

    queued = 0
    message_count = 0
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        request = json.loads(line)
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
            continue
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
            continue
        # Composer actions: acked success, mirroring omp's `message` field.
        if request.get("type") in ("steer", "follow_up", "abort_and_prompt"):
            command = request.get("type")
            if request.get("message", "") == "fail":
                emit(
                    {
                        "id": request_id,
                        "type": "response",
                        "command": command,
                        "success": False,
                        "error": "boom",
                    }
                )
                continue
            emit(
                {
                    "id": request_id,
                    "type": "response",
                    "command": command,
                    "success": True,
                }
            )
            continue
        if request.get("type") != "prompt":
            emit(
                {
                    "id": request_id,
                    "type": "response",
                    "command": request.get("type"),
                    "success": False,
                    "error": f"Unknown command: {request.get('type')}",
                }
            )
            continue
        message = request.get("message", "")
        if message == "die":
            sys.exit(23)
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
