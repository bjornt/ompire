"""Unit tests for project file search and spawn-prompt `@file` mentions."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ompire_daemon.projectfiles import (
    MAX_LIMIT,
    CheckoutMissingError,
    CheckoutNotGitError,
    FileSearchFailedError,
    mention_tokens,
    rank_paths,
    resolve_mention,
    search_project_files,
    unresolved_mentions,
    validate_mentions,
)

TIMEOUT = 30


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A checkout with tracked, uncommitted, and ignored files on `main`."""
    checkout = tmp_path / "checkout"
    (checkout / "src" / "lib").mkdir(parents=True)
    (checkout / "docs").mkdir()
    _git("init", "--initial-branch=main", ".", cwd=checkout)
    (checkout / ".gitignore").write_text("ignored.txt\nbuild/\n")
    (checkout / "README.md").write_text("readme\n")
    (checkout / "src" / "app.ts").write_text("app\n")
    (checkout / "src" / "lib" / "token.ts").write_text("token\n")
    (checkout / "docs" / "token-guide.md").write_text("guide\n")
    _git("add", "-A", cwd=checkout)
    _git("commit", "-m", "initial", cwd=checkout)

    # Present but never committed, so a clone of this checkout will not have it.
    (checkout / "scratch.md").write_text("scratch\n")
    # Ignored by .gitignore, and an ignored directory.
    (checkout / "ignored.txt").write_text("secret\n")
    (checkout / "build").mkdir()
    (checkout / "build" / "out.js").write_text("built\n")
    return checkout


# --- search -----------------------------------------------------------------


async def test_lists_tracked_and_uncommitted_but_never_ignored(repo: Path) -> None:
    result = await search_project_files(str(repo), limit=MAX_LIMIT, timeout=TIMEOUT)

    assert "README.md" in result.paths
    assert "src/lib/token.ts" in result.paths
    # Uncommitted-but-not-ignored is deliberately offered (SPEC: search scope).
    assert "scratch.md" in result.paths
    assert "ignored.txt" not in result.paths
    assert "build/out.js" not in result.paths


async def test_never_returns_git_internals_or_absolute_paths(repo: Path) -> None:
    result = await search_project_files(str(repo), limit=MAX_LIMIT, timeout=TIMEOUT)

    assert all(not path.startswith("/") for path in result.paths)
    assert all(not path.startswith(".git/") for path in result.paths)
    assert all(str(repo) not in path for path in result.paths)


async def test_query_ranks_basename_matches_before_path_matches(repo: Path) -> None:
    result = await search_project_files(str(repo), query="token", limit=MAX_LIMIT, timeout=TIMEOUT)

    # `token-guide.md` and `token.ts` match on their final segment; nothing
    # else matches only deeper in the path here, so both groups are checked
    # through their relative order.
    assert result.paths == ["docs/token-guide.md", "src/lib/token.ts"]


async def test_query_matching_only_a_directory_segment_ranks_last(repo: Path) -> None:
    result = await search_project_files(str(repo), query="lib", limit=MAX_LIMIT, timeout=TIMEOUT)

    assert result.paths == ["src/lib/token.ts"]


def test_ranking_is_deterministic_and_groups_are_sorted() -> None:
    paths = ["z/token.ts", "a/token.ts", "token/other.ts", "b/token.ts"]

    first = rank_paths(paths, "token", 10)
    second = rank_paths(list(reversed(paths)), "token", 10)

    assert first.paths == second.paths
    assert first.paths == ["a/token.ts", "b/token.ts", "z/token.ts", "token/other.ts"]


async def test_empty_query_returns_sorted_head_of_the_listing(repo: Path) -> None:
    result = await search_project_files(str(repo), limit=MAX_LIMIT, timeout=TIMEOUT)

    assert result.paths == sorted(result.paths)


def test_limit_truncates_and_reports_truncation() -> None:
    paths = [f"file{index:02d}.txt" for index in range(10)]

    limited = rank_paths(paths, "", 3)
    assert limited.paths == ["file00.txt", "file01.txt", "file02.txt"]
    assert limited.truncated is True

    full = rank_paths(paths, "", 10)
    assert full.truncated is False


def test_limit_cannot_exceed_the_hard_maximum() -> None:
    paths = [f"file{index:04d}.txt" for index in range(MAX_LIMIT + 25)]

    result = rank_paths(paths, "", MAX_LIMIT + 25)

    assert len(result.paths) == MAX_LIMIT
    assert result.truncated is True


async def test_missing_checkout_is_distinct_from_no_matches(tmp_path: Path) -> None:
    with pytest.raises(CheckoutMissingError):
        await search_project_files(str(tmp_path / "gone"), limit=10, timeout=TIMEOUT)


async def test_non_git_directory_is_distinct_from_no_matches(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.txt").write_text("a\n")

    with pytest.raises(CheckoutNotGitError):
        await search_project_files(str(plain), limit=10, timeout=TIMEOUT)


async def test_search_is_bounded_in_time(repo: Path) -> None:
    with pytest.raises(FileSearchFailedError, match="did not answer within"):
        await search_project_files(str(repo), limit=10, timeout=0)


# --- the mention rule -------------------------------------------------------


def test_mention_is_at_a_word_boundary_only() -> None:
    tokens = mention_tokens("Mail someone@example.com about @src/app.ts and\n@README.md")

    assert tokens == ["src/app.ts", "README.md"]


def test_decorator_style_text_mid_word_is_not_a_mention() -> None:
    assert mention_tokens("use the@thing and x@y") == []


def test_mention_at_string_start_is_found() -> None:
    assert mention_tokens("@a.txt is first") == ["a.txt"]


def test_trailing_prose_punctuation_is_trimmed(repo: Path) -> None:
    root = repo.resolve()

    assert resolve_mention(root, "README.md.") == ("README.md", "")
    assert resolve_mention(root, "src/app.ts),") == ("src/app.ts", "")


def test_a_filename_ending_in_punctuation_beats_the_trimmed_reading(repo: Path) -> None:
    (repo / "weird.).txt").write_text("x\n")

    assert resolve_mention(repo.resolve(), "weird.).txt")[0] == "weird.).txt"


def test_leading_punctuation_is_never_trimmed(repo: Path) -> None:
    # Only the tail is prose; `@(README.md)` is not a way to write a mention.
    assert resolve_mention(repo.resolve(), "(README.md)") == (None, "missing")


# --- validation at submit ---------------------------------------------------


async def test_valid_mention_on_the_base_branch_is_accepted(repo: Path) -> None:
    rejections = await validate_mentions(
        "look at @src/lib/token.ts please",
        checkout_path=str(repo),
        base_branch="main",
        timeout=TIMEOUT,
    )

    assert rejections == []


async def test_prompt_without_mentions_needs_no_git(tmp_path: Path) -> None:
    assert (
        await validate_mentions(
            "no mentions here",
            checkout_path=str(tmp_path / "does-not-exist"),
            base_branch="main",
            timeout=TIMEOUT,
        )
        == []
    )


@pytest.mark.parametrize(
    ("mention", "reason"),
    [
        ("/etc/passwd", "absolute"),
        ("../outside.txt", "traversal"),
        ("src/../../outside.txt", "traversal"),
        ("no-such-file.txt", "missing"),
        ("src", "not_a_file"),
        ("scratch.md", "not_on_base_branch"),
    ],
)
async def test_each_rejection_reason(repo: Path, mention: str, reason: str) -> None:
    rejections = await validate_mentions(
        f"see @{mention} now",
        checkout_path=str(repo),
        base_branch="main",
        timeout=TIMEOUT,
    )

    assert [rejection.reason for rejection in rejections] == [reason]
    assert mention in rejections[0].message()


async def test_uncommitted_file_names_the_base_branch_in_its_message(repo: Path) -> None:
    rejections = await validate_mentions(
        "see @scratch.md",
        checkout_path=str(repo),
        base_branch="main",
        timeout=TIMEOUT,
    )

    assert "base branch" in rejections[0].message()
    assert "clone" in rejections[0].message()


async def test_a_filename_with_glob_characters_is_matched_literally(repo: Path) -> None:
    tricky = "a[1].txt"
    (repo / tricky).write_text("x\n")
    _git("add", tricky, cwd=repo)
    _git("commit", "-m", "glob-ish name", cwd=repo)

    rejections = await validate_mentions(
        f"see @{tricky}",
        checkout_path=str(repo),
        base_branch="main",
        timeout=TIMEOUT,
    )

    assert rejections == []


async def test_symlink_escaping_the_checkout_is_refused(repo: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("secret\n")
    (repo / "escape.txt").symlink_to(outside)

    rejections = await validate_mentions(
        "read @escape.txt",
        checkout_path=str(repo),
        base_branch="main",
        timeout=TIMEOUT,
    )

    assert [rejection.reason for rejection in rejections] == ["outside_checkout"]


async def test_email_in_a_prompt_never_causes_a_refusal(repo: Path) -> None:
    rejections = await validate_mentions(
        "ask someone@example.com to look",
        checkout_path=str(repo),
        base_branch="main",
        timeout=TIMEOUT,
    )

    assert rejections == []


async def test_unresolvable_base_branch_skips_the_branch_check(repo: Path) -> None:
    # A broken template is the `branch` step's error to report, not the
    # mention validator's — the file itself is present and readable.
    rejections = await validate_mentions(
        "see @scratch.md",
        checkout_path=str(repo),
        base_branch="no-such-branch",
        timeout=TIMEOUT,
    )

    assert rejections == []


async def test_every_bad_mention_is_reported_not_just_the_first(repo: Path) -> None:
    rejections = await validate_mentions(
        "see @no-such-file.txt and @/etc/passwd and @src/lib/token.ts",
        checkout_path=str(repo),
        base_branch="main",
        timeout=TIMEOUT,
    )

    assert sorted(rejection.reason for rejection in rejections) == ["absolute", "missing"]


# --- resolution at delivery -------------------------------------------------


def test_unresolved_mentions_reports_what_the_clone_lacks(repo: Path) -> None:
    assert unresolved_mentions("@README.md and @gone.txt", str(repo)) == ["gone.txt"]


def test_unresolved_mentions_is_empty_when_everything_resolves(repo: Path) -> None:
    assert unresolved_mentions("@README.md @src/app.ts", str(repo)) == []


def test_unresolved_mentions_treats_a_missing_root_as_all_dangling(tmp_path: Path) -> None:
    assert unresolved_mentions("@a.txt @b.txt", str(tmp_path / "gone")) == ["a.txt", "b.txt"]
