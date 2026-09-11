"""One literal Git-exclusion mechanism for daemon-owned and protected paths.

`.git/info/exclude` holds *gitignore patterns*, not paths. A captured filename
may legitimately contain `*`, `?`, `[`, `]`, or a leading `!` or `#`, and any
of those would silently turn one file's entry into a pattern matching other
files — or into a negation. Every one of them is escaped, and the leading `/`
anchors the pattern at the repository root so a handoff at `epics/PLAN.md`
cannot also hide an unrelated `docs/epics/PLAN.md`.

Exclusion is workspace mechanics and convenience only. It keeps daemon-owned
paths and supplied protected destinations out of ordinary staging and status;
it is never proof that content is safe to publish. The actual guarantee lives
at the trusted candidate and delivery boundary, checked against the proposed
Git result and retained history (ADR-0035). Which destinations are protected
is decided by the task's accepted attachments, not inferred here from
filenames (ADR-0039).
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

WORKSHOP_LOCK_FILENAME = ".workshop.lock"

# Daemon-owned paths, excluded from git per clone: the outcome directory
# (workflow-engine design D-3) and the workshop lock (design D-1). They must
# never appear in status, diffs, reviews, or a ship commit — the ship flow
# stages the agent's whole delta, lock file included (dogfooding).
GIT_EXCLUDE_OMPIRE_ENTRY = ".ompire/"
_GIT_EXCLUDE_ENTRIES = (GIT_EXCLUDE_OMPIRE_ENTRY, WORKSHOP_LOCK_FILENAME)


class ExcludeUpdateError(Exception):
    """The clone's exclude file could not be updated.

    `step` names the caller's operation for the failure message.
    """

    def __init__(self, step: str, detail: str) -> None:
        super().__init__(f"{step}: {detail}")
        self.step = step
        self.detail = detail


def exclude_pattern_for(path: str) -> str:
    """One repository-relative path as a literal, root-anchored exclude pattern.

    A directory that matches an exclude is not descended into, so this one
    entry keeps covering the path if a file there is later replaced by a
    directory.
    """
    escaped = "".join(
        "\\" + character if character in "\\*?[]" else character for character in path
    )
    if escaped[:1] in {"!", "#"}:
        escaped = "\\" + escaped
    return "/" + escaped


def ensure_git_excludes(
    clone_path: str, step: str = "clone", protected: Sequence[str] = ()
) -> None:
    """Append every daemon-owned entry to the clone's `.git/info/exclude`
    (idempotent) so outcome files and the workshop lock are invisible to
    git status, staging, reviews, and PRs (workflow-engine design D-3/D-8;
    workshop design D-1).

    `protected` adds supplied protected destinations (ADR-0035). This keeps
    them out of ordinary staging, which is a convenience — it is *not* the
    publication guarantee. An agent can edit this file, force-add a path, or
    commit one directly, so the actual refusal lives at the trusted candidate
    and delivery boundary, checked against the proposed Git result.
    """
    exclude_path = Path(clone_path) / ".git" / "info" / "exclude"
    try:
        existing = exclude_path.read_text(encoding="utf-8")
    except OSError:
        existing = ""
    wanted = [
        *_GIT_EXCLUDE_ENTRIES,
        *(exclude_pattern_for(path) for path in protected),
    ]
    present = set(existing.splitlines())
    missing = [entry for entry in wanted if entry not in present]
    if not missing:
        return
    separator = "" if existing.endswith("\n") or not existing else "\n"
    try:
        with exclude_path.open("a", encoding="utf-8") as handle:
            handle.write(separator + "\n".join(missing) + "\n")
    except OSError as exc:
        raise ExcludeUpdateError(step, f"cannot write {exclude_path}: {exc}") from exc
