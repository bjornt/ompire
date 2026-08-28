"""Trusted GitHub CLI boundary: safe process execution and eligibility probes.

Every daemon-owned ``gh`` invocation crosses this module.  The runner is
non-interactive, bounded by its caller, and redacts credentials before output
can be parsed, logged, retained in memory, or published to a client.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ompire_daemon.config import Config
    from ompire_daemon.events import EventHub


_SUPPORTED_HOST = "github.com"
_DEFAULT_TIMEOUT = 10
_REDACTED = "[redacted]"

# GitHub's documented token prefixes.  The delimiter checks avoid changing an
# unrelated longer identifier which merely contains a token-shaped substring.
_GITHUB_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:github_pat_[A-Za-z0-9_]+|gh[opusr]_[A-Za-z0-9]+)(?![A-Za-z0-9_])"
)
_AUTHORIZATION_RE = re.compile(r"(?i)(\bauthorization\s*:\s*)[^\r\n]*")
_URL_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]+@")
_GITHUB_SSH_RE = re.compile(
    r"^git@(?P<host>github\.com):(?P<path>[^?#\s]+?)/?$", re.IGNORECASE
)
_GITHUB_HTTPS_RE = re.compile(
    r"^https://(?P<host>github\.com)/(?P<path>[^?#\s]+?)/?$", re.IGNORECASE
)
_GITHUB_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?$")
_GITHUB_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?$")


@dataclass(frozen=True)
class GitHubTarget:
    """A supported canonical pull-request target derived from project state."""

    host: str
    owner: str
    repository: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repository}"

    @property
    def canonical(self) -> str:
        return f"{self.host}/{self.slug}"


@dataclass(frozen=True)
class GitHubIdentityBinding:
    """The safe identity tuple that makes a target result current."""

    host: str
    login: str
    credential_source: str


@dataclass(frozen=True)
class GitHubIdentityStatus:
    """Current global GitHub CLI observation.  It never contains a credential."""

    state: str  # unknown | missing | unauthenticated | ready | error
    host: str
    login: str | None
    credential_source: str | None
    executable_path: str | None
    version: str | None
    detail: str | None
    checked_at: str | None

    def binding(self) -> GitHubIdentityBinding | None:
        if (
            self.state != "ready"
            or self.login is None
            or self.credential_source is None
        ):
            return None
        return GitHubIdentityBinding(
            host=self.host,
            login=self.login,
            credential_source=self.credential_source,
        )


@dataclass(frozen=True)
class GitHubTargetStatus:
    """Current target eligibility, bound to the identity which observed it."""

    state: str  # unchecked | allowed | denied | error
    target: GitHubTarget | None
    identity: GitHubIdentityBinding | None
    detail: str | None
    checked_at: str | None


@dataclass(frozen=True)
class GitHubStatus:
    """The complete in-memory status published by REST and WebSocket."""

    identity: GitHubIdentityStatus
    targets: dict[str, GitHubTargetStatus]


@dataclass(frozen=True)
class GitHubCommandResult:
    """Sanitized result from one configured GitHub CLI invocation."""

    stdout: str
    stderr: str
    returncode: int
    launched: bool = True


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def parse_github_target(url: str) -> GitHubTarget:
    """Parse the GitHub HTTPS/SSH target shapes already supported by shipping.

    The parser intentionally accepts no new forge or URL matrix.  Its error is
    generic so an invalid registered URL cannot become an accidental status
    payload (for example, through credential-bearing URL userinfo).
    """

    match = _GITHUB_SSH_RE.match(url) or _GITHUB_HTTPS_RE.match(url)
    if match is None:
        raise ValueError("unsupported GitHub upstream target")

    host = match.group("host").lower()
    path = match.group("path").strip("/")
    path = path.removesuffix(".git")
    pieces = path.split("/")
    if len(pieces) != 2 or not all(
        _GITHUB_NAME_RE.fullmatch(piece) for piece in pieces
    ):
        raise ValueError("unsupported GitHub upstream target")
    return GitHubTarget(
        host=host, owner=pieces[0].lower(), repository=pieces[1].lower()
    )


def parse_github_slug(url: str) -> str:
    """Return canonical ``owner/repository`` from a supported upstream URL."""

    return parse_github_target(url).slug


def parse_github_owner(url: str) -> str:
    """Return the canonical owner from a supported upstream URL."""

    return parse_github_target(url).owner


def redact_github_text(text: str, token_values: Iterable[str] = ()) -> str:
    """Remove credential material from one string before it crosses this boundary."""

    # Header values and URL userinfo can contain formats no token-prefix rule
    # recognizes, so remove them before applying known-token substitutions.
    sanitized = _AUTHORIZATION_RE.sub(r"\1" + _REDACTED, text)
    sanitized = _URL_USERINFO_RE.sub(r"\1" + _REDACTED + "@", sanitized)
    for token in sorted(
        (value for value in token_values if value), key=len, reverse=True
    ):
        sanitized = sanitized.replace(token, _REDACTED)
    return _GITHUB_TOKEN_RE.sub(_REDACTED, sanitized)


def _active_token_values() -> tuple[str, ...]:
    return tuple(
        value
        for value in (os.environ.get("GH_TOKEN"), os.environ.get("GITHUB_TOKEN"))
        if value
    )


class GitHubCli:
    """One non-interactive, redacting process boundary for configured ``gh``."""

    def __init__(self, config: Config) -> None:
        self._command = tuple(config.gh_command)

    @staticmethod
    def credential_source() -> str:
        """The source real ``gh`` will use, without reading or returning its value."""

        if os.environ.get("GH_TOKEN"):
            return "GH_TOKEN"
        if os.environ.get("GITHUB_TOKEN"):
            return "GITHUB_TOKEN"
        return "GitHub CLI configuration"

    def executable_path(self) -> str | None:
        """Resolve the configured executable without running a shell."""

        executable = self._command[0]
        found = shutil.which(executable)
        if found is None:
            return None
        try:
            path = str(Path(found).resolve())
        except OSError:
            # The executable remains usable even if an odd filesystem refuses
            # canonicalization; retain its lookup result as the safe path label.
            path = found
        return redact_github_text(path, _active_token_values())

    async def run(
        self,
        args: list[str],
        cwd: str,
        timeout: float,
    ) -> GitHubCommandResult:
        """Run one allowed ``gh`` operation and return only sanitized streams."""

        token_values = _active_token_values()
        argv = [*self._command, *args]
        env = os.environ.copy()
        # gh otherwise may try to interactively recover a missing login.  Do
        # not unset either token: inherited precedence is the behavior we are
        # checking and the command will actually use it as well.
        env["GH_PROMPT_DISABLED"] = "1"
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            return GitHubCommandResult(
                stdout="",
                stderr=redact_github_text(
                    f"could not start GitHub CLI: {exc}", token_values
                ),
                returncode=127,
                launched=False,
            )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(process.wait(), timeout=5)
            raise
        except TimeoutError:
            process.kill()
            await process.wait()
            return GitHubCommandResult(
                stdout="",
                stderr=f"GitHub CLI timed out after {timeout}s",
                returncode=124,
            )

        return GitHubCommandResult(
            stdout=redact_github_text(
                stdout_bytes.decode("utf-8", errors="replace"), token_values
            ),
            stderr=redact_github_text(
                stderr_bytes.decode("utf-8", errors="replace"), token_values
            ),
            returncode=process.returncode or 0,
        )


class GitHubProbe:
    """Shared ambient-identity and target-eligibility observation.

    Status is intentionally process-local.  A restart starts at ``unknown``
    and probes again; it never turns an old successful observation into policy.
    """

    def __init__(self, config: Config, hub: EventHub) -> None:
        self._config = config
        self._hub = hub
        self._cli = GitHubCli(config)
        self._identity = GitHubIdentityStatus(
            state="unknown",
            host=_SUPPORTED_HOST,
            login=None,
            credential_source=None,
            executable_path=None,
            version=None,
            detail="not probed yet",
            checked_at=None,
        )
        self._targets: dict[str, GitHubTargetStatus] = {}
        self._lock = asyncio.Lock()

    def current(self) -> GitHubStatus:
        """Return current in-memory observation without invoking the CLI."""

        return GitHubStatus(identity=self._identity, targets=dict(self._targets))

    async def run(
        self, args: list[str], cwd: str, timeout: float
    ) -> GitHubCommandResult:
        """Expose the mandatory sanitized CLI boundary to ship and PR polling."""

        return await self._cli.run(args, cwd, timeout)

    async def probe(self) -> GitHubStatus:
        """Refresh global identity and publish a complete safe status projection."""

        return await self._refresh(None)

    async def probe_target(
        self, upstream_url: str
    ) -> tuple[GitHubStatus, GitHubTargetStatus]:
        """Refresh identity plus one trusted-upstream target eligibility result."""

        status = await self._refresh(upstream_url)
        try:
            target = parse_github_target(upstream_url)
        except ValueError:
            return status, status.targets["unsupported"]
        return status, status.targets[target.canonical]

    async def _refresh(self, upstream_url: str | None) -> GitHubStatus:
        async with self._lock:
            identity = await self._probe_identity()
            if identity.binding() != self._identity.binding():
                # A later repository result is only meaningful for the exact
                # ambient account/source which produced it.  Never retain an
                # allowed target through an account change or failed recheck.
                self._targets.clear()
            self._identity = identity

            if upstream_url is not None:
                target_status = await self._probe_upstream(upstream_url, identity)
                key = (
                    target_status.target.canonical
                    if target_status.target is not None
                    else "unsupported"
                )
                self._targets[key] = target_status

            status = self.current()

        # Keep the status event full-state rather than patch-shaped.  That
        # mirrors snapshots, makes identity invalidation atomic for clients,
        # and leaves the daemon as the authority on what remains current.
        self._hub.publish("gh_status", {"gh": asdict(status)})
        return status

    async def _probe_identity(self) -> GitHubIdentityStatus:
        checked_at = _now_iso()
        source = self._cli.credential_source()
        executable_path = self._cli.executable_path()
        if executable_path is None:
            return GitHubIdentityStatus(
                state="missing",
                host=_SUPPORTED_HOST,
                login=None,
                credential_source=source,
                executable_path=None,
                version=None,
                detail="configured GitHub CLI executable is not available",
                checked_at=checked_at,
            )

        version_result = await self.run(
            ["--version"], str(self._config.data_dir), _DEFAULT_TIMEOUT
        )
        if not version_result.launched:
            return GitHubIdentityStatus(
                state="missing",
                host=_SUPPORTED_HOST,
                login=None,
                credential_source=source,
                executable_path=executable_path,
                version=None,
                detail="configured GitHub CLI executable is not available",
                checked_at=checked_at,
            )
        if version_result.returncode != 0:
            return GitHubIdentityStatus(
                state="error",
                host=_SUPPORTED_HOST,
                login=None,
                credential_source=source,
                executable_path=executable_path,
                version=None,
                detail=_command_failure_detail(
                    "GitHub CLI version check", version_result
                ),
                checked_at=checked_at,
            )
        version = _first_line(version_result.stdout)
        if version is None:
            return GitHubIdentityStatus(
                state="error",
                host=_SUPPORTED_HOST,
                login=None,
                credential_source=source,
                executable_path=executable_path,
                version=None,
                detail="GitHub CLI version check returned no version",
                checked_at=checked_at,
            )

        user_result = await self.run(
            ["api", "--hostname", _SUPPORTED_HOST, "user"],
            str(self._config.data_dir),
            _DEFAULT_TIMEOUT,
        )
        if user_result.returncode != 0:
            state = "unauthenticated" if _is_unauthenticated(user_result) else "error"
            label = (
                "GitHub CLI authentication"
                if state == "unauthenticated"
                else "GitHub identity check"
            )
            return GitHubIdentityStatus(
                state=state,
                host=_SUPPORTED_HOST,
                login=None,
                credential_source=source,
                executable_path=executable_path,
                version=version,
                detail=_command_failure_detail(label, user_result),
                checked_at=checked_at,
            )

        login = _parse_login(user_result.stdout)
        if login is None:
            return GitHubIdentityStatus(
                state="error",
                host=_SUPPORTED_HOST,
                login=None,
                credential_source=source,
                executable_path=executable_path,
                version=version,
                detail="GitHub API user response was malformed",
                checked_at=checked_at,
            )
        return GitHubIdentityStatus(
            state="ready",
            host=_SUPPORTED_HOST,
            login=login,
            credential_source=source,
            executable_path=executable_path,
            version=version,
            detail=None,
            checked_at=checked_at,
        )

    async def _probe_upstream(
        self, upstream_url: str, identity: GitHubIdentityStatus
    ) -> GitHubTargetStatus:
        checked_at = _now_iso()
        try:
            target = parse_github_target(upstream_url)
        except ValueError:
            return GitHubTargetStatus(
                state="error",
                target=None,
                identity=identity.binding(),
                detail="registered upstream is not a supported GitHub target",
                checked_at=checked_at,
            )

        binding = identity.binding()
        if binding is None:
            return GitHubTargetStatus(
                state="unchecked",
                target=target,
                identity=None,
                detail=None,
                checked_at=checked_at,
            )

        repo_result = await self.run(
            ["api", "--hostname", target.host, f"repos/{target.slug}"],
            str(self._config.data_dir),
            _DEFAULT_TIMEOUT,
        )
        if repo_result.returncode != 0:
            state = "denied" if _is_repository_denial(repo_result) else "error"
            return GitHubTargetStatus(
                state=state,
                target=target,
                identity=binding,
                detail=_command_failure_detail("GitHub repository check", repo_result),
                checked_at=checked_at,
            )

        repository = _parse_json_object(repo_result.stdout)
        if repository is None:
            return GitHubTargetStatus(
                state="error",
                target=target,
                identity=binding,
                detail="GitHub repository response was malformed",
                checked_at=checked_at,
            )
        eligibility = _repository_eligibility(repository)
        if eligibility is not None:
            state, detail = eligibility
            return GitHubTargetStatus(
                state=state,
                target=target,
                identity=binding,
                detail=detail,
                checked_at=checked_at,
            )

        pulls_result = await self.run(
            ["api", "--hostname", target.host, f"repos/{target.slug}/pulls?per_page=1"],
            str(self._config.data_dir),
            _DEFAULT_TIMEOUT,
        )
        if pulls_result.returncode != 0:
            state = "denied" if _is_repository_denial(pulls_result) else "error"
            return GitHubTargetStatus(
                state=state,
                target=target,
                identity=binding,
                detail=_command_failure_detail(
                    "GitHub pull-request check", pulls_result
                ),
                checked_at=checked_at,
            )
        if _parse_json_array(pulls_result.stdout) is None:
            return GitHubTargetStatus(
                state="error",
                target=target,
                identity=binding,
                detail="GitHub pull-request response was malformed",
                checked_at=checked_at,
            )
        return GitHubTargetStatus(
            state="allowed",
            target=target,
            identity=binding,
            detail=None,
            checked_at=checked_at,
        )


def _first_line(text: str) -> str | None:
    for line in text.splitlines():
        value = line.strip()
        if value:
            return value
    return None


def _command_failure_detail(label: str, result: GitHubCommandResult) -> str:
    detail = (
        result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
    )
    # The runner has already redacted streams.  Keep status/error payloads
    # bounded because a hostile wrapper should not turn an error detail into an
    # unbounded event or REST response.
    return f"{label} failed: {detail[:1000]}"


def _is_unauthenticated(result: GitHubCommandResult) -> bool:
    if result.returncode == 4:  # GitHub CLI's documented auth-required exit code.
        return True
    detail = f"{result.stderr}\n{result.stdout}".lower()
    return any(
        marker in detail
        for marker in (
            "http 401",
            "bad credentials",
            "authentication required",
            "not logged into",
            "gh auth login",
            "no authentication token",
        )
    )


def _is_repository_denial(result: GitHubCommandResult) -> bool:
    detail = f"{result.stderr}\n{result.stdout}".lower()
    return (
        "http 403" in detail
        or "http 404" in detail
        or "resource not accessible" in detail
    )


def _parse_login(text: str) -> str | None:
    data = _parse_json_object(text)
    if data is None:
        return None
    login = data.get("login")
    if not isinstance(login, str):
        return None
    login = login.strip()
    if not _GITHUB_LOGIN_RE.fullmatch(login):
        return None
    return login


def _parse_json_object(text: str) -> dict[str, Any] | None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _parse_json_array(text: str) -> list[Any] | None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None


def _repository_eligibility(repository: dict[str, Any]) -> tuple[str, str] | None:
    """Return a terminal target state/detail, or ``None`` when it is eligible.

    Unknown values are intentionally errors rather than optimistic defaults.
    A false known policy/access bit is a denial, while an absent or malformed
    bit is indeterminate and therefore an error.
    """

    for field in ("archived", "disabled", "has_issues"):
        value = repository.get(field)
        if not isinstance(value, bool):
            return "error", f"GitHub repository response omitted boolean {field}"
    if repository["archived"]:
        return "denied", "repository is archived"
    if repository["disabled"]:
        return "denied", "repository is disabled"
    if not repository["has_issues"]:
        return "denied", "pull requests are disabled for the repository"

    policy = repository.get("pull_request_creation_policy")
    if not isinstance(policy, str):
        return (
            "error",
            "GitHub repository response omitted pull-request creation policy",
        )
    if policy == "all":
        return None
    if policy != "collaborators_only":
        return (
            "error",
            "GitHub repository response has an unsupported pull-request creation policy",
        )

    role = repository.get("role_name")
    permissions = repository.get("permissions")
    if not isinstance(role, str):
        return "error", "GitHub repository response omitted effective repository role"
    if not isinstance(permissions, dict) or not isinstance(
        permissions.get("pull"), bool
    ):
        return "error", "GitHub repository response omitted effective pull permission"
    normalized_role = role.strip().lower()
    pull = permissions["pull"]
    if not normalized_role or normalized_role == "none":
        if pull:
            return (
                "error",
                "GitHub repository response contradicted effective repository access",
            )
        return "denied", "account has no effective repository role"
    if not pull:
        return "denied", "account lacks pull permission for the repository"
    return None
