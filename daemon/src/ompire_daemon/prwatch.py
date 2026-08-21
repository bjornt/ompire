"""PR state polling (merge-poll capability; SPEC Decision 7 step 3).

A single asyncio loop polls `gh pr view <pr_url> --json state,mergedAt` for
every shipped, not-yet-terminal PR and records transitions durably on the
task row (`pr_state`, `pr_merged_at`), publishing `task_updated` — the
established mutation event — so a reconnecting client sees PR state from the
snapshot without replaying anything.

Failure posture (design D-2): any per-task failure (gh exit≠0, unparseable
JSON, timeout) logs a warning and defers to the next tick. Polling never
marks a task, never skips the rest of the poll set, and never crashes the
loop — a logged-out or offline `gh` self-heals once it recovers.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import asdict
from typing import Any

from sqlalchemy import Engine

from ompire_daemon.config import Config
from ompire_daemon.events import EventHub
from ompire_daemon.registry.tasks import Task, list_pr_pollable_tasks, mark_pr_state
from ompire_daemon.ship import _run_command

logger = logging.getLogger(__name__)

# Per-`gh`-call timeout: a `pr view` is one API round-trip; 30s already means
# the network is in trouble, and the tick must not wedge the loop.
_GH_TIMEOUT = 30

# GitHub's uppercase states, mapped lowercase for the registry.
_STATE_MAP = {"OPEN": "open", "MERGED": "merged", "CLOSED": "closed"}


class PrWatcher:
    """One instance per daemon; started/stopped from the app lifespan."""

    def __init__(self, config: Config, engine: Engine, hub: EventHub) -> None:
        self._config = config
        self._engine = engine
        self._hub = hub
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        """Spawn the poll loop; idempotent. Must be called from a running
        event loop (app lifespan)."""
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception:
                logger.exception("pr poll tick failed")
            await asyncio.sleep(self._config.pr_poll_interval)

    async def poll_once(self) -> None:
        """One tick: poll every pollable task sequentially (the poll set is
        small; serial `gh` calls keep auth/rate-limit behavior boring)."""
        for task in await asyncio.to_thread(list_pr_pollable_tasks, self._engine):
            try:
                await self._poll_task(task)
            except Exception:
                logger.warning("pr poll failed for task %d", task.id, exc_info=True)

    async def _poll_task(self, task: Task) -> None:
        assert task.pr_url is not None  # guaranteed by the poll-set query
        # `gh pr view` takes a full URL and needs no repo cwd; run from the
        # data dir so a manually-deleted clone can never break the poll.
        stdout, stderr, code = await _run_command(
            [*self._config.gh_command, "pr", "view", task.pr_url, "--json", "state,mergedAt"],
            str(self._config.data_dir),
            _GH_TIMEOUT,
        )
        if code != 0:
            logger.warning(
                "gh pr view failed for task %d (%s): %s",
                task.id,
                task.pr_url,
                stderr.strip() or stdout.strip() or f"exit {code}",
            )
            return

        pr_state, merged_at = _parse_pr_view(stdout)
        if pr_state is None:
            logger.warning(
                "unparseable gh pr view output for task %d (%s): %.200r",
                task.id,
                task.pr_url,
                stdout,
            )
            return
        if pr_state == task.pr_state:
            return

        updated = await asyncio.to_thread(
            mark_pr_state, self._engine, task.id, pr_state, merged_at
        )
        logger.info("task %d pr_state -> %s", task.id, pr_state)
        self._hub.publish("task_updated", asdict(updated))


def _parse_pr_view(stdout: str) -> tuple[str | None, str | None]:
    """`(pr_state, merged_at)` from `gh pr view --json state,mergedAt`, or
    `(None, None)` on any surprise — the caller treats that as a poll
    failure (findings 1.1: defensive parse, never a bogus state change)."""
    try:
        data: Any = json.loads(stdout)
    except json.JSONDecodeError:
        return None, None
    if not isinstance(data, dict):
        return None, None
    pr_state = _STATE_MAP.get(data.get("state"))
    if pr_state is None:
        return None, None
    merged_at = data.get("mergedAt")
    if pr_state == "merged" and isinstance(merged_at, str) and merged_at:
        return pr_state, merged_at
    return pr_state, None
