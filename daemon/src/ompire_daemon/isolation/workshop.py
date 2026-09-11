"""Workshop container mechanics: status, teardown, sandbox execution, transport.

The daemon never persists live workshop status (design D-3): "does the
container still exist" is answered on demand by running the workshop CLI in
the task's clone, where the single-workshop project makes the name optional.
Statuses: `present` / `absent` / `unknown` (tool missing, error, timeout).

This module also owns the two execution transports into a clone's container:

- `run_sandbox_command` — one finite `workshop exec` command with a deadline.
  It returns the exit code and output as *data*; only the inability to start
  the command or a timeout is a failure it raises. A nonzero exit is the
  caller's to interpret.
- `start_sandbox_process` — a long-lived piped process inside the container
  for a caller that owns the protocol spoken over its streams. The module
  knows argv construction and pipes; it does not know `omp`, readiness
  frames, model roles, or conversation ids (ADR-0039).

There is deliberately no host-execution fallback here: arbitrary workflow
commands run only inside the selected workspace's container, and host-side
Git/review operations use the platform process primitives instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections.abc import Sequence
from dataclasses import dataclass
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


class SandboxCommandError(Exception):
    """A sandbox command could not be executed, or exceeded its deadline.

    `timed_out` distinguishes the two. A command that ran and exited nonzero
    is *not* this error — that exit is result data for the caller to route on.
    """

    def __init__(self, detail: str, *, timed_out: bool = False) -> None:
        super().__init__(detail)
        self.detail = detail
        self.timed_out = timed_out


@dataclass(frozen=True)
class SandboxCommandResult:
    """One completed sandbox command. `output` is the decoded captured stream
    (already tailed when a bound was given); `stderr` carries the separate
    stderr tail when the caller asked for unmerged streams, and is empty
    otherwise."""

    exit_code: int
    output: str
    stderr: str = ""


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


def sandbox_command_argv(clone_path: str, argv: Sequence[str]) -> list[str]:
    """The literal `workshop exec` argv that runs `argv` in the clone's
    container. Owned here so no caller constructs container transport itself."""
    return ["workshop", "exec", "-p", clone_path, "--", *argv]


async def run_sandbox_command(
    clone_path: str,
    argv: Sequence[str],
    *,
    timeout: float,
    output_tail: int | None = None,
    merge_stderr: bool = True,
) -> SandboxCommandResult:
    """Run one finite command in the clone's container and return its result.

    The command runs in its own process group so a timeout or a cancellation
    tears down the whole group, not just the transport process. A nonzero exit
    is returned as data; only start failure and timeout raise
    `SandboxCommandError`.

    `merge_stderr` appends the command's stderr to its output (the workflow
    evidence shape); leaving it false captures the two streams separately for
    callers that interpret stderr themselves.
    """
    full_argv = sandbox_command_argv(clone_path, argv)
    try:
        process = await asyncio.create_subprocess_exec(
            *full_argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=(
                asyncio.subprocess.STDOUT if merge_stderr else asyncio.subprocess.PIPE
            ),
            start_new_session=True,
        )
    except OSError as exc:
        raise SandboxCommandError(f"cannot exec 'workshop': {exc}") from exc
    try:
        output_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=timeout
        )
    except (asyncio.CancelledError, TimeoutError) as exc:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.communicate()
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise SandboxCommandError(
            f"command timed out after {timeout:g}s", timed_out=True
        ) from None
    output = output_bytes.decode("utf-8", errors="replace")
    if output_tail is not None:
        output = output[-output_tail:]
    stderr = (
        ""
        if merge_stderr
        else stderr_bytes.decode("utf-8", errors="replace")[-_STDERR_LIMIT:]
    )
    exit_code = process.returncode if process.returncode is not None else -1
    return SandboxCommandResult(exit_code=exit_code, output=output, stderr=stderr)


async def start_sandbox_process(
    clone_path: str,
    argv: Sequence[str],
    *,
    stream_limit: int,
) -> asyncio.subprocess.Process:
    """Start one long-lived piped process in the clone's container.

    stdin/stdout/stderr are pipes, and the stream reader limit is the caller's
    declared frame size. The caller owns the protocol spoken over the pipes,
    readiness, supervision, and teardown; this function only starts the
    process. Raises nothing on its own — an `OSError` (tool missing) is the
    caller's to classify.
    """
    return await asyncio.create_subprocess_exec(
        *sandbox_command_argv(clone_path, argv),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=stream_limit,
    )
