"""Hardened host-side Git invocation.

`safe_git` builds a `git` argv that cannot execute anything the repository
configures; `run_git` executes one such argv with a deadline and captured
streams; `git_out` is the stdout-only convenience over both. Every daemon
owner that reads or writes Git state on the host — candidate capture, review,
publication, result provenance — runs through here instead of growing a second
wrapper with subtly different hooks or environment rules.

This module is platform code: standard library only, no product state, and no
opinion about which Git command is correct or what a failure means. Command
selection, authorization, and failure classification stay with the calling
owner; a technical failure surfaces as `GitCommandError` for it to classify
(ADR-0039).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path


class GitCommandError(Exception):
    """A Git command could not be started, timed out, or exited nonzero."""


def safe_git(clone_path: str | Path, *args: str) -> list[str]:
    """A `git` argv that cannot execute anything the clone configures.

    `core.hooksPath` is pointed at a directory that does not exist, so no
    repository-authored hook runs on the host during capture, signing, or
    push.
    """
    return [
        "git",
        "-C",
        str(clone_path),
        "-c",
        "core.hooksPath=/nonexistent/ompire-no-hooks",
        *args,
    ]


async def run_git(
    argv: list[str],
    *,
    cwd: str | Path,
    timeout: int,
    step: str,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> tuple[str, str, int]:
    """Run one Git command and return `(stdout, stderr, returncode)`."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **(env or {})},
        )
    except OSError as exc:
        raise GitCommandError(f"{step}: cannot exec {argv[0]!r}: {exc}") from exc
    try:
        out_bytes, err_bytes = await asyncio.wait_for(
            process.communicate(), timeout=timeout
        )
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise GitCommandError(f"{step}: timed out after {timeout}s") from exc
    stdout = out_bytes.decode("utf-8", errors="replace")
    stderr = err_bytes.decode("utf-8", errors="replace")
    code = process.returncode or 0
    if check and code != 0:
        raise GitCommandError(
            f"{step} failed: {stderr.strip() or stdout.strip() or f'exit {code}'}"
        )
    return stdout, stderr, code


async def git_out(
    clone_path: str | Path, args: list[str], *, timeout: int, step: str
) -> str:
    stdout, _stderr, _code = await run_git(
        safe_git(clone_path, *args), cwd=clone_path, timeout=timeout, step=step
    )
    return stdout
