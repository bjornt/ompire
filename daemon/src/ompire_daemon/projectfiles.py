"""Project file search and spawn-prompt `@file` mentions.

Two halves of one feature (add-spawn-file-mentions):

*Search* lists a project's repository-relative paths so the Spawn view can
offer them while the operator writes a prompt. The listing comes from
`git ls-files --cached --others --exclude-standard`, which is what makes it
follow the repository's ignore rules for free. Only names are returned —
never contents, never absolute paths.

*Mentions* are the `@path` tokens in a stored prompt. Omp parses them out of
the RPC `prompt` request's `message` itself, resolving each against the
agent's working directory, and **silently drops one that does not resolve**
(verified against omp 17.4.0). That fail-open behavior is why the daemon
validates mentions at submit and re-resolves them against the clone before
delivery: an unresolvable mention would otherwise cost the operator context
they explicitly asked for, with nothing anywhere saying so.

The listing deliberately includes files that are present but not committed,
which a task's clone will not contain — `git clone` of a local checkout
copies neither untracked files nor branches other than the one checked out.
`validate_mentions` is what turns that into an immediate, named refusal at
submit rather than a dangling reference discovered after the workspace is
built.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_LIMIT = 50
MAX_LIMIT = 200

# `@` at the start of a word only: `someone@example.com` is prose, not a
# mention. Omp's own boundary rule agrees (see findings-omp-file-mentions.md).
_MENTION_RE = re.compile(r"(?<![^\s])@(\S+)")

# Trimmed from a mention's tail only when the untrimmed token does not resolve,
# so a file whose name really ends in one of these still wins.
_TRAILING_PUNCTUATION = ".,;:!?)]}\"'"


class ProjectFilesError(Exception):
    """Base for file-search failures that should reach the operator verbatim."""


class CheckoutMissingError(ProjectFilesError):
    def __init__(self, checkout_path: str) -> None:
        super().__init__(f"checkout path does not exist: {checkout_path}")
        self.checkout_path = checkout_path


class CheckoutNotGitError(ProjectFilesError):
    def __init__(self, checkout_path: str) -> None:
        super().__init__(f"checkout path is not a git repository: {checkout_path}")
        self.checkout_path = checkout_path


class FileSearchFailedError(ProjectFilesError):
    """git ran but did not succeed, or did not answer within the bound."""


@dataclass(frozen=True)
class FileSearchResult:
    paths: list[str]
    truncated: bool


async def _git_output(argv: list[str], cwd: str, timeout: int) -> tuple[int, str, str]:
    """Run git and return (returncode, stdout, stderr). Never raises on exit status."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        # A missing cwd surfaces here, before git ever runs.
        raise CheckoutMissingError(cwd) from exc
    except NotADirectoryError as exc:
        raise CheckoutMissingError(cwd) from exc
    except OSError as exc:
        raise FileSearchFailedError(f"cannot exec {argv[0]!r}: {exc}") from exc
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=timeout
        )
    except TimeoutError as exc:
        process.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.wait(), timeout=5)
        raise FileSearchFailedError(f"git did not answer within {timeout}s") from exc
    return (
        process.returncode or 0,
        stdout_bytes.decode("utf-8", errors="replace"),
        stderr_bytes.decode("utf-8", errors="replace"),
    )


def _split_nul(text: str) -> list[str]:
    return [entry for entry in text.split("\0") if entry]


def rank_paths(paths: list[str], query: str, limit: int) -> FileSearchResult:
    """Filter by `query`, rank final-segment matches first, and apply `limit`.

    Deterministic for a given query and path set: both groups are sorted
    lexicographically, so equal queries always produce equal results.
    """
    effective_limit = max(1, min(limit, MAX_LIMIT))
    needle = query.strip().casefold()
    if needle:
        basename_hits: list[str] = []
        path_hits: list[str] = []
        for path in paths:
            if needle in path.rsplit("/", 1)[-1].casefold():
                basename_hits.append(path)
            elif needle in path.casefold():
                path_hits.append(path)
        matches = sorted(basename_hits) + sorted(path_hits)
    else:
        matches = sorted(paths)
    return FileSearchResult(
        paths=matches[:effective_limit], truncated=len(matches) > effective_limit
    )


async def search_project_files(
    checkout_path: str, *, query: str = "", limit: int = DEFAULT_LIMIT, timeout: int
) -> FileSearchResult:
    """List repository-relative paths under `checkout_path`, filtered by `query`.

    Tracked and not-yet-committed files, never ignored ones, never `.git`.
    """
    root = Path(checkout_path).expanduser()
    if not root.is_dir():
        raise CheckoutMissingError(checkout_path)
    code, stdout, stderr = await _git_output(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        str(root),
        timeout,
    )
    if code != 0:
        if "not a git repository" in stderr.lower():
            raise CheckoutNotGitError(checkout_path)
        raise FileSearchFailedError(f"git ls-files failed: {stderr.strip()}")
    return rank_paths(_split_nul(stdout), query, limit)


# --- mentions ---------------------------------------------------------------


@dataclass(frozen=True)
class MentionRejection:
    """One mention the daemon refuses, with the reason an operator can act on."""

    token: str  # as written in the prompt, without the leading '@'
    reason: str  # absolute | traversal | outside_checkout | missing | not_a_file | not_on_base_branch

    def message(self) -> str:
        return f"@{self.token}: {_REASON_TEXT[self.reason]}"


_REASON_TEXT = {
    "absolute": "absolute paths cannot be attached; use a path relative to the repository root",
    "traversal": "'..' is not allowed in a file mention",
    "outside_checkout": "resolves outside the project's checkout",
    "missing": "no such file in the project's checkout",
    "not_a_file": "not a regular file",
    "not_on_base_branch": (
        "not on the template's base branch, so the task's clone will not contain it "
        "(commit it to the base branch, or remove the mention)"
    ),
}


def mention_tokens(prompt: str) -> list[str]:
    """Every `@token` in `prompt`, in order, without the leading `@`."""
    return _MENTION_RE.findall(prompt)


def _candidates(token: str) -> list[str]:
    """The token itself, then its punctuation-trimmed form when they differ."""
    trimmed = token.rstrip(_TRAILING_PUNCTUATION)
    return [token] if trimmed == token else [token, trimmed]


def _confined_path(root: Path, candidate: str) -> Path | None:
    """Resolve `candidate` under `root`, or None when it escapes after resolution."""
    resolved = (root / candidate).resolve()
    if resolved != root and root not in resolved.parents:
        return None
    return resolved


def _check_candidate(root: Path, candidate: str) -> tuple[Path | None, str]:
    """Return (path, reason). `path` is set only when the candidate is usable."""
    if candidate.startswith("/"):
        return None, "absolute"
    if ".." in Path(candidate).parts:
        return None, "traversal"
    resolved = _confined_path(root, candidate)
    if resolved is None:
        return None, "outside_checkout"
    if not resolved.exists():
        return None, "missing"
    if not resolved.is_file():
        return None, "not_a_file"
    return resolved, ""


def resolve_mention(root: Path, token: str) -> tuple[str | None, str]:
    """Resolve one mention token under `root`.

    Returns (repository-relative path, "") when usable, else (None, reason).
    The untrimmed token is tried first so a filename ending in punctuation
    still wins over the prose reading.
    """
    first_reason = ""
    for candidate in _candidates(token):
        resolved, reason = _check_candidate(root, candidate)
        if resolved is not None:
            return candidate, ""
        first_reason = first_reason or reason
    return None, first_reason


async def _paths_on_branch(
    checkout_path: str, base_branch: str, candidates: list[str], timeout: int
) -> set[str] | None:
    """Which of `candidates` exist in `base_branch`'s tree, or None if unknown.

    None means the branch ref does not resolve — a broken template or checkout
    that the spawn pipeline's own `branch` step will report accurately. The
    mention check is skipped rather than blamed for it.
    """
    if not candidates:
        return set()
    ref = f"refs/heads/{base_branch}"
    code, _, _ = await _git_output(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        checkout_path,
        timeout,
    )
    if code != 0:
        return None
    code, stdout, stderr = await _git_output(
        # `--literal-pathspecs`: a filename containing glob characters is a
        # path here, not a pattern, or it would be reported as absent.
        [
            "git", "--literal-pathspecs", "ls-tree", "-r", "--name-only", "-z",
            ref, "--", *candidates,
        ],
        checkout_path,
        timeout,
    )
    if code != 0:
        raise FileSearchFailedError(f"git ls-tree failed: {stderr.strip()}")
    return set(_split_nul(stdout))


async def validate_mentions(
    prompt: str, *, checkout_path: str, base_branch: str, timeout: int
) -> list[MentionRejection]:
    """Every reason this prompt's mentions cannot become file context.

    An empty list means every mention will resolve inside the task's clone.
    """
    tokens = mention_tokens(prompt)
    if not tokens:
        return []
    root = Path(checkout_path).expanduser()
    if not root.is_dir():
        raise CheckoutMissingError(checkout_path)
    root = root.resolve()

    rejections: list[MentionRejection] = []
    resolved: dict[str, str] = {}  # token -> repo-relative path
    for token in tokens:
        path, reason = resolve_mention(root, token)
        if path is None:
            rejections.append(MentionRejection(token, reason))
        else:
            resolved[token] = path
    if not resolved:
        return rejections

    on_branch = await _paths_on_branch(
        checkout_path, base_branch, sorted(set(resolved.values())), timeout
    )
    if on_branch is not None:
        for token, path in resolved.items():
            if path not in on_branch:
                rejections.append(MentionRejection(token, "not_on_base_branch"))
    return rejections


def unresolved_mentions(prompt: str, root_path: str) -> list[str]:
    """Mention tokens that will not resolve for an agent working in `root_path`.

    The delivery-time counterpart of `validate_mentions`: the clone is the
    truth once it exists, and Omp drops what it cannot resolve without a word.
    """
    tokens = mention_tokens(prompt)
    if not tokens:
        return []
    root = Path(root_path).expanduser()
    if not root.is_dir():
        return tokens
    root = root.resolve()
    return [token for token in tokens if resolve_mention(root, token)[0] is None]
