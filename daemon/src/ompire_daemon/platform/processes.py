"""The shared checked host-subprocess step runner.

One primitive for every daemon operation that runs a finite host-side command
and needs a failure it can show: run the argv with no shell, bound it by a
timeout, capture both streams, and turn any failure into a typed error whose
diagnosis is the command's own output — stderr first, stdout as the fallback a
silent hook or wrapper needs, and the bare exit code or signal when neither
stream says anything.

This module is platform code: it imports the standard library only, knows
nothing about tasks, workspaces, or which command is being run, and never
chooses an argv itself. Callers own command selection, authorization, and the
interpretation of a completed nonzero exit (ADR-0039).
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass

_STDERR_LIMIT = 64 * 1024

# A grandchild holding the pipe open after the direct child died must not hang
# the caller; the post-kill wait is bounded to this many seconds.
_POST_KILL_WAIT = 5


class ProcessStepError(Exception):
    """A checked host subprocess step failed or timed out.

    `stderr` is the diagnosis: the command's stderr, its stdout when stderr
    was silent, or the exit status when neither stream spoke.
    """

    def __init__(self, step: str, stderr: str) -> None:
        super().__init__(f"step {step!r} failed")
        self.step = step
        self.stderr = stderr


@dataclass(frozen=True)
class ProcessStep:
    """One finite host command: a literal argv, a deadline, and an optional
    working directory. The name labels the step in errors."""

    name: str
    argv: list[str]
    timeout: int
    cwd: str | None = None


async def run_process_step(step: ProcessStep) -> str:
    """Run the step's command; return stderr text. Raises on failure/timeout."""
    try:
        process = await asyncio.create_subprocess_exec(
            *step.argv,
            cwd=step.cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        # e.g. a required tool not installed on this host: fail loudly
        # instead of letting the calling job die with the work stuck.
        raise ProcessStepError(step.name, f"cannot exec {step.argv[0]!r}: {exc}") from exc
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=step.timeout
        )
    except asyncio.CancelledError:
        if process.returncode is None:
            process.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.wait(), timeout=_POST_KILL_WAIT)
        raise
    except TimeoutError:
        process.kill()
        # `wait()` also waits for pipe EOF; a grandchild still holding the
        # pipe after the kill must not hang the caller — bound the wait.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.wait(), timeout=_POST_KILL_WAIT)
        raise ProcessStepError(step.name, f"timed out after {step.timeout}s") from None
    stderr = stderr_bytes[-_STDERR_LIMIT:].decode("utf-8", errors="replace")
    code = process.returncode
    assert code is not None  # communicate() has reaped the process
    if code != 0:
        # A hook or gpg wrapper can fail with nothing on stderr; stdout and
        # the exit code are then the only diagnosis (dogfooding: a ship
        # commit died silently and the error carried no cause at all).
        stdout = stdout_bytes[-_STDERR_LIMIT:].decode("utf-8", errors="replace")
        detail = stderr.strip() or stdout.strip()
        if not detail:
            detail = (
                f"killed by signal {-code}"
                if code < 0
                else f"exited with code {code}"
            )
        raise ProcessStepError(step.name, detail)
    return stderr
