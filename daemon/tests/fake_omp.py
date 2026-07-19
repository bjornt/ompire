"""Fake omp: speaks rpc-ui NDJSON frames recorded from the spike's
experiment2 transcript (design D-8). Tests pass this script directly as the
agent argv, skipping `workshop exec` entirely.

Usage: python -u fake_omp.py [scenario]

Scenarios:
  happy             ready, then answer requests (default)
  silent            never emit ready; swallow stdin until killed
  crash             exit 1 with "No models available" on stderr before ready
  exit-after-ready  emit ready then exit 7

In the `happy` scenario, non-`prompt` requests get the recorded
`success: false` "Unknown command" response; `prompt` requests get the
recorded ack + event burst, with these magic messages:
  die      exit 23 without answering
  fail     respond success: false, error "boom"
  garbage  emit an unparseable line and an extra push event before the burst
  big      include a ~200 KB event frame in the burst (>64 KiB stream limit)
"""

from __future__ import annotations

import json
import sys


def emit(frame: dict) -> None:
    print(json.dumps(frame), flush=True)


def burst(prompt_id: str, message: str, big: bool = False) -> None:
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
    emit({"type": "agent_end", "messages": [user_message, assistant_message]})


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

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        request = json.loads(line)
        request_id = request.get("id", "")
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
        burst(request_id, message, big=message == "big")


if __name__ == "__main__":
    main()
