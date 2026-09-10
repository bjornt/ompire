"""Read-only inspection of a project's base checkout, and the input
validators that guard the clone path.

Architecture: ADR-0022
(docs/adr/0022-create-or-adopt-base-checkouts-without-mutating-them.md)

Adoption is an *inspection*, never a repair. Every git command here is
plumbing that reads: `rev-parse`, `remote -v`. Nothing fetches, nothing
writes config, nothing touches refs, the index, or the working tree. That
restraint is the whole contract — the base checkout is the operator's, and
Ompire's only claim on it is that it is allowed to clone from it.

The URL and remote-name validators live here because they exist for the same
reason: what the operator types becomes `git clone` argv on the host. Git's
transport layer treats `ext::<command>` as "run this command", and an
argument beginning with `-` as an option, so an unvalidated URL is a host
command-execution vector rather than a bad string. Both are rejected before
any subprocess is created.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

# Deliberately narrow. `https://` and ssh are what a GitHub ship target needs
# (ADR-0017); every other transport git accepts either runs a helper command
# (`ext::`), reads a local path, or is unencrypted.
_HTTPS_URL_RE = re.compile(r"^https://[A-Za-z0-9._~-]+(:\d+)?/[^\s]+$")
_SSH_URL_RE = re.compile(r"^ssh://[A-Za-z0-9._~-]+@?[A-Za-z0-9._~-]+(:\d+)?/[^\s]+$")
_SCP_URL_RE = re.compile(r"^[A-Za-z0-9._~-]+@[A-Za-z0-9._~-]+:[^\s:][^\s]*$")

_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# `git config --get-regexp` line: "remote.<name>.url <url>".
_REMOTE_CONFIG_RE = re.compile(r"^remote\.(.+)\.url (\S.*)$")


class InvalidRepoUrlError(ValueError):
    def __init__(self, field: str, value: str) -> None:
        super().__init__(
            f"{field}: {value!r} is not an accepted git URL — use "
            "https://host/owner/repo, ssh://host/owner/repo, or git@host:owner/repo"
        )
        self.field = field
        self.value = value


class InvalidRemoteNameError(ValueError):
    def __init__(self, value: str) -> None:
        super().__init__(
            f"fetch_remote: {value!r} is not a valid git remote name — use "
            "letters, digits, dot, underscore, or hyphen"
        )
        self.value = value


def validate_repo_url(field: str, url: str) -> str:
    """Return `url` stripped, or raise. Refuses anything git would treat as a
    local path, an option, or a helper command."""
    candidate = url.strip()
    if not candidate or candidate.startswith("-") or "::" in candidate:
        raise InvalidRepoUrlError(field, url)
    if (
        _HTTPS_URL_RE.match(candidate)
        or _SSH_URL_RE.match(candidate)
        or _SCP_URL_RE.match(candidate)
    ):
        return candidate
    raise InvalidRepoUrlError(field, url)


def validate_remote_name(name: str) -> str:
    candidate = name.strip()
    if not _REMOTE_NAME_RE.match(candidate):
        raise InvalidRemoteNameError(name)
    return candidate


@dataclass(frozen=True)
class Remote:
    name: str
    url: str


@dataclass(frozen=True)
class CheckoutInspection:
    """What a read-only look at a candidate checkout found.

    `reason` is empty exactly when `ok` is true. `remotes` is populated
    whenever the path is a git work tree, even if the requested fetch remote
    is missing — the operator needs to see what *is* there to fix it.
    """

    ok: bool
    reason: str
    path: str
    remotes: list[Remote]
    suggested_upstream: str | None = None
    suggested_fork: str | None = None

    @property
    def remote_list(self) -> str:
        return ", ".join(remote.name for remote in self.remotes) or "none"


_REASON_TEXT = {
    "not_absolute": "checkout path must be absolute, got {path}",
    "missing": "checkout path does not exist: {path}",
    "not_a_directory": "checkout path is not a directory: {path}",
    "not_git": "not a git repository: {path}",
    "not_toplevel": (
        "{path} is inside a git repository but is not its top level; "
        "register the repository root instead"
    ),
    "bare": "{path} is a bare repository; Ompire clones from a work tree",
    "no_remote": "no remote named {remote!r} in {path} (remotes: {remotes})",
    "unborn_head": (
        "{path} has no commits yet; Ompire cannot clone a task workspace from it"
    ),
    "git_failed": "could not inspect {path}",
}


async def _git(argv: list[str], timeout: int) -> tuple[int, str, str]:
    """Run a read-only git command. Never raises on exit status."""
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=no_prompt_env(),
        )
    except OSError as exc:
        return 1, "", f"cannot exec {argv[0]!r}: {exc}"
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=timeout
        )
    except TimeoutError:
        process.kill()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(process.wait(), timeout=5)
        return 1, "", f"git did not answer within {timeout}s"
    return (
        process.returncode or 0,
        stdout_bytes.decode("utf-8", errors="replace"),
        stderr_bytes.decode("utf-8", errors="replace"),
    )


def no_prompt_env() -> dict[str, str]:
    """The daemon's environment with every interactive git prompt disabled.

    A background clone that stops at a credential or host-key prompt would
    hang until its timeout with nothing to show for it; failing immediately
    with git's own message is the honest outcome. No credential is added
    here — the clone gets exactly the operator's own git configuration.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")
    env.pop("GIT_ASKPASS", None)
    env.pop("SSH_ASKPASS", None)
    return env


def _parse_remotes(stdout: str) -> list[Remote]:
    """Remotes as *configured*, not as git would resolve them.

    Deliberately not `git remote -v`: that output has `url.<base>.insteadOf`
    rewrites already applied, so a checkout using them would suggest whatever
    the rewrite points at — a local mirror path, say — as the project's
    upstream. The configured URL is what the operator wrote and the only
    thing that can be a pull-request target.
    """
    seen: dict[str, str] = {}
    for line in stdout.splitlines():
        match = _REMOTE_CONFIG_RE.match(line.strip())
        if match:
            seen.setdefault(match.group(1), match.group(2).strip())
    return [Remote(name, url) for name, url in seen.items()]


def _suggestions(remotes: list[Remote]) -> tuple[str | None, str | None]:
    """Read the fork workflow out of the remote names, without applying it.

    A checkout with both `upstream` and `origin` is the classic fork layout:
    `upstream` is where PRs land and `origin` is where branches are pushed.
    Otherwise `origin` is the upstream and an explicit `fork` remote, if any,
    is the push target. The operator confirms either way.
    """
    by_name = {remote.name: remote.url for remote in remotes}
    if "upstream" in by_name:
        return by_name["upstream"], by_name.get("origin") or by_name.get("fork")
    return by_name.get("origin"), by_name.get("fork")


async def inspect_checkout(
    checkout_path: str, *, fetch_remote: str, timeout: int
) -> CheckoutInspection:
    """Look at `checkout_path` without writing anything to it.

    Usable means: an absolute path that is the top level of a non-bare git
    work tree, holding a remote named `fetch_remote` and at least one commit.
    """
    path = checkout_path.strip()

    def fail(reason: str, remotes: list[Remote] | None = None) -> CheckoutInspection:
        return CheckoutInspection(False, reason, path, remotes or [])

    if not path or not Path(path).is_absolute():
        return fail("not_absolute")
    target = Path(path)
    if not target.exists():
        return fail("missing")
    if not target.is_dir():
        return fail("not_a_directory")

    # Bareness first: `--show-toplevel` *fails* in a bare repository, so
    # asking for both at once would report "not a git repository" for one that
    # very much is.
    code, stdout, _ = await _git(
        ["git", "-C", path, "rev-parse", "--is-bare-repository"], timeout
    )
    if code != 0:
        return fail("not_git")
    if stdout.strip() == "true":
        return fail("bare")

    code, stdout, _ = await _git(
        ["git", "-C", path, "rev-parse", "--show-toplevel"], timeout
    )
    if code != 0 or not stdout.strip():
        return fail("git_failed")
    if os.path.realpath(stdout.strip()) != os.path.realpath(path):
        return fail("not_toplevel")

    # `--get-regexp` exits 1 when nothing matches, which is "no remotes", not
    # an error — an unusable result is caught by the fetch-remote check below.
    _, stdout, _ = await _git(
        ["git", "-C", path, "config", "--get-regexp", r"^remote\..*\.url$"], timeout
    )
    remotes = _parse_remotes(stdout)
    if fetch_remote not in {remote.name for remote in remotes}:
        return fail("no_remote", remotes)

    code, _, _ = await _git(
        ["git", "-C", path, "rev-parse", "--verify", "--quiet", "HEAD^{commit}"],
        timeout,
    )
    if code != 0:
        return fail("unborn_head", remotes)

    upstream, fork = _suggestions(remotes)
    return CheckoutInspection(
        True, "", path, remotes, suggested_upstream=upstream, suggested_fork=fork
    )


def inspection_message(inspection: CheckoutInspection, fetch_remote: str) -> str:
    """Human-readable refusal text, with the remote name interpolated."""
    if inspection.ok:
        return ""
    return _REASON_TEXT[inspection.reason].format(
        path=inspection.path, remote=fetch_remote, remotes=inspection.remote_list
    )
