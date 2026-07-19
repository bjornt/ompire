"""Workshop CLI helpers: derived existence checks and teardown (SPEC D5).

The daemon never persists live workshop status (design D-3): "does the
container still exist" is answered on demand by running the workshop CLI in
the task's clone, where the single-workshop project makes the name optional.
Statuses: `present` / `absent` / `unknown` (tool missing, error, timeout).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

_STDERR_LIMIT = 16 * 1024

# Existence checks are interactive-path queries (task detail); keep them snappy.
STATUS_TIMEOUT = 10

# Stderr fragments from the workshop CLI that mean "no container behind this
# clone" — the distinction remove idempotence (design D-4) hinges on. Settled
# against workshop 0.9.3; the dogfood pass re-validates.
_GONE_MARKERS = ("not launched", "not found")


class WorkshopRemoveError(Exception):
    def __init__(self, stderr: str) -> None:
        super().__init__("workshop remove failed")
        self.stderr = stderr


async def _run_workshop(args: list[str], cwd: str, timeout: int) -> tuple[int | None, str]:
    """Run `workshop <args>` in `cwd`; return (returncode, stderr text).

    Returns (None, message) when the tool can't run at all (missing binary,
    bad cwd, timeout) — callers map that to `unknown` or a remove failure.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            "workshop",
            *args,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return None, f"cannot exec 'workshop': {exc}"
    try:
        _, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        return None, f"'workshop {args[0]}' timed out after {timeout}s"
    return process.returncode, stderr_bytes[-_STDERR_LIMIT:].decode("utf-8", errors="replace")


def _is_gone(stderr: str) -> bool:
    lowered = stderr.lower()
    return any(marker in lowered for marker in _GONE_MARKERS)


async def workshop_status(clone_path: str) -> str:
    """Report `present`, `absent`, or `unknown` for the clone's workshop."""
    if not Path(clone_path).is_dir():
        return "absent"
    returncode, stderr = await _run_workshop(["info"], clone_path, STATUS_TIMEOUT)
    if returncode == 0:
        return "present"
    if returncode is not None and _is_gone(stderr):
        return "absent"
    return "unknown"


async def remove_workshop(clone_path: str, timeout: int) -> None:
    """Remove the clone's workshop; an already-gone workshop is success.

    Raises WorkshopRemoveError on any other failure — cleanup must not
    delete the clone under a container it failed to tear down (design D-4).
    """
    if not Path(clone_path).is_dir():
        # No clone to run the CLI in; nothing left to tear down from our side.
        return
    returncode, stderr = await _run_workshop(["remove"], clone_path, timeout)
    if returncode == 0:
        return
    if returncode is not None and _is_gone(stderr):
        return
    raise WorkshopRemoveError(stderr)
