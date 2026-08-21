"""E2E validation: the daemon's real AgentSupervisor/AgentHandle driving the
fake omp through the fake workshop (local-test Part 4, task 3.1/3.2).

Runs from the daemon directory: uv run python ../local-test/fakes/../../scripts/… —
no: run as `uv run python local-test/fakes/../e2e_check.py`-style from repo
root with the daemon venv's python. See findings.md for the invocation.
"""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "daemon" / "src"))

from ompire_daemon.agent import AgentSupervisor  # noqa: E402
from ompire_daemon.config import Config  # noqa: E402
from ompire_daemon.events import EventHub  # noqa: E402
from ompire_daemon.ship import _DRAFT_PROMPT, _parse_draft  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
FAKES = REPO / "local-test" / "fakes"
WORK = Path(tempfile.mkdtemp(prefix="omp-e2e-"))

failures = 0
total = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures, total
    total += 1
    if ok:
        print(f"  ok    {total:2d}  {name}")
    else:
        failures += 1
        print(f"  FAIL  {total:2d}  {name}" + (f" — {detail}" if detail else ""))


async def main() -> None:
    state = WORK / "state"
    clone = WORK / "clone"
    state.mkdir()
    clone.mkdir()

    # The clone is a git repo with a workshop.yaml so fake workshop launch/
    # exec accept it (Part 3's registry).
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=clone, check=True)
    (clone / "workshop.yaml").write_text("name: e2e-agent\nbase: ubuntu@26.04\n")
    (clone / "README.md").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], cwd=clone, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
         "-m", "seed"], cwd=clone, check=True)

    env = {
        **os.environ,
        "PATH": f"{FAKES}:{os.environ['PATH']}",
        "LOCAL_TEST_STATE": str(state),
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    os.environ.clear()
    os.environ.update(env)

    # Launch the workshop through the fake so `workshop exec` accepts the clone.
    r = subprocess.run(["workshop", "launch"], cwd=clone,
                       capture_output=True, text=True)
    check("fake workshop launch", r.returncode == 0 and
          (clone / ".workshop.lock").exists())

    config = Config()
    hub = EventHub()
    supervisor = AgentSupervisor(config, hub)

    # --- 3.1: preflight, spawn, happy turn, session capture ----------------
    handle = await supervisor.start(1, "main", str(clone))
    check("ready handshake via AgentHandle", handle.returncode is None)

    outcome_prompt = (
        "Fix the bug.\n\nWhen you have finished the work above, write your "
        "result as JSON to `.ompire/outcome.json` with exactly this schema."
    )
    response = await handle.prompt(outcome_prompt)
    check("prompt acked success", response.get("success") is True)

    await asyncio.sleep(0.5)  # let the burst land
    types = [e.payload.get("type") for e in handle.snapshot()]
    check("event burst observed ending in agent_end",
          "agent_start" in types and "agent_end" in types)

    try:
        outcome = json.loads((clone / ".ompire/outcome.json").read_text())
    except (OSError, json.JSONDecodeError):
        outcome = {}
    check("outcome authored in clone", outcome.get("status") == "success")

    session_id = await handle.read_session_id()
    check("session id captured via get_state", bool(session_id))

    # --- ask flow through respond_ui_request --------------------------------
    got_ask = asyncio.Event()
    ask_event = {}

    # Subscribe and wait for the question to appear.
    queue = handle.subscribe()

    async def wait_ask() -> None:
        while True:
            item = await queue.get()
            if item is None:
                return
            if item.payload.get("type") == "extension_ui_request":
                ask_event.update(item.payload)
                got_ask.set()
                return

    waiter = asyncio.create_task(wait_ask())
    await handle.prompt("[[scenario:ask]]\nplease?")
    try:
        await asyncio.wait_for(got_ask.wait(), timeout=5)
    except TimeoutError:
        pass
    check("ask question reached the daemon", bool(ask_event))
    waiter.cancel()

    if ask_event.get("id"):
        await handle.respond_ui_request(ask_event["id"], {"value": "v4 only"})
        await asyncio.sleep(0.5)
        types = [e.payload.get("type") for e in handle.snapshot()]
        check("ask reply completes the turn", types.count("agent_end") >= 2)

    # --- ship draft via the daemon's verbatim prompt ------------------------
    await handle.prompt(_DRAFT_PROMPT)
    await asyncio.sleep(0.5)
    response = await handle.request("get_last_assistant_text")
    text = response.get("data", {}).get("text", "")
    parsed = _parse_draft(text)
    check("ship draft classified + parseable by ship._parse_draft",
          parsed is not None and all(
              [parsed.commit_message, parsed.pr_title, parsed.pr_body]),
          text[:50])

    # --- 3.2: crash + resume -----------------------------------------------
    await handle.kill()
    await asyncio.sleep(0.2)
    resumed = await supervisor.start(1, "main", str(clone), resume=session_id)
    check("resume spawn passes preflight + ready",
          resumed.returncode is None)
    state_resp = await resumed.request("get_state")
    data = state_resp.get("data", {})
    check("resumed session id preserved", data.get("sessionId") == session_id)
    # 4 transcript prompt records (happy, ask, ask-reply, ship-draft) × 2.
    check("resumed message count restored", data.get("messageCount") == 8,
          str(data.get("messageCount")))
    resp = await resumed.request("get_last_assistant_text")
    check("resumed last-assistant-text restored",
          "<<<COMMIT_MESSAGE>>>" in resp.get("data", {}).get("text", ""))

    await supervisor.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
    if failures:
        print(f"\n{failures}/{total} checks FAILED")
        sys.exit(1)
    print(f"\nall {total} checks passed")
