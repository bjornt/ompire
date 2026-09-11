"""Protected delivery candidates.

Architecture: ADR-0032, ADR-0039
(docs/adr/0032-bind-trusted-delivery-to-retained-candidates.md,
docs/adr/0039-own-workspace-resources-behind-isolation.md)

The candidate: one resolution of *what would be published*: the pinned
base branch, the base commit the delta is measured from, the HEAD it was
captured at, the full publishable tree, and — for retain — the ordered source
commits with their trees and messages. Its identity is a hash of that
normalized data, so the same workspace captures to the same candidate and any
change to what would be published captures to a different one. The objects are
copied into an owner-private bare repository outside the workshop mount, so
the task cannot rewrite or garbage-collect what a review graded and a
signature covers.

Everything here runs Git with the clone's own hooks disabled and refuses a
clone that configures content filters. The per-task clone is agent-writable,
so any Git operation the daemon runs in it could otherwise execute task-
authored code on the host as the operator (ADR-0011). The workspace writer
guard and the hardened Git primitives this module used to carry live behind
the isolation and platform boundaries now; candidate identity, protected-path
policy, and clone-safety refusal stay here.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import shutil
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import Engine

from ompire_daemon.config import Config
from ompire_daemon.isolation import ExcludeUpdateError, ensure_git_excludes
from ompire_daemon.platform.git import git_out, run_git, safe_git
from ompire_daemon.registry.ships import (
    CandidateRecord,
    SourceCommit,
    record_candidate,
)
from ompire_daemon.work.tasks import Task

logger = logging.getLogger(__name__)


def _ensure_excludes(clone_path: str, protected: Sequence[str] = ()) -> None:
    """Apply Ompire's own Git exclude policy to the clone.

    `protected` is the task's pinned handoff destinations, passed so the
    delivery path writes exactly the same entries the spawn pipeline did. It
    improves what `git add --all` picks up; it proves nothing, and the tree
    checks below never trust it (ADR-0035).
    """
    try:
        ensure_git_excludes(clone_path, "delivery-exclude", protected=protected)
    except ExcludeUpdateError as exc:
        raise DeliveryWorkspaceError(str(exc)) from exc

# Where candidate staging repositories live: under the daemon's own data
# directory, never inside the task clone or the workshop mount.
CANDIDATE_DIR_NAME = "delivery"

# Field and record separators for the plumbing format below. Git emits them
# from `%x00`/`%x1e` escapes — an argument vector cannot carry a literal NUL —
# and neither byte can appear in an object id or a commit message, so parsing
# stays unambiguous for any message content.
_NUL = "\x00"
_RECORD = "\x1e"
_LOG_FORMAT = "%H%x00%T%x00%P%x00%B%x1e"


class DeliveryWorkspaceError(Exception):
    """A Git or filesystem failure while capturing or storing a candidate."""


class UnsafeCloneConfigError(DeliveryWorkspaceError):
    """The clone configures something the daemon refuses to run under."""


class ProtectedPathError(DeliveryWorkspaceError):
    """The proposed Git result carries a handoff destination (ADR-0035).

    A `DeliveryWorkspaceError` on purpose: review already turns one of those
    into a content refusal before llmvet starts, so contamination cannot be
    hidden behind a successful content review.

    Nothing is filtered, reset, or rewritten to make the refusal go away.
    Ompire does not delete an operator's files or rewrite their history on
    their behalf; it says exactly which paths — and, for retained history,
    which commit — are in the way, and the correction is theirs to make.
    """

    def __init__(self, paths: Sequence[str], *, commit: str | None = None) -> None:
        listed = ", ".join(paths[:8]) + ("…" if len(paths) > 8 else "")
        where = (
            f"commit {commit[:12]} in the range this delivery would publish"
            if commit
            else "the tree this delivery would publish"
        )
        super().__init__(
            f"{where} contains handoff input(s) that are never publishable: "
            f"{listed}. Remove them from what is being published — Ompire will "
            "not delete files or rewrite history for you — then review again."
        )
        self.paths = tuple(paths)
        self.commit = commit


class EmptyCandidateError(DeliveryWorkspaceError):
    """The task has nothing to deliver against its base."""




# --- clone safety ----------------------------------------------------------

# Clone-local settings that would make the daemon run task-authored code, or
# transform content on its way into a signed commit. The clone is agent-
# writable, so these are refused rather than overridden: a workspace that
# configures them is not one Ompire can safely capture.
_REFUSED_LOCAL_CONFIG_PREFIXES = (
    "filter.",
    "core.hookspath",
    "core.fsmonitor",
    "core.gitproxy",
    "core.sshcommand",
    "diff.external",
    "gpg.program",
    "gpg.openpgp.program",
    "uploadpack.packobjectshook",
)


async def assert_clone_config_safe(clone_path: str, timeout: int) -> None:
    """Refuse a clone that configures content filters or host-executed hooks.

    Overriding these per command is not enough for `filter.*`: an in-repo
    `.gitattributes` can name a driver, and the driver's definition lives in the
    clone's own config. Refusing is honest and checkable; silently capturing
    filtered content would mean signing something nobody reviewed.
    """
    stdout, _stderr, code = await run_git(
        safe_git(clone_path, "config", "--local", "--list"),
        cwd=clone_path,
        timeout=timeout,
        step="delivery-config-scan",
        check=False,
    )
    if code != 0:
        return
    offending = sorted(
        {
            line.split("=", 1)[0]
            for line in stdout.splitlines()
            if line.split("=", 1)[0]
            .lower()
            .startswith(_REFUSED_LOCAL_CONFIG_PREFIXES)
        }
    )
    if offending:
        raise UnsafeCloneConfigError(
            "the task clone configures settings Ompire will not run delivery "
            f"under: {', '.join(offending)}. Remove them from the clone's Git "
            "configuration and capture the content again."
        )


# --- candidate capture -----------------------------------------------------


def protected_destinations(task: Task) -> tuple[str, ...]:
    """The paths this task may never publish, from its own pinned inputs.

    Derived from the accepted launch document and nothing else (ADR-0035): not
    from an ignore file, not from a pattern like `epics/` or `PLAN.md`, and not
    from anything the task or its agent supplies. An ordinary task returns
    nothing, which is what keeps its candidate identity and its delivery
    exactly as they were.

    A stored attachment whose classification cannot be read raises out of the
    decoder rather than arriving here as an empty tuple — refusing to publish
    is the only safe reading of a policy nobody can parse.
    """
    inputs = task.execution_inputs
    return inputs.protected_destinations if inputs is not None else ()


@dataclass(frozen=True)
class CandidateInputs:
    """Everything a capture needs that comes from outside the workspace."""

    task_id: int
    clone_path: str
    base_branch: str


def candidate_root(config: Config) -> Path:
    return Path(config.data_dir) / CANDIDATE_DIR_NAME


def candidate_store_path(config: Config, task_id: int, candidate_id: str) -> Path:
    return candidate_root(config) / str(task_id) / f"{candidate_id}.git"


def compute_candidate_id(
    *,
    task_id: int,
    base_commit: str,
    tree_id: str,
    source_commits: tuple[SourceCommit, ...],
    protected: Sequence[str] = (),
) -> str:
    """Hash the normalized semantic content, and only that.

    Not the rendered preview, not the agent's draft, not a timestamp: the same
    workspace has to capture to the same identity across restarts, or "unchanged
    since review" would be unanswerable.

    `protected` is folded in *only when it is non-empty* (ADR-0035). A task with
    no handoff inputs keeps byte-identical identities to the ones it had before
    attachments existed, so nothing already reviewed goes stale; a task that has
    them binds the publication policy to what was reviewed, so a candidate
    cannot be carried into a delivery that thinks the policy is different.
    """
    document: dict[str, Any] = {
        "task_id": task_id,
        "base_commit": base_commit,
        "tree_id": tree_id,
        "source_commits": [
            {
                "commit_id": c.commit_id,
                "tree_id": c.tree_id,
                "message": c.message,
                "parent_ids": list(c.parent_ids),
            }
            for c in source_commits
        ],
    }
    if protected:
        document["protected_paths"] = sorted(protected)
    return hashlib.sha256(
        json.dumps(document, sort_keys=True).encode("utf-8")
    ).hexdigest()


async def protected_paths_in_tree(
    clone_path: str, tree_ish: str, protected: Sequence[str], timeout: int
) -> tuple[str, ...]:
    """Which protected destinations a given tree actually contains.

    Asks the *tree*, never the working directory, the index, or an ignore file:
    an exclude an agent can edit is not evidence, and a file force-staged into
    the agent's own index may never reach the proposed tree at all. What is
    published is a tree, so a tree is what gets inspected.

    `-r` recurses, so a protected file later replaced by a directory is caught
    through its descendants rather than slipping past a name that no longer
    matches a blob. `--literal-pathspecs` keeps a captured filename containing
    Git pathspec metacharacters a path rather than a pattern — a `[` in a
    filename must not turn its own protection into a character class.
    """
    if not protected:
        return ()
    stdout = await git_out(
        clone_path,
        [
            "--literal-pathspecs",
            "ls-tree",
            "-r",
            "-z",
            "--full-tree",
            "--name-only",
            tree_ish,
            "--",
            *protected,
        ],
        timeout=timeout,
        step="candidate-protected-paths",
    )
    return tuple(sorted({name for name in stdout.split("\0") if name.strip()}))


async def assert_tree_unprotected(
    clone_path: str,
    tree_ish: str,
    protected: Sequence[str],
    timeout: int,
    *,
    commit: str | None = None,
) -> None:
    found = await protected_paths_in_tree(clone_path, tree_ish, protected, timeout)
    if found:
        raise ProtectedPathError(found, commit=commit)


async def assert_range_unprotected(
    repo_path: str | Path,
    base: str,
    tip: str,
    protected: Sequence[str],
    timeout: int,
) -> None:
    """Refuse if *any* commit that would be published carries a protected path.

    A final-tree check alone is not enough for retained history: a handoff file
    added in one checkpoint and deleted before HEAD leaves a clean final tree
    and a published commit that still contains it. Anyone reading that branch
    can recover the file, so every tree in the range is inspected — that is the
    difference between "the result is clean" and "nothing published is dirty".

    Squash never reaches here with more than the one commit it creates, which
    is why a contaminated *unpublished* checkpoint does not block a clean
    squash: those commits are not published at all.
    """
    if not protected:
        return
    stdout = await git_out(
        str(repo_path),
        ["log", "--format=%H%x00%T", f"{base}..{tip}"],
        timeout=timeout,
        step="delivery-protected-range",
    )
    for line in stdout.splitlines():
        commit, _sep, tree = line.strip().partition("\0")
        if not commit or not tree:
            continue
        found = await protected_paths_in_tree(
            str(repo_path), tree, protected, timeout
        )
        if found:
            raise ProtectedPathError(found, commit=commit)


async def _capture_tree(clone_path: str, timeout: int) -> str:
    """Write the full publishable tree using a daemon-private index.

    The task's own index is never touched: an agent mid-turn keeps whatever it
    had staged, and a capture that fails leaves no trace in the workspace.
    """
    fd, index_path = tempfile.mkstemp(prefix="ompire-candidate-index-")
    os.close(fd)
    # `read-tree` wants to create the file itself.
    Path(index_path).unlink()
    env = {"GIT_INDEX_FILE": index_path}
    try:
        await run_git(
            safe_git(clone_path, "read-tree", "HEAD"),
            cwd=clone_path,
            timeout=timeout,
            step="candidate-read-tree",
            env=env,
        )
        # `--all` picks up modifications, deletions, and non-ignored untracked
        # files — the delta an agent actually leaves behind. Ompire's own
        # excludes keep the workshop lock and outcome directory out.
        await run_git(
            safe_git(clone_path, "add", "--all"),
            cwd=clone_path,
            timeout=timeout,
            step="candidate-stage",
            env=env,
        )
        stdout, _stderr, _code = await run_git(
            safe_git(clone_path, "write-tree"),
            cwd=clone_path,
            timeout=timeout,
            step="candidate-write-tree",
            env=env,
        )
        tree = stdout.strip()
        if not tree:
            raise DeliveryWorkspaceError("could not write the candidate tree")
        return tree
    finally:
        with contextlib.suppress(OSError):
            Path(index_path).unlink()


async def _capture_source_commits(
    clone_path: str, base: str, timeout: int
) -> tuple[SourceCommit, ...]:
    """The ordered `base..HEAD` commits with the trees and messages retain
    mode has to preserve exactly."""
    stdout = await git_out(
        clone_path,
        ["log", "--reverse", f"--format={_LOG_FORMAT}", f"{base}..HEAD"],
        timeout=timeout,
        step="candidate-source-commits",
    )
    commits: list[SourceCommit] = []
    for chunk in stdout.split(_RECORD):
        entry = chunk.strip("\n")
        if not entry.strip():
            continue
        parts = entry.split(_NUL)
        if len(parts) < 4:
            raise DeliveryWorkspaceError("could not read the candidate's commit range")
        commit_id, tree_id, parents, message = parts[0], parts[1], parts[2], parts[3]
        commits.append(
            SourceCommit(
                commit_id=commit_id.strip(),
                tree_id=tree_id.strip(),
                message=message,
                parent_ids=tuple(p for p in parents.split() if p),
            )
        )
    return tuple(commits)


async def _store_candidate_objects(
    clone_path: str,
    store: Path,
    *,
    base: str,
    original_head: str,
    tree_id: str,
    timeout: int,
) -> None:
    """Copy exactly the objects the candidate needs into its own repository.

    A bare repository with three refs, not a second writable checkout of the
    task's `.git` and not a copy of its configuration: the point is to hold the
    reviewed objects where the task cannot reach them, with nothing that could
    carry clone-local settings or credentials along.
    """
    store.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not (store / "HEAD").exists():
        if store.exists():
            shutil.rmtree(store)
        await run_git(
            ["git", "init", "--bare", "--quiet", str(store)],
            cwd=store.parent,
            timeout=timeout,
            step="candidate-store-init",
        )
        store.chmod(0o700)
    # A commit object makes the candidate tree reachable, so the store's own
    # garbage collection cannot drop it. It is evidence, never the signed
    # result: the signed commit is built later with the operator's message,
    # identity, and signature.
    stdout, _stderr, _code = await run_git(
        safe_git(clone_path, "commit-tree", tree_id, "-p", base, "-m", "ompire candidate"),
        cwd=clone_path,
        timeout=timeout,
        step="candidate-commit-tree",
        env={
            "GIT_AUTHOR_NAME": "ompire",
            "GIT_AUTHOR_EMAIL": "ompire@localhost",
            "GIT_AUTHOR_DATE": "@0 +0000",
            "GIT_COMMITTER_NAME": "ompire",
            "GIT_COMMITTER_EMAIL": "ompire@localhost",
            "GIT_COMMITTER_DATE": "@0 +0000",
        },
    )
    candidate_commit = stdout.strip()
    await run_git(
        safe_git(
            clone_path,
            "push",
            "--quiet",
            "--force",
            str(store),
            f"{base}:refs/ompire/base",
            f"{original_head}:refs/ompire/head",
            f"{candidate_commit}:refs/ompire/candidate",
        ),
        cwd=clone_path,
        timeout=timeout,
        step="candidate-store-push",
    )


async def capture_candidate(
    config: Config,
    engine: Engine,
    task: Task,
    *,
    base_branch: str,
    fetch: bool = True,
) -> CandidateRecord:
    """Resolve and protect what this task would publish, right now.

    Read-only against the task's workspace: the working tree, index, HEAD and
    branch are all left exactly as they were. Callers hold the workspace guard
    around this so the capture and whatever it is compared against cannot be
    separated by another writer.
    """
    clone_path = task.clone_path
    timeout = config.spawn_step_timeout
    protected = protected_destinations(task)

    await assert_clone_config_safe(clone_path, timeout)
    await asyncio.to_thread(_ensure_excludes, clone_path, protected)
    if fetch:
        await run_git(
            safe_git(clone_path, "fetch", "origin"),
            cwd=clone_path,
            timeout=timeout,
            step="candidate-fetch",
        )

    original_head = (
        await git_out(
            clone_path, ["rev-parse", "HEAD"], timeout=timeout, step="candidate-head"
        )
    ).strip()
    if not original_head:
        raise DeliveryWorkspaceError("could not read the task's current HEAD")
    base = (
        await git_out(
            clone_path,
            ["merge-base", f"origin/{base_branch}", "HEAD"],
            timeout=timeout,
            step="candidate-merge-base",
        )
    ).strip()
    if not base:
        raise DeliveryWorkspaceError(
            f"could not compute the merge-base against origin/{base_branch}"
        )

    tree_id = await _capture_tree(clone_path, timeout)
    base_tree = (
        await git_out(
            clone_path,
            ["rev-parse", f"{base}^{{tree}}"],
            timeout=timeout,
            step="candidate-base-tree",
        )
    ).strip()
    if tree_id == base_tree:
        raise EmptyCandidateError(
            "there is nothing to deliver: the task's content is identical to "
            f"its base ({base[:12]})."
        )
    # Mode-neutral, and before anything is retained or reviewed: a proposed
    # tree carrying a handoff input is refused whichever way it would be
    # committed. The base tree is checked too — an upstream-tracked path at a
    # protected destination would otherwise be published implicitly, without
    # this task having done anything wrong.
    await assert_tree_unprotected(clone_path, base_tree, protected, timeout)
    await assert_tree_unprotected(clone_path, tree_id, protected, timeout)

    source_commits = await _capture_source_commits(clone_path, base, timeout)
    status = (
        await git_out(
            clone_path,
            ["status", "--porcelain"],
            timeout=timeout,
            step="candidate-status",
        )
    ).strip()

    candidate_id = compute_candidate_id(
        task_id=task.id,
        base_commit=base,
        tree_id=tree_id,
        source_commits=source_commits,
        protected=protected,
    )
    store = candidate_store_path(config, task.id, candidate_id)
    await _store_candidate_objects(
        clone_path,
        store,
        base=base,
        original_head=original_head,
        tree_id=tree_id,
        timeout=timeout,
    )
    return record_candidate(
        engine,
        candidate_id=candidate_id,
        task_id=task.id,
        base_branch=base_branch,
        base_commit=base,
        original_head=original_head,
        tree_id=tree_id,
        source_commits=source_commits,
        dirty=bool(status),
        storage_path=str(store),
    )


async def workspace_tree_id(config: Config, task: Task) -> str:
    """The tree the task's working files would publish as, right now.

    Deliberately narrower than a candidate identity: installing a signed result
    *changes* the identity — the commits are Ompire's own now — while the tree
    is exactly what was reviewed. Comparing trees is how "did the operator's
    files change under us?" gets asked without the signature's own rewrite
    answering yes.
    """
    clone_path = task.clone_path
    timeout = config.spawn_step_timeout
    protected = protected_destinations(task)
    await assert_clone_config_safe(clone_path, timeout)
    await asyncio.to_thread(_ensure_excludes, clone_path, protected)
    tree_id = await _capture_tree(clone_path, timeout)
    await assert_tree_unprotected(clone_path, tree_id, protected, timeout)
    return tree_id


async def candidate_identity(
    config: Config, task: Task, *, base_branch: str, fetch: bool = False
) -> str:
    """The identity the task's workspace would capture to right now, without
    storing anything.

    How "is this still the reviewed content?" is answered before a delivery is
    accepted and before each dependent action. Raises rather than returning a
    sentinel: an empty delta and an unreadable workspace are different refusals,
    and collapsing them would make one of them unexplainable.
    """
    clone_path = task.clone_path
    timeout = config.spawn_step_timeout
    protected = protected_destinations(task)
    await assert_clone_config_safe(clone_path, timeout)
    await asyncio.to_thread(_ensure_excludes, clone_path, protected)
    if fetch:
        await run_git(
            safe_git(clone_path, "fetch", "origin"),
            cwd=clone_path,
            timeout=timeout,
            step="candidate-fetch",
        )
    base = (
        await git_out(
            clone_path,
            ["merge-base", f"origin/{base_branch}", "HEAD"],
            timeout=timeout,
            step="candidate-merge-base",
        )
    ).strip()
    if not base:
        raise DeliveryWorkspaceError(
            f"could not compute the merge-base against origin/{base_branch}"
        )
    tree_id = await _capture_tree(clone_path, timeout)
    base_tree = (
        await git_out(
            clone_path,
            ["rev-parse", f"{base}^{{tree}}"],
            timeout=timeout,
            step="candidate-base-tree",
        )
    ).strip()
    if tree_id == base_tree:
        raise EmptyCandidateError(
            "there is nothing to deliver: the task's content is identical to "
            f"its base ({base[:12]})."
        )
    await assert_tree_unprotected(clone_path, base_tree, protected, timeout)
    await assert_tree_unprotected(clone_path, tree_id, protected, timeout)
    source_commits = await _capture_source_commits(clone_path, base, timeout)
    return compute_candidate_id(
        task_id=task.id,
        base_commit=base,
        tree_id=tree_id,
        source_commits=source_commits,
        protected=protected,
    )


async def store_has_objects(store: Path, oids: list[str], timeout: int) -> bool:
    """Whether the candidate's own repository still holds the named objects."""
    if not (store / "HEAD").exists():
        return False
    for oid in oids:
        _out, _err, code = await run_git(
            ["git", "-C", str(store), "cat-file", "-e", f"{oid}^{{object}}"],
            cwd=store,
            timeout=timeout,
            step="candidate-store-verify",
            check=False,
        )
        if code != 0:
            return False
    return True


def remove_candidate_storage(path: str | Path) -> None:
    """Delete one candidate staging repository. Safe to call twice."""
    with contextlib.suppress(OSError):
        shutil.rmtree(path)


# --- review view -----------------------------------------------------------


def review_view_path(config: Config, task_id: int, candidate_id: str) -> Path:
    return candidate_root(config) / str(task_id) / f"review-{candidate_id[:16]}"


async def prepare_review_view(
    config: Config, task_id: int, candidate: CandidateRecord
) -> Path:
    """Build the isolated checkout the reviewer reads.

    HEAD and the index sit at the candidate's base commit while the working
    tree holds the candidate's full tree, so `git status` and `git diff` expose
    exactly the delta the reset dance used to expose inside the task clone —
    committed work and pending edits together, as one change to review.

    The difference is where it lives. The reviewer no longer reads the task's
    live tree, so an agent that keeps working cannot alter what is being
    reviewed mid-review, and a crashed daemon leaves no parked task clone to
    restore. What such activity *can* do is change the task's current candidate,
    which makes the resulting approval unusable for delivery — visibly, rather
    than by quietly reviewing one thing and signing another.
    """
    timeout = config.spawn_step_timeout
    store = candidate.storage_path
    if store is None or not (Path(store) / "HEAD").exists():
        raise DeliveryWorkspaceError(
            "the reviewed content is no longer available in its protected store"
        )
    view = review_view_path(config, task_id, candidate.candidate_id)
    if view.exists():
        shutil.rmtree(view)
    view.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    await run_git(
        ["git", "init", "--quiet", str(view)],
        cwd=view.parent,
        timeout=timeout,
        step="review-view-init",
    )
    view.chmod(0o700)
    await run_git(
        [
            "git",
            "-C",
            str(view),
            "fetch",
            "--quiet",
            store,
            "+refs/ompire/*:refs/ompire/*",
        ],
        cwd=view,
        timeout=timeout,
        step="review-view-fetch",
    )
    await run_git(
        ["git", "-C", str(view), "checkout", "--quiet", "--detach", candidate.base_commit],
        cwd=view,
        timeout=timeout,
        step="review-view-checkout",
    )
    # Working tree to the candidate, then the index back to the base. What is
    # left is HEAD and index at the base with the candidate's files on disk:
    # modifications read as modifications, deletions as deletions, and files
    # the task added as untracked — the same full delta the superseded reset
    # dance exposed, assembled where the task cannot reach it.
    await run_git(
        ["git", "-C", str(view), "read-tree", "--reset", "-u", candidate.tree_id],
        cwd=view,
        timeout=timeout,
        step="review-view-overlay",
    )
    await run_git(
        ["git", "-C", str(view), "reset", "--quiet", "--mixed", candidate.base_commit],
        cwd=view,
        timeout=timeout,
        step="review-view-park-index",
    )
    return view


def remove_review_view(path: str | Path) -> None:
    with contextlib.suppress(OSError):
        shutil.rmtree(path)
