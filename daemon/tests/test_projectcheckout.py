"""Read-only checkout inspection and the clone-input validators (ADR-0022)."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from ompire_daemon.projectcheckout import (
    InvalidRemoteNameError,
    InvalidRepoUrlError,
    inspect_checkout,
    inspection_message,
    validate_remote_name,
    validate_repo_url,
)

TIMEOUT = 30


def git(*args: str, cwd: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def make_repo(
    path: Path, *, remote: str | None = "origin", commit: bool = True
) -> Path:
    path.mkdir(parents=True)
    git("init", "--initial-branch=main", ".", cwd=path)
    if commit:
        (path / "README.md").write_text("hello\n")
        git("add", "README.md", cwd=path)
        git("commit", "-m", "initial", cwd=path)
    if remote:
        git("remote", "add", remote, "https://example.com/demo.git", cwd=path)
    return path


def fingerprint(root: Path) -> dict[str, tuple[int, str]]:
    """Every file under `root` with its mtime_ns and content hash."""
    out: dict[str, tuple[int, str]] = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for filename in filenames:
            full = Path(dirpath) / filename
            try:
                data = full.read_bytes()
                stat = full.stat()
            except OSError:
                continue
            out[str(full.relative_to(root))] = (
                stat.st_mtime_ns,
                hashlib.sha256(data).hexdigest(),
            )
    return out


# --- URL and remote-name validators -----------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/owner/repo",
        "https://github.com/owner/repo.git",
        "https://git.example.com:8443/owner/repo.git",
        "ssh://git@github.com/owner/repo.git",
        "git@github.com:owner/repo.git",
    ],
)

def test_accepted_repo_urls(url: str) -> None:
    assert validate_repo_url("upstream_url", url) == url


@pytest.mark.parametrize(
    "url",
    [
        # Remote helpers run a command; this is host code execution, not a URL.
        "ext::sh -c 'touch /tmp/pwned'",
        "ext::git-upload-pack",
        # Option-shaped values are read by git as flags, not repositories.
        "--upload-pack=touch /tmp/pwned",
        "-u",
        # Local and unencrypted transports are deliberately out of scope.
        "/etc/passwd",
        "../../etc",
        "file:///etc",
        "git://example.com/repo.git",
        "http://example.com/repo.git",
        "",
        "   ",
        "https://example.com/repo with space",
    ],
)

def test_refused_repo_urls(url: str) -> None:
    with pytest.raises(InvalidRepoUrlError):
        validate_repo_url("upstream_url", url)


@pytest.mark.parametrize("name", ["origin", "upstream", "my-fork", "r2.d2", "a_b"])

def test_accepted_remote_names(name: str) -> None:
    assert validate_remote_name(name) == name


@pytest.mark.parametrize(
    "name", ["", "-origin", "a b", "a/b", "..", "origin\nfetch", "$(whoami)"]
)

def test_refused_remote_names(name: str) -> None:
    with pytest.raises(InvalidRemoteNameError):
        validate_remote_name(name)


# --- inspection --------------------------------------------------------------


async def test_usable_checkout_is_accepted_with_suggestions(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "demo")

    result = await inspect_checkout(str(repo), fetch_remote="origin", timeout=TIMEOUT)

    assert result.ok
    assert result.reason == ""
    assert [r.name for r in result.remotes] == ["origin"]
    assert result.suggested_upstream == "https://example.com/demo.git"
    assert result.suggested_fork is None


async def test_fork_layout_suggests_upstream_and_fork(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "demo")
    git("remote", "add", "upstream", "https://github.com/org/demo.git", cwd=repo)

    result = await inspect_checkout(
        str(repo), fetch_remote="upstream", timeout=TIMEOUT
    )

    assert result.ok
    assert result.suggested_upstream == "https://github.com/org/demo.git"
    assert result.suggested_fork == "https://example.com/demo.git"


async def test_missing_fetch_remote_names_what_is_there(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "demo")

    result = await inspect_checkout(
        str(repo), fetch_remote="upstream", timeout=TIMEOUT
    )

    assert not result.ok
    assert result.reason == "no_remote"
    message = inspection_message(result, "upstream")
    assert "upstream" in message
    assert "origin" in message


async def test_relative_path_is_refused(tmp_path: Path) -> None:
    result = await inspect_checkout("relative/path", fetch_remote="origin", timeout=TIMEOUT)
    assert result.reason == "not_absolute"


async def test_missing_path_is_refused(tmp_path: Path) -> None:
    result = await inspect_checkout(
        str(tmp_path / "gone"), fetch_remote="origin", timeout=TIMEOUT
    )
    assert result.reason == "missing"


async def test_plain_directory_is_refused(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    result = await inspect_checkout(str(plain), fetch_remote="origin", timeout=TIMEOUT)
    assert result.reason == "not_git"


async def test_subdirectory_of_a_repository_is_refused(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "demo")
    nested = repo / "src"
    nested.mkdir()

    result = await inspect_checkout(str(nested), fetch_remote="origin", timeout=TIMEOUT)

    assert result.reason == "not_toplevel"


async def test_bare_repository_is_refused(tmp_path: Path) -> None:
    bare = tmp_path / "bare.git"
    bare.mkdir()
    git("init", "--bare", "--initial-branch=main", ".", cwd=bare)

    result = await inspect_checkout(str(bare), fetch_remote="origin", timeout=TIMEOUT)

    assert result.reason in ("bare", "not_toplevel")


async def test_repository_without_commits_is_refused(tmp_path: Path) -> None:
    repo = make_repo(tmp_path / "demo", commit=False)

    result = await inspect_checkout(str(repo), fetch_remote="origin", timeout=TIMEOUT)

    assert result.reason == "unborn_head"


@pytest.mark.parametrize("fetch_remote", ["origin", "upstream"])

async def test_inspection_never_writes_to_the_checkout(
    tmp_path: Path, fetch_remote: str
) -> None:
    """The invariant adoption rests on: looking changes nothing.

    Run for a remote that exists (success) and one that does not (refusal),
    because a validator that "helpfully" repairs would do it on the failing
    path.
    """
    repo = make_repo(tmp_path / "demo")
    before = fingerprint(repo)

    await inspect_checkout(str(repo), fetch_remote=fetch_remote, timeout=TIMEOUT)

    assert fingerprint(repo) == before
    remotes = subprocess.run(
        ["git", "remote"], cwd=repo, capture_output=True, text=True, check=True
    )
    assert remotes.stdout.split() == ["origin"]


async def test_suggestions_ignore_insteadof_rewrites(tmp_path: Path) -> None:
    """A checkout using `url.<base>.insteadOf` must still suggest the URL the
    operator configured.

    `git remote -v` reports the *rewritten* URL, which for a local mirror is a
    filesystem path — never a valid pull-request target.
    """
    mirror = tmp_path / "mirror.git"
    mirror.mkdir()
    repo = make_repo(tmp_path / "demo")
    git(
        "config",
        f"url.{mirror}.insteadOf",
        "https://example.com/demo.git",
        cwd=repo,
    )
    rewritten = subprocess.run(
        ["git", "remote", "-v"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    assert str(mirror) in rewritten  # the rewrite really is in effect

    result = await inspect_checkout(str(repo), fetch_remote="origin", timeout=TIMEOUT)

    assert result.ok
    assert result.suggested_upstream == "https://example.com/demo.git"
    assert [r.url for r in result.remotes] == ["https://example.com/demo.git"]
