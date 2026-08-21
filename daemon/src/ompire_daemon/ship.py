"""Daemon-run ship flow: draft → squash → sign → push → PR.

Implements SPEC Decision 7's "shipping on approval" (ROADMAP #13).  The
only durable artifact is `tasks.pr_url`; everything else is transient, in
memory state mirroring `ReviewManager`.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import Engine

from ompire_daemon.config import Config
from ompire_daemon.events import EventHub
from ompire_daemon.registry.projects import Project, get_project
from ompire_daemon.registry.tasks import Task, mark_pr_url
from ompire_daemon.registry.templates import get_template
from ompire_daemon.review import _run_git_output
from ompire_daemon.sessions import wait_for_idle
from ompire_daemon.spawn import Step, _run_step

if TYPE_CHECKING:
    from ompire_daemon.agent import AgentSupervisor
    from ompire_daemon.gpg import GpgProbe, GpgStatus
    from ompire_daemon.sessions import SessionTracker

logger = logging.getLogger(__name__)

_SHIP_ORIGIN_NAME = "origin"
_SHIP_GIT_REF = "refs/ompire/ship-orig"

_GITHUB_SSH_RE = re.compile(r"^git@github\.com:(.+?)(?:\.git)?/?$")
_GITHUB_HTTPS_RE = re.compile(r"^https://github\.com/(.+?)(?:\.git)?/?$")
_PR_URL_RE = re.compile(r"https://github\.com/[^/\s]+/[^/\s]+/pull/\d+")

_DRAFT_PROMPT = """You are helping the operator ship this task.

Please propose:
1. A concise commit message for the final squash commit.
2. A PR title.
3. A PR body (a few sentences is fine).

Return exactly three blocks in this order, using these literal markers:

<<<COMMIT_MESSAGE>>>
<the commit message>

<<<PR_TITLE>>>
<the PR title>

<<<PR_BODY>>>
<the PR body>
"""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class ShipDraft:
    commit_message: str
    pr_title: str
    pr_body: str
    source: str = "agent"


@dataclass
class ShipState:
    status: str  # drafting | drafted | committing | pushing | shipped | error
    mode: str = "squash"
    draft: ShipDraft | None = None
    commit_sha: str | None = None
    pr_url: str | None = None
    error: str | None = None
    updated_at: str = field(default_factory=_now_iso)


class ShipError(Exception):
    """Base for ship-manager errors surfaced as ship outcomes."""


class GpgLockedError(ShipError):
    def __init__(self, status: GpgStatus) -> None:
        super().__init__("GPG signing key is not cached")
        self.status = status


class NoLiveAgentError(ShipError):
    def __init__(self, task_id: int) -> None:
        super().__init__(f"task {task_id} has no live agent")
        self.task_id = task_id


class ShipInProgressError(ShipError):
    def __init__(self, task_id: int) -> None:
        super().__init__(f"task {task_id} already has a ship in flight")
        self.task_id = task_id


# --- URL parsing helpers --------------------------------------------------


def parse_github_slug(url: str) -> str:
    """`owner/name` from a GitHub SSH or HTTPS URL."""
    match = _GITHUB_SSH_RE.match(url) or _GITHUB_HTTPS_RE.match(url)
    if match is None:
        raise ValueError(f"not a github.com URL: {url!r}")
    path = match.group(1).strip("/")
    if path.count("/") != 1 or not all(path.split("/")):
        raise ValueError(f"could not parse owner/name from {url!r}")
    return path


def parse_github_owner(url: str) -> str:
    """The owner component of a GitHub SSH or HTTPS URL."""
    return parse_github_slug(url).split("/", 1)[0]


# --- manager --------------------------------------------------------------


class ShipManager:
    def __init__(
        self,
        config: Config,
        engine: Engine,
        hub: EventHub,
        sessions: SessionTracker,
        agents: AgentSupervisor,
        gpg: GpgProbe,
    ) -> None:
        self._config = config
        self._engine = engine
        self._hub = hub
        self._sessions = sessions
        self._agents = agents
        self._gpg = gpg
        self._ships: dict[int, ShipState] = {}
        self._backgrounds: dict[int, asyncio.Task] = {}

    def snapshot(self) -> dict[int, dict[str, Any]]:
        """Current ship states for the WebSocket snapshot (design D-7)."""
        return {task_id: asdict(state) for task_id, state in self._ships.items()}

    def get(self, task_id: int) -> ShipState | None:
        return self._ships.get(task_id)

    def drop_ship(self, task_id: int) -> None:
        """Drop in-memory ship state (cleanup/purge path)."""
        task = self._backgrounds.pop(task_id, None)
        if task is not None:
            task.cancel()
        self._ships.pop(task_id, None)

    async def cancel_and_drop(self, task_id: int) -> None:
        """Cancel an active ship if one exists, then drop state."""
        await self._cancel_background(task_id)
        self.drop_ship(task_id)

    async def _cancel_background(self, task_id: int) -> None:
        task = self._backgrounds.get(task_id)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # --- public operations -------------------------------------------------

    async def draft(self, task: Task) -> ShipState:
        """Ask the primary session's live agent for commit/PR text
        (workflow-engine design D-8); on failure degrade to manual entry via
        a `ship_draft` event with `source="manual"`.
        """
        from ompire_daemon.workflows import get_workflow

        primary = get_workflow(task.workflow_name).primary
        handle = self._agents.get(task.id, primary)
        if handle is None or handle.returncode is not None:
            raise NoLiveAgentError(task.id)

        self._set_state(task.id, status="drafting", draft=None, error=None)

        try:
            await handle.prompt(_DRAFT_PROMPT)
            await wait_for_idle(
                self._hub, task.id, primary, timeout=self._config.spawn_step_timeout
            )

            try:
                response = await handle.request("get_last_assistant_text")
            except Exception as exc:
                raise ShipError(f"agent request failed: {exc}") from exc

            # Live omp wraps the text: {"data": {"text": ...}} — the same
            # shape advisories.py reads (found via dogfooding: reading data
            # as a bare string made every draft fail against real omp).
            data = response.get("data") if isinstance(response, dict) else None
            text = data.get("text") if isinstance(data, dict) else None
            if not isinstance(text, str):
                raise ShipError("agent did not return text for draft")

            parsed = _parse_draft(text)
            if parsed is None:
                raise ShipError("could not parse draft markers from agent reply")

            state = self._set_state(
                task.id, status="drafted", draft=parsed, error=None
            )
            self._hub.publish(
                "ship_draft", {"task_id": task.id, "draft": asdict(parsed)}
            )
            return state
        except TimeoutError:
            state = self._set_state(
                task.id, status="error", draft=None, error="timed out waiting for agent draft"
            )
            return state
        except Exception as exc:  # noqa: BLE001
            state = self._set_state(
                task.id, status="error", draft=None, error=f"draft failed: {exc}"
            )
            return state

    def seed_commit(self, task_id: int, mode: str = "squash") -> ShipState:
        """Synchronously seed the committing state for the REST route, which
        then backgrounds `commit_and_ship`.
        """
        state = self._set_state(task_id, status="committing", error=None, mode=mode)
        self._hub.publish(
            "ship_step",
            {"task_id": task_id, "step": "commit", "status": "started"},
        )
        return state

    async def commit_and_ship(
        self,
        task: Task,
        message: str,
        pr_title: str,
        pr_body: str,
        mode: str = "squash",
    ) -> ShipState:
        """Sign, push, and open a PR for `task` in `squash` or `retain` mode."""
        status = await self._gpg.probe()
        if status.state != "cached":
            raise GpgLockedError(status)

        if mode not in ("squash", "retain"):
            raise ShipError(f"ship mode {mode!r} is not supported")

        existing = self._ships.get(task.id)
        self._set_state(task.id, status="committing", error=None, mode=mode)
        if existing is None or existing.status != "committing":
            self._hub.publish(
                "ship_step",
                {"task_id": task.id, "step": "commit", "status": "started"},
            )

        project = self._project(task)
        base_branch = self._base_branch(task)
        clone_path = task.clone_path
        timeout = self._config.spawn_step_timeout

        try:
            await self._fetch(clone_path, timeout)
            await self._save_ship_orig(clone_path, timeout)
            base = await self._merge_base(clone_path, base_branch, timeout)
            if mode == "squash":
                sha, commit_count = await self._squash_commit(
                    clone_path, base, message, timeout
                )
            else:
                sha, commit_count = await self._retain_rewrite(
                    task, clone_path, base, timeout
                )

            self._set_state(task.id, commit_sha=sha)
            self._hub.publish(
                "ship_step",
                {
                    "task_id": task.id,
                    "step": "commit",
                    "status": "ok",
                    "detail": {"sha": sha, "count": commit_count},
                },
            )
        except Exception as exc:  # noqa: BLE001
            message = f"commit failed: {exc}"
            self._set_state(task.id, status="error", error=message)
            self._hub.publish(
                "ship_step",
                {
                    "task_id": task.id,
                    "step": "commit",
                    "status": "failed",
                    "detail": message,
                },
            )
            self._hub.publish("ship_finished", {"task_id": task.id, "status": "error"})
            return self._ships[task.id]
        finally:
            await self._delete_ship_orig(clone_path, timeout)

        self._set_state(task.id, status="pushing")
        try:
            pr_url = await self._push_and_pr(task, project, base_branch, pr_title, pr_body)
        except Exception as exc:  # noqa: BLE001
            message = f"push/PR failed: {exc}"
            self._set_state(task.id, status="error", error=message)
            self._hub.publish(
                "ship_step",
                {
                    "task_id": task.id,
                    "step": "pr",
                    "status": "failed",
                    "detail": message,
                },
            )
            self._hub.publish("ship_finished", {"task_id": task.id, "status": "error"})
            return self._ships[task.id]

        updated = mark_pr_url(self._engine, task.id, pr_url)
        self._hub.publish("task_updated", asdict(updated))
        state = self._set_state(
            task.id, status="shipped", pr_url=pr_url, error=None
        )
        self._hub.publish(
            "ship_finished",
            {"task_id": task.id, "status": "shipped", "pr_url": pr_url},
        )
        return state

    # --- git / gh steps ----------------------------------------------------

    def _project(self, task: Task) -> Project:
        return get_project(self._engine, task.project_name)

    def _base_branch(self, task: Task) -> str:
        """`<base>` for the squash/PR: the task's template base branch
        (templates capability, design D-3). Tasks that predate templates
        (null `template_name`) fall back to `main`."""
        if task.template_name is None:
            return "main"
        return get_template(self._engine, task.template_name).base_branch

    async def _fetch(self, clone_path: str, timeout: int) -> None:
        await _run_step(
            Step(
                "ship-fetch",
                ["git", "-C", clone_path, "fetch", _SHIP_ORIGIN_NAME],
                timeout,
            )
        )

    async def _save_ship_orig(self, clone_path: str, timeout: int) -> str:
        orig = (
            await _run_git_output(
                ["git", "-C", clone_path, "rev-parse", "HEAD"],
                clone_path,
                timeout,
                "ship-save-orig",
            )
        ).strip()
        if not orig:
            raise ShipError("could not capture current HEAD for ship restore")
        await _run_step(
            Step(
                "ship-save-orig-ref",
                ["git", "-C", clone_path, "update-ref", _SHIP_GIT_REF, orig],
                timeout,
            )
        )
        return orig

    async def _merge_base(
        self, clone_path: str, base_branch: str, timeout: int
    ) -> str:
        base = (
            await _run_git_output(
                [
                    "git",
                    "-C",
                    clone_path,
                    "merge-base",
                    f"{_SHIP_ORIGIN_NAME}/{base_branch}",
                    "HEAD",
                ],
                clone_path,
                timeout,
                "ship-merge-base",
            )
        ).strip()
        if not base:
            raise ShipError("could not compute merge-base for squash")
        return base

    async def _soft_reset(self, clone_path: str, base: str, timeout: int) -> None:
        await _run_step(
            Step(
                "ship-soft-reset",
                ["git", "-C", clone_path, "reset", "--soft", base],
                timeout,
            )
        )

    async def _commit(
        self, clone_path: str, message: str, timeout: int
    ) -> str:
        name = await self._git_config(clone_path, "user.name")
        email = await self._git_config(clone_path, "user.email")

        def write_message() -> str:
            fd, path = tempfile.mkstemp(
                dir=clone_path, prefix="ompire-ship-msg-", suffix=".txt"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(message)
            return path

        msg_path = await asyncio.to_thread(write_message)
        try:
            argv: list[str] = ["git", "-C", clone_path]
            if name:
                argv += ["-c", f"user.name={name}"]
            if email:
                argv += ["-c", f"user.email={email}"]
            argv += ["commit", "-S", "-F", msg_path]

            await _run_step(Step("ship-commit", argv, timeout))
            sha = (
                await _run_git_output(
                    ["git", "-C", clone_path, "rev-parse", "HEAD"],
                    clone_path,
                    timeout,
                    "ship-rev-parse",
                )
            ).strip()
            if not sha:
                raise ShipError("could not read commit sha after signing")
            return sha
        finally:
            await asyncio.to_thread(Path(msg_path).unlink)

    async def _squash_commit(
        self, clone_path: str, base: str, message: str, timeout: int
    ) -> tuple[str, int]:
        """Soft-reset to merge-base and create one signed operator commit."""
        await self._soft_reset(clone_path, base, timeout)
        try:
            sha = await self._commit(clone_path, message, timeout)
        except Exception:
            await self._restore_ship_orig(clone_path, timeout)
            raise
        return sha, 1

    async def check_retain_preconditions(self, task: Task) -> None:
        """Fast preflight for retain mode; raises ShipError on 409 conditions."""
        clone_path = task.clone_path
        timeout = self._config.spawn_step_timeout
        base_branch = self._base_branch(task)
        await self._fetch(clone_path, timeout)
        base = await self._merge_base(clone_path, base_branch, timeout)
        await self._assert_retain_preconditions(clone_path, base, timeout)

    async def _assert_retain_preconditions(
        self, clone_path: str, base: str, timeout: int
    ) -> None:
        stdout, stderr, code = await _run_command(
            ["git", "-C", clone_path, "status", "--porcelain"],
            clone_path,
            timeout,
        )
        if code != 0:
            raise ShipError(f"git status failed: {stderr}")
        if stdout.strip():
            raise ShipError("working tree is dirty; commit or stash changes before retain mode")

        range_commits = (
            await _run_git_output(
                ["git", "-C", clone_path, "rev-list", f"{base}..HEAD"],
                clone_path,
                timeout,
                "ship-retain-rev-list",
            )
        ).strip()
        if not range_commits:
            raise ShipError("no commits to retain")

        merges = (
            await _run_git_output(
                ["git", "-C", clone_path, "rev-list", "--merges", f"{base}..HEAD"],
                clone_path,
                timeout,
                "ship-retain-merges",
            )
        ).strip()
        if merges:
            raise ShipError("range contains merge commits; use squash mode")

    async def _retain_rewrite(
        self, task: Task, clone_path: str, base: str, timeout: int
    ) -> tuple[str, int]:
        """Rewrite merge-base..HEAD in place with operator-authored signed commits."""
        await self._assert_retain_preconditions(clone_path, base, timeout)

        range_commits = (
            await _run_git_output(
                ["git", "-C", clone_path, "rev-list", f"{base}..HEAD"],
                clone_path,
                timeout,
                "ship-retain-count",
            )
        ).strip()
        pre_count = len(range_commits.splitlines())

        name = await self._git_config(clone_path, "user.name")
        email = await self._git_config(clone_path, "user.email")

        argv = ["git", "-C", clone_path, "-c", "sequence.editor=true"]
        if name:
            argv += ["-c", f"user.name={name}"]
        if email:
            argv += ["-c", f"user.email={email}"]
        argv += [
            "rebase",
            base,
            "--keep-empty",
            "--empty=keep",
            "--exec",
            "git commit --amend --no-edit --reset-author -S",
        ]

        try:
            await _run_step(Step("ship-retain-rebase", argv, timeout))
        except Exception:
            await self._retain_restore(clone_path, timeout)
            raise

        post_count_str = await _run_git_output(
            ["git", "-C", clone_path, "rev-list", "--count", f"{base}..HEAD"],
            clone_path,
            timeout,
            "ship-retain-post-count",
        )
        post_count = int(post_count_str.strip())
        if post_count != pre_count:
            await self._retain_restore(clone_path, timeout)
            raise ShipError(
                f"retain rewrite changed commit count: {post_count} != {pre_count}"
            )

        sigs = (
            await _run_git_output(
                ["git", "-C", clone_path, "log", "--format=%G?", f"{base}..HEAD"],
                clone_path,
                timeout,
                "ship-retain-sigs",
            )
        ).strip().splitlines()
        bad = [s for s in sigs if s not in ("G", "U")]
        if bad:
            await self._retain_restore(clone_path, timeout)
            raise ShipError("retain rewrite produced commits without good signatures")

        sha = (
            await _run_git_output(
                ["git", "-C", clone_path, "rev-parse", "HEAD"],
                clone_path,
                timeout,
                "ship-retain-sha",
            )
        ).strip()
        if not sha:
            raise ShipError("could not read commit sha after retain rewrite")
        return sha, post_count

    async def _retain_restore(self, clone_path: str, timeout: int) -> None:
        """Abort an in-progress rebase and restore the pre-ship HEAD."""
        clone = Path(clone_path)
        for state_dir in (
            clone / ".git" / "rebase-merge",
            clone / ".git" / "rebase-apply",
        ):
            if state_dir.exists():
                await _run_step(
                    Step(
                        "ship-retain-abort",
                        ["git", "-C", clone_path, "rebase", "--abort"],
                        timeout,
                    )
                )
                break
        await _run_step(
            Step(
                "ship-retain-restore",
                ["git", "-C", clone_path, "reset", "--hard", _SHIP_GIT_REF],
                timeout,
            )
        )
        await self._delete_ship_orig(clone_path, timeout)

    async def _git_config(self, clone_path: str, key: str) -> str | None:
        try:
            out = await _run_git_output(
                ["git", "-C", clone_path, "config", "--get", key],
                clone_path,
                10,
                f"git-config-{key.replace('.', '-')}",
            )
        except Exception:  # noqa: BLE001 — missing config is fine
            return None
        value = out.strip()
        return value if value else None

    async def _restore_ship_orig(self, clone_path: str, timeout: int) -> None:
        await _run_step(
            Step(
                "ship-restore-orig",
                ["git", "-C", clone_path, "reset", "--soft", _SHIP_GIT_REF],
                timeout,
            )
        )

    async def _delete_ship_orig(self, clone_path: str, timeout: int) -> None:
        await _run_step(
            Step(
                "ship-delete-orig-ref",
                ["git", "-C", clone_path, "update-ref", "-d", _SHIP_GIT_REF],
                timeout,
            )
        )

    async def _push_and_pr(
        self, task: Task, project: Project, base_branch: str, pr_title: str, pr_body: str
    ) -> str:
        clone_path = task.clone_path
        branch = task.branch

        if project.fork_url:
            remote_url = project.fork_url
            head = f"{parse_github_owner(project.fork_url)}:{branch}"
        else:
            # Task clones are hardlink-cloned from the local checkout, so their
            # `origin` is a *local path* — pushing to `origin` never reaches
            # GitHub (found via dogfooding). Push to the upstream URL instead.
            remote_url = project.upstream_url
            head = branch

        self._hub.publish(
            "ship_step",
            {"task_id": task.id, "step": "push", "status": "started"},
        )
        await self._push(clone_path, remote_url, branch)
        self._hub.publish(
            "ship_step", {"task_id": task.id, "step": "push", "status": "ok"}
        )

        self._hub.publish(
            "ship_step",
            {"task_id": task.id, "step": "pr", "status": "started"},
        )
        upstream_slug = parse_github_slug(project.upstream_url)
        pr_url = await self._create_pr(
            clone_path, upstream_slug, base_branch, head, pr_title, pr_body
        )
        self._hub.publish(
            "ship_step",
            {"task_id": task.id, "step": "pr", "status": "ok", "detail": pr_url},
        )
        return pr_url

    async def _push(
        self, clone_path: str, remote_url: str, branch: str
    ) -> None:
        # Push via a named remote + fetch: `--force-with-lease` needs
        # remote-tracking refs to lease against; git rejects lease pushes to
        # bare URLs with "stale info" (found via dogfooding).
        name = await self._ensure_ship_remote(
            clone_path, remote_url, self._config.spawn_step_timeout
        )
        await _run_step(
            Step(
                "ship-push",
                [
                    "git",
                    "-C",
                    clone_path,
                    "push",
                    name,
                    f"HEAD:refs/heads/{branch}",
                    "--force-with-lease",
                ],
                self._config.spawn_step_timeout,
            )
        )

    async def _ensure_ship_remote(
        self, clone_path: str, remote_url: str, timeout: int
    ) -> str:
        """Point the `ship-target` remote at the push destination and fetch it."""
        name = "ship-target"
        existing = await _run_git_output(
            ["git", "-C", clone_path, "remote"],
            cwd=clone_path,
            timeout=timeout,
            step_name="ship-remote-list",
        )
        if name in existing.split():
            await _run_step(
                Step(
                    "ship-remote-set-url",
                    ["git", "-C", clone_path, "remote", "set-url", name, remote_url],
                    timeout,
                )
            )
        else:
            await _run_step(
                Step(
                    "ship-remote-add",
                    ["git", "-C", clone_path, "remote", "add", name, remote_url],
                    timeout,
                )
            )
        await _run_step(
            Step("ship-fetch-target", ["git", "-C", clone_path, "fetch", name], timeout)
        )
        return name

    async def _create_pr(
        self,
        clone_path: str,
        upstream_slug: str,
        base_branch: str,
        head: str,
        title: str,
        body: str,
    ) -> str:
        def write_body() -> str:
            fd, path = tempfile.mkstemp(
                dir=clone_path, prefix="ompire-pr-body-", suffix=".md"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(body)
            return path

        body_path = await asyncio.to_thread(write_body)
        try:
            argv = [
                *self._config.gh_command,
                "pr",
                "create",
                "--repo",
                upstream_slug,
                "--base",
                base_branch,
                "--head",
                head,
                "--title",
                title,
                "--body-file",
                body_path,
            ]
            stdout, stderr, code = await _run_command(argv, clone_path, self._config.spawn_step_timeout)
        finally:
            await asyncio.to_thread(Path(body_path).unlink)

        if code != 0:
            combined = f"{stdout}\n{stderr}"
            url = _find_pr_url(combined)
            if url:
                return url
            raise ShipError(stderr.strip() or stdout.strip() or f"gh exited {code}")

        url = _find_pr_url(stdout)
        if url:
            return url
        lines = [line for line in stdout.splitlines() if line.strip()]
        if lines:
            return lines[-1]
        raise ShipError("gh pr create succeeded but printed no PR URL")

    # --- helpers ------------------------------------------------------------

    def _set_state(self, task_id: int, **kwargs: Any) -> ShipState:
        state = self._ships.get(task_id)
        if state is None:
            state = ShipState(status="drafting")
            self._ships[task_id] = state
        for key, value in kwargs.items():
            setattr(state, key, value)
        state.updated_at = _now_iso()
        return state

    # --- startup crash-recovery helper -------------------------------------

    @staticmethod
    async def restore_parked_clone(clone_path: str, timeout: int) -> bool:
        """If `refs/ompire/ship-orig` exists, restore HEAD to it and delete
        the marker.
        """
        try:
            await _run_git_output(
                ["git", "-C", clone_path, "rev-parse", "--verify", _SHIP_GIT_REF],
                clone_path,
                timeout,
                "ship-ref-check",
            )
        except Exception:  # noqa: BLE001
            return False
        await _run_step(
            Step(
                "ship-startup-restore",
                ["git", "-C", clone_path, "reset", "--soft", _SHIP_GIT_REF],
                timeout,
            )
        )
        await _run_step(
            Step(
                "ship-startup-delete-ref",
                ["git", "-C", clone_path, "update-ref", "-d", _SHIP_GIT_REF],
                timeout,
            )
        )
        return True


# --- module-level helpers --------------------------------------------------


def _parse_draft(text: str) -> ShipDraft | None:
    markers = ["<<<COMMIT_MESSAGE>>>", "<<<PR_TITLE>>>", "<<<PR_BODY>>>"]
    sections: dict[str, str] = {}
    for i, marker in enumerate(markers):
        idx = text.find(marker)
        if idx == -1:
            return None
        start = idx + len(marker)
        next_marker = len(text)
        for other in markers:
            nxt = text.find(other, start)
            if nxt != -1 and nxt < next_marker:
                next_marker = nxt
        sections[marker] = text[start:next_marker].strip()
    return ShipDraft(
        commit_message=sections["<<<COMMIT_MESSAGE>>>"],
        pr_title=sections["<<<PR_TITLE>>>"],
        pr_body=sections["<<<PR_BODY>>>"],
    )


def _find_pr_url(text: str) -> str | None:
    match = _PR_URL_RE.search(text)
    return match.group(0) if match else None


async def _run_command(
    argv: list[str], cwd: str, timeout: int
) -> tuple[str, str, int]:
    """Run a command, capture stdout/stderr, return both plus exit code."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return "", f"cannot exec {argv[0]!r}: {exc}", 127
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=timeout
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        return "", f"timed out after {timeout}s", 1
    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    return stdout, stderr, process.returncode or 0
