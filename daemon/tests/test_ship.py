"""Tests for `ompire_daemon.ship`."""

from __future__ import annotations

import asyncio
import os
import subprocess
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ompire_daemon.agent import AgentSupervisor
from ompire_daemon.app import create_app
from ompire_daemon.config import Config
from ompire_daemon.db import db_path_for, ensure_db_dir, make_engine
from ompire_daemon.events import EventHub
from ompire_daemon.gh import (
    GitHubCli,
    GitHubIdentityBinding,
    GitHubIdentityStatus,
    GitHubStatus,
    GitHubTargetStatus,
    parse_github_owner,
    parse_github_slug,
    parse_github_target,
)
from ompire_daemon.gpg import GpgProbe, GpgSelection, GpgStatus, parse_candidates
from ompire_daemon.registry.projects import create_project
from ompire_daemon.registry.tasks import (
    create_task,
    get_task,
    mark_archived,
    mark_pr_url,
)
from ompire_daemon.sessions import SessionTracker
from ompire_daemon.ship import (
    GitHubPreflightError,
    NoLiveAgentError,
    PushError,
    SessionNotIdleError,
    ShipAlreadyPublishedError,
    ShipDraft,
    ShipError,
    ShipInProgressError,
    ShipManager,
    ShipState,
    SshAuthenticationError,
    _find_pr_url,
    _parse_draft,
)
from ompire_daemon.spawn import StepFailedError


@pytest.fixture
def tmp_root(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def config(tmp_root: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    bin_dir = tmp_root / "bin"
    bin_dir.mkdir(exist_ok=True)
    _write_script(
        bin_dir,
        "gh",
        "#!/bin/sh\n"
        'case "$*" in\n'
        "'--version') echo 'gh version 2.97.0 (test)' ;;\n"
        "'api --hostname github.com user') echo '{\"login\":\"test-user\"}' ;;\n"
        "'api --hostname github.com repos/'*'/pulls?per_page=1') echo '[]' ;;\n"
        '\'api --hostname github.com repos/\'*) echo \'{"archived":false,"disabled":false,"has_issues":true,"pull_request_creation_policy":"all"}\' ;;\n'
        "'pr create'*) echo 'https://github.com/owner/repo/pull/42' ;;\n"
        '\'pr view\'*) echo \'{"state":"OPEN","mergedAt":null}\' ;;\n'
        '*) echo "unsupported gh invocation: $*" >&2; exit 1 ;;\n'
        "esac\n",
    )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return Config(
        data_dir=tmp_root / "data",
        task_dir_root=tmp_root / "tasks",
        checkout_root=tmp_root / "proj",
        spawn_step_timeout=30,
        gpg_signing_key="test@example.com",
    )


@pytest.fixture
def engine(config: Config):
    config.data_dir.mkdir(parents=True, exist_ok=True)
    db_path = db_path_for(config.data_dir)
    ensure_db_dir(db_path)
    eng = make_engine(db_path)
    # Apply schema migrations (tests that bypass create_app need this).
    from ompire_daemon.migrate import upgrade_head

    upgrade_head(db_path)
    return eng


@pytest.fixture
def hub() -> EventHub:
    return EventHub()


@pytest.fixture
def sessions(hub: EventHub) -> SessionTracker:
    return SessionTracker(hub, idle_debounce=0.1, stall_threshold=300)


@pytest.fixture
def agents(config: Config, hub: EventHub, sessions: SessionTracker) -> AgentSupervisor:
    return AgentSupervisor(config, hub, sessions)


@pytest.fixture
def gpg(config: Config, hub: EventHub) -> GpgProbe:
    return GpgProbe(config, hub)


class _AllowedGitHub:
    """Use the production redacting runner while isolating ship tests from
    repository-probe setup.  `test_gh.py` covers the probe's own contract.
    """

    def __init__(self, config: Config) -> None:
        self._cli = GitHubCli(config)

    async def run(self, args: list[str], cwd: str, timeout: int):
        return await self._cli.run(args, cwd, timeout)

    async def probe_target(self, upstream_url: str):
        target = parse_github_target(upstream_url)
        binding = GitHubIdentityBinding(
            host=target.host,
            login="test-user",
            credential_source="GitHub CLI configuration",
        )
        identity = GitHubIdentityStatus(
            state="ready",
            host=target.host,
            login=binding.login,
            credential_source=binding.credential_source,
            executable_path="/test/gh",
            version="gh version test",
            detail=None,
            checked_at="t0",
        )
        target_status = GitHubTargetStatus(
            state="allowed",
            target=target,
            identity=binding,
            detail=None,
            checked_at="t0",
        )
        status = GitHubStatus(
            identity=identity, targets={target.canonical: target_status}
        )
        return status, target_status


@pytest.fixture
def gh(config: Config) -> _AllowedGitHub:
    return _AllowedGitHub(config)


@pytest.fixture
def ships(
    config: Config,
    engine,
    hub: EventHub,
    sessions: SessionTracker,
    agents: AgentSupervisor,
    gpg: GpgProbe,
    gh: _AllowedGitHub,
) -> ShipManager:
    return ShipManager(config, engine, hub, sessions, agents, gpg, gh)


@pytest.fixture
def app(config: Config):
    return create_app(config)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth_header(app):
    return {"Authorization": f"Bearer {app.state.auth_token}"}


def _write_script(bin_dir: Path, name: str, content: str) -> Path:
    script = bin_dir / name
    script.write_text(content, encoding="utf-8")
    script.chmod(0o755)
    return script


def _run_git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def _setup_signing_gpg(
    bin_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, str]:
    """Generate a throwaway GPG key.

    Returns the wrapper script that reaches its keyring and the key's real
    fingerprint, which the daemon now passes to `git commit -S<key>` and
    verifies afterwards.
    """
    gnupg_home = bin_dir / "gnupg"
    gnupg_home.mkdir(parents=True, exist_ok=True)
    gpg_bin = subprocess.run(
        ["which", "gpg"], check=True, capture_output=True, text=True
    ).stdout.strip()
    key_spec = bin_dir / "key-spec"
    key_spec.write_text(
        textwrap.dedent(
            """\
            %echo generating
            Key-Type: RSA
            Key-Length: 2048
            Key-Usage: cert
            Subkey-Type: RSA
            Subkey-Length: 2048
            Subkey-Usage: sign
            Name-Real: Test User
            Name-Email: test@example.com
            Expire-Date: 0
            %no-protection
            %commit
            %echo done
            """
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [gpg_bin, "--batch", "--gen-key", str(key_spec)],
        check=True,
        env={**os.environ, "GNUPGHOME": str(gnupg_home)},
        capture_output=True,
    )
    wrapper = bin_dir / "gpg-wrapper"
    wrapper.write_text(
        f'#!/bin/sh\nexport GNUPGHOME={gnupg_home}\nexec {gpg_bin} "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    listing = subprocess.run(
        [
            gpg_bin, "--list-secret-keys", "--with-colons", "--with-keygrip",
            "test@example.com",
        ],
        check=True,
        env={**os.environ, "GNUPGHOME": str(gnupg_home)},
        capture_output=True,
        text=True,
    ).stdout
    # Use the daemon's own parser: the key git actually signs with is the
    # signing subkey, which is exactly the candidate the probe would select.
    candidates = parse_candidates(listing)
    assert len(candidates) == 1, candidates
    fingerprint = candidates[0].fingerprint
    # The daemon reads gpg.program from operator-owned config, never the
    # clone's. Redirect the global scope to a temp file so the suite cannot
    # touch the developer's real ~/.gitconfig.
    _set_operator_signing_program(bin_dir, wrapper, monkeypatch)
    return wrapper, fingerprint


def _set_operator_signing_program(
    bin_dir: Path, program: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point operator-owned Git config at `program`, never the clone's."""
    global_config = bin_dir / "gitconfig"
    global_config.write_text(f"[gpg]\n\tprogram = {program}\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))


def _setup_git_clone(origin_path: Path, clone_path: Path) -> None:
    """Create a bare `origin`, clone it, put a base branch and a task branch
    with multiple commits plus an uncommitted working-tree edit.
    """
    origin_path.mkdir(parents=True, exist_ok=True)
    _run_git(origin_path, "init", "--bare")
    clone_path.parent.mkdir(parents=True, exist_ok=True)
    _run_git(clone_path.parent, "clone", str(origin_path), clone_path.name)
    _run_git(clone_path, "config", "user.email", "test@example.com")
    _run_git(clone_path, "config", "user.name", "Test User")

    (clone_path / "base.txt").write_text("base\n", encoding="utf-8")
    _run_git(clone_path, "add", ".")
    _run_git(clone_path, "commit", "-m", "base commit")
    _run_git(clone_path, "push", "origin", "HEAD:main")

    _run_git(clone_path, "checkout", "-b", "ompire/task-1")
    (clone_path / "file1.txt").write_text("one\n", encoding="utf-8")
    _run_git(clone_path, "add", ".")
    _run_git(clone_path, "commit", "-m", "first task commit")
    (clone_path / "file2.txt").write_text("two\n", encoding="utf-8")
    _run_git(clone_path, "add", ".")
    _run_git(clone_path, "commit", "-m", "second task commit")
    # Uncommitted working-tree change.
    (clone_path / "file3.txt").write_text("three\n", encoding="utf-8")


_TEST_FINGERPRINT = "B4C4207720270E2FB99002559F1C030DE2985A55"


def _selection(fingerprint: str) -> GpgSelection:
    return GpgSelection(
        fingerprint=fingerprint,
        key_id=fingerprint[-16:],
        uid="Test User <test@example.com>",
        keygrip="8C9301DF2FFD432192448A04C8F2A6BA372A1830",
        source="auto",
        protection="unprotected",
    )


def _ready_gpg_probe(fingerprint: str = _TEST_FINGERPRINT):
    async def probe():
        return GpgStatus(state="ready", selected=_selection(fingerprint))

    return probe


def _blocked_gpg_probe(state: str = "locked"):
    async def probe():
        return GpgStatus(state=state, selected=_selection(_TEST_FINGERPRINT))

    return probe


def _blocked_preflight_error(upstream_url: str) -> GitHubPreflightError:
    target = parse_github_target(upstream_url)
    identity = GitHubIdentityStatus(
        state="unauthenticated",
        host=target.host,
        login=None,
        credential_source="GH_TOKEN",
        executable_path="/test/gh",
        version="gh version test",
        detail="GitHub CLI authentication failed: HTTP 401: Bad credentials",
        checked_at="t0",
    )
    target_status = GitHubTargetStatus(
        state="unchecked", target=target, identity=None, detail=None, checked_at="t0"
    )
    return GitHubPreflightError(
        GitHubStatus(identity, {target.canonical: target_status}), target_status
    )


def _make_project_and_task(
    engine,
    tmp_root: Path,
    upstream_url: str = "https://github.com/owner/repo",
    fork_url: str | None = None,
) -> tuple:
    checkout_dir = tmp_root / "proj" / "myproject"
    checkout_dir.mkdir(parents=True, exist_ok=True)
    project = create_project(
        engine,
        name="myproject",
        title="My Project",
        upstream_url=upstream_url,
        fork_url=fork_url,
        checkout_path=str(checkout_dir),
        default_checkout_root=tmp_root / "proj",
    )
    clone_path = tmp_root / "tasks" / "myproject" / "task-1"
    clone_path.parent.mkdir(parents=True, exist_ok=True)
    task = create_task(
        engine,
        project_name="myproject",
        slug="task-1",
        branch="ompire/task-1",
        clone_path=str(clone_path),
        prompt="do the thing",
    )
    return project, task


@pytest.mark.parametrize("mode", ["squash", "retain"])
async def test_direct_preflight_blocks_before_any_local_ship_mutation(
    tmp_root, engine, ships, hub, monkeypatch, mode
):
    project, task = _make_project_and_task(engine, tmp_root)
    origin = tmp_root / "preflight-origin.git"
    _setup_git_clone(origin, Path(task.clone_path))
    clone = Path(task.clone_path)
    original_head = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    original_tree = (clone / "file3.txt").read_text(encoding="utf-8")
    error = _blocked_preflight_error(project.upstream_url)

    async def blocked(_upstream_url: str):
        return error.status, error.target

    monkeypatch.setattr(ships._gh, "probe_target", blocked)
    events = hub.subscribe()
    try:
        with pytest.raises(GitHubPreflightError):
            await ships.commit_and_ship(task, "message", "title", "body", mode=mode)
        assert (
            subprocess.run(
                ["git", "-C", str(clone), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            == original_head
        )
        assert (clone / "file3.txt").read_text(encoding="utf-8") == original_tree
        assert (
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(clone),
                    "rev-parse",
                    "--verify",
                    "refs/ompire/ship-orig",
                ],
                capture_output=True,
                text=True,
                check=False,
            ).returncode
            != 0
        )
        assert ships.get(task.id) is None
        assert ships._backgrounds == {}
        assert events.empty()
    finally:
        hub.unsubscribe(events)


# --- URL parsing ----------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("git@github.com:owner/repo.git", "owner/repo"),
        ("git@github.com:owner/repo", "owner/repo"),
        ("https://github.com/owner/repo.git", "owner/repo"),
        ("https://github.com/owner/repo", "owner/repo"),
    ],
)
def test_parse_github_slug(url, expected):
    assert parse_github_slug(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.com/owner/repo",
        "not-a-url",
        "https://github.com/owner",
    ],
)
def test_parse_github_slug_rejects_non_github(url):
    with pytest.raises(ValueError):
        parse_github_slug(url)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("git@github.com:forkowner/repo.git", "forkowner"),
        ("https://github.com/forkowner/repo", "forkowner"),
    ],
)
def test_parse_github_owner(url, expected):
    assert parse_github_owner(url) == expected


# --- draft parsing --------------------------------------------------------


def test_parse_draft_extracts_sections():
    text = textwrap.dedent(
        """\
        <<<COMMIT_MESSAGE>>>
        Add feature

        <<<PR_TITLE>>>
        Add the feature

        <<<PR_BODY>>>
        This adds the feature.
        """
    )
    draft = _parse_draft(text)
    assert draft.commit_message == "Add feature"
    assert draft.pr_title == "Add the feature"
    assert draft.pr_body == "This adds the feature."


def test_parse_draft_returns_none_on_missing_marker():
    assert _parse_draft("no markers") is None


# --- commit flow ----------------------------------------------------------


async def test_commit_and_ship_squashes_delta(
    tmp_root, engine, ships, gpg, monkeypatch
):
    monkeypatch.setattr(gpg, "probe", _ready_gpg_probe())

    origin = tmp_root / "origin.git"
    _project, task = _make_project_and_task(
        engine, tmp_root, upstream_url="git@github.com:owner/repo.git"
    )

    bin_dir = tmp_root / "bin"
    bin_dir.mkdir(exist_ok=True)
    _write_script(
        bin_dir,
        "fake-gpg",
        "#!/bin/sh\ncat <<'EOF'\n-----BEGIN PGP SIGNATURE-----\n\nxxxx\n-----END PGP SIGNATURE-----\nEOF\n",
    )
    _write_script(
        bin_dir,
        "gh",
        "#!/bin/sh\necho 'https://github.com/owner/repo/pull/42'\n",
    )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    _setup_git_clone(origin, Path(task.clone_path))
    _wrapper, _fingerprint = _setup_signing_gpg(bin_dir, monkeypatch)
    monkeypatch.setattr(gpg, "probe", _ready_gpg_probe(_fingerprint))

    # Assert the ship flow pushes to the project's upstream_url (not the
    # clone's local-path origin), then redirect the transport to the local
    # bare repo so the push actually lands. See test_push_and_pr_routes_to_*.
    real_ensure = ships._ensure_ship_remote
    seen_urls: list[str] = []

    async def ensure_local(clone_path, url, timeout):
        seen_urls.append(url)
        return await real_ensure(clone_path, str(origin), timeout)

    monkeypatch.setattr(ships, "_ensure_ship_remote", ensure_local)

    state = await ships.commit_and_ship(
        task,
        message="Final squash commit",
        pr_title="Final PR",
        pr_body="Body text",
    )

    assert state.status == "shipped", state.error
    assert state.commit_sha is not None
    assert state.pr_url == "https://github.com/owner/repo/pull/42"
    assert seen_urls == ["git@github.com:owner/repo.git"]

    persisted = get_task(engine, task.id)
    assert persisted.pr_url == "https://github.com/owner/repo/pull/42"

    # Exactly one commit ahead of origin/main.
    clone = Path(task.clone_path)
    log = subprocess.run(
        ["git", "-C", str(clone), "log", "--oneline", "origin/main..HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert len(log.strip().splitlines()) == 1
    assert "Final squash commit" in log

    # The uncommitted file made it into the commit.
    assert (clone / "file3.txt").read_text() == "three\n"


async def test_clone_local_signing_config_cannot_redirect_the_commit(
    tmp_root, engine, ships, gpg, monkeypatch
):
    """The clone is agent-writable, so nothing it says about signing counts.

    A clone-local `gpg.program` would otherwise make the daemon execute an
    arbitrary binary on the host, outside the sandbox, as the operator.
    """
    origin = tmp_root / "origin.git"
    _project, task = _make_project_and_task(engine, tmp_root)

    bin_dir = tmp_root / "bin"
    bin_dir.mkdir(exist_ok=True)
    _write_script(
        bin_dir, "gh", "#!/bin/sh\necho 'https://github.com/owner/repo/pull/42'\n"
    )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    _setup_git_clone(origin, Path(task.clone_path))
    _wrapper, fingerprint = _setup_signing_gpg(bin_dir, monkeypatch)
    monkeypatch.setattr(gpg, "probe", _ready_gpg_probe(fingerprint))

    # What a compromised agent would write into the task clone.
    tripwire = tmp_root / "tripwire"
    hostile = _write_script(
        bin_dir,
        "hostile-gpg",
        f"#!/bin/sh\ntouch {tripwire}\nexit 1\n",
    )
    clone = Path(task.clone_path)
    _run_git(clone, "config", "gpg.program", str(hostile))
    _run_git(clone, "config", "gpg.format", "ssh")
    _run_git(clone, "config", "user.signingkey", "DEADBEEFDEADBEEF")

    real_ensure = ships._ensure_ship_remote

    async def ensure_local(clone_path, url, timeout):
        return await real_ensure(clone_path, str(origin), timeout)

    monkeypatch.setattr(ships, "_ensure_ship_remote", ensure_local)

    state = await ships.commit_and_ship(
        task, message="Signed", pr_title="Title", pr_body="Body"
    )

    assert state.status == "shipped", state.error
    assert not tripwire.exists(), "clone-local gpg.program was executed"

    signer = subprocess.run(
        [
            "git", "-C", task.clone_path,
            "-c", "gpg.format=openpgp", "-c", f"gpg.program={_wrapper}",
            "log", "-1", "--format=%GF",
        ],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert signer == fingerprint


async def test_commit_failure_restores_head_and_cleans_ref(
    tmp_root, engine, ships, gpg, monkeypatch, caplog
):
    monkeypatch.setattr(gpg, "probe", _ready_gpg_probe())

    origin = tmp_root / "origin.git"
    _project, task = _make_project_and_task(engine, tmp_root)

    # Fake gpg that fails signing.
    tmp_root.joinpath("bin").mkdir(exist_ok=True)
    fake_gpg = _write_script(
        tmp_root / "bin",
        "fake-gpg-fail",
        "#!/bin/sh\necho 'signing failed' >&2\nexit 2\n",
    )
    monkeypatch.setenv("PATH", f"{tmp_root / 'bin'}{os.pathsep}{os.environ['PATH']}")

    _setup_git_clone(origin, Path(task.clone_path))
    _set_operator_signing_program(tmp_root / "bin", fake_gpg, monkeypatch)

    orig_sha = subprocess.run(
        ["git", "-C", task.clone_path, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    state = await ships.commit_and_ship(
        task,
        message="Will fail",
        pr_title="Title",
        pr_body="Body",
    )

    assert state.status == "error"
    assert state.commit_sha is None
    # The commit's stderr must reach the operator, not just the step name
    # (dogfooding: "spawn step 'ship-commit' failed" carried no diagnosis).
    assert state.error is not None
    assert state.error.startswith("commit failed:")
    assert "gpg failed to sign" in state.error
    # And the failure must reach the daemon log so journald shows it too.
    assert any(
        "ship commit failed" in rec.getMessage() and "gpg failed to sign" in rec.getMessage()
        for rec in caplog.records
    )

    # HEAD restored to original.
    head_sha = subprocess.run(
        ["git", "-C", task.clone_path, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert head_sha == orig_sha

    # Ship-orig ref cleaned up.
    result = subprocess.run(
        [
            "git",
            "-C",
            task.clone_path,
            "rev-parse",
            "--verify",
            "refs/ompire/ship-orig",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0


async def test_commit_and_ship_retain_rewrites_range(
    tmp_root, engine, ships, gpg, monkeypatch
):
    monkeypatch.setattr(gpg, "probe", _ready_gpg_probe())

    origin = tmp_root / "origin.git"
    _project, task = _make_project_and_task(
        engine, tmp_root, upstream_url="git@github.com:owner/repo.git"
    )

    bin_dir = tmp_root / "bin"
    bin_dir.mkdir(exist_ok=True)
    _write_script(
        bin_dir,
        "gh",
        "#!/bin/sh\necho 'https://github.com/owner/repo/pull/42'\n",
    )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    _setup_git_clone(origin, Path(task.clone_path))
    _wrapper, _fingerprint = _setup_signing_gpg(bin_dir, monkeypatch)
    monkeypatch.setattr(gpg, "probe", _ready_gpg_probe(_fingerprint))
    # Clean up the working tree so retain mode accepts the clone.
    _run_git(Path(task.clone_path), "add", "file3.txt")
    _run_git(Path(task.clone_path), "commit", "-m", "stage working edit")

    clone = Path(task.clone_path)
    pre = (
        subprocess.run(
            ["git", "-C", str(clone), "log", "--format=%T %s", "origin/main..HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        .stdout.strip()
        .splitlines()
    )

    real_ensure = ships._ensure_ship_remote
    seen_urls: list[str] = []

    async def ensure_local(clone_path, url, timeout):
        seen_urls.append(url)
        return await real_ensure(clone_path, str(origin), timeout)

    monkeypatch.setattr(ships, "_ensure_ship_remote", ensure_local)

    state = await ships.commit_and_ship(
        task,
        message="ignored in retain",
        pr_title="Final PR",
        pr_body="Body text",
        mode="retain",
    )

    assert state.status == "shipped", state.error
    assert state.mode == "retain"
    assert state.commit_sha is not None
    assert state.pr_url == "https://github.com/owner/repo/pull/42"
    assert seen_urls == ["git@github.com:owner/repo.git"]

    # Same number of commits and same trees/subjects.
    post = (
        subprocess.run(
            ["git", "-C", str(clone), "log", "--format=%T %s", "origin/main..HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        .stdout.strip()
        .splitlines()
    )
    assert len(post) == len(pre)
    assert post == pre

    # Every commit operator-authored and well-signed.
    authors = (
        subprocess.run(
            [
                "git",
                "-C",
                str(clone),
                "log",
                "--format=%an|%ae|%cn|%ce|%G?",
                "origin/main..HEAD",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        .stdout.strip()
        .splitlines()
    )
    for line in authors:
        an, ae, cn, ce, sig = line.split("|")
        assert an == "Test User"
        assert ae == "test@example.com"
        assert cn == "Test User"
        assert ce == "test@example.com"
        assert sig in ("G", "U")


async def test_commit_and_ship_retain_refuses_dirty_tree(
    tmp_root, engine, ships, gpg, monkeypatch
):
    monkeypatch.setattr(gpg, "probe", _ready_gpg_probe())

    origin = tmp_root / "origin.git"
    _project, task = _make_project_and_task(engine, tmp_root)

    _setup_git_clone(origin, Path(task.clone_path))

    state = await ships.commit_and_ship(
        task,
        message="m",
        pr_title="t",
        pr_body="b",
        mode="retain",
    )

    assert state.status == "error"
    assert "dirty" in state.error.lower()


async def test_commit_and_ship_retain_refuses_merge_commits(
    tmp_root, engine, ships, gpg, monkeypatch
):
    monkeypatch.setattr(gpg, "probe", _ready_gpg_probe())

    origin = tmp_root / "origin.git"
    _project, task = _make_project_and_task(engine, tmp_root)

    _setup_git_clone(origin, Path(task.clone_path))
    _run_git(Path(task.clone_path), "add", "file3.txt")
    _run_git(Path(task.clone_path), "commit", "-m", "stage working edit")

    clone = Path(task.clone_path)
    _run_git(clone, "checkout", "-b", "side")
    (clone / "side.txt").write_text("side\n", encoding="utf-8")
    _run_git(clone, "add", ".")
    _run_git(clone, "commit", "-m", "side commit")
    _run_git(clone, "checkout", "ompire/task-1")
    _run_git(clone, "merge", "--no-ff", "side", "-m", "merge side")

    state = await ships.commit_and_ship(
        task,
        message="m",
        pr_title="t",
        pr_body="b",
        mode="retain",
    )

    assert state.status == "error"
    assert "merge" in state.error.lower()


async def test_commit_and_ship_retain_refuses_empty_range(
    tmp_root, engine, ships, gpg, monkeypatch
):
    monkeypatch.setattr(gpg, "probe", _ready_gpg_probe())

    origin = tmp_root / "origin.git"
    _project, task = _make_project_and_task(engine, tmp_root)

    _setup_git_clone(origin, Path(task.clone_path))
    # Drop the uncommitted change and reset the branch to main.
    (Path(task.clone_path) / "file3.txt").unlink()
    _run_git(Path(task.clone_path), "checkout", "main")
    _run_git(Path(task.clone_path), "checkout", "-B", "ompire/task-1")

    state = await ships.commit_and_ship(
        task,
        message="m",
        pr_title="t",
        pr_body="b",
        mode="retain",
    )

    assert state.status == "error"
    assert state.commit_sha is None


async def test_commit_and_ship_retain_amend_failure_restores_head(
    tmp_root, engine, ships, gpg, monkeypatch
):
    monkeypatch.setattr(gpg, "probe", _ready_gpg_probe())

    origin = tmp_root / "origin.git"
    _project, task = _make_project_and_task(engine, tmp_root)

    tmp_root.joinpath("bin").mkdir(exist_ok=True)
    fake_gpg = _write_script(
        tmp_root / "bin",
        "fake-gpg-fail",
        "#!/bin/sh\necho 'signing failed' >&2\nexit 2\n",
    )
    monkeypatch.setenv("PATH", f"{tmp_root / 'bin'}{os.pathsep}{os.environ['PATH']}")

    _setup_git_clone(origin, Path(task.clone_path))
    _wrapper, _fingerprint = _setup_signing_gpg(tmp_root / "bin", monkeypatch)
    monkeypatch.setattr(gpg, "probe", _ready_gpg_probe(_fingerprint))
    _run_git(Path(task.clone_path), "add", "file3.txt")
    _run_git(Path(task.clone_path), "commit", "-m", "stage working edit")
    # Swap to failing gpg so the rebase amend fails mid-range.
    _set_operator_signing_program(tmp_root / "bin", fake_gpg, monkeypatch)

    orig_sha = subprocess.run(
        ["git", "-C", task.clone_path, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    state = await ships.commit_and_ship(
        task,
        message="Will fail",
        pr_title="Title",
        pr_body="Body",
        mode="retain",
    )

    assert state.status == "error"
    assert state.commit_sha is None

    # HEAD restored to original and tree is clean.
    head_sha = subprocess.run(
        ["git", "-C", task.clone_path, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert head_sha == orig_sha

    status = subprocess.run(
        ["git", "-C", task.clone_path, "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert status == ""

    # No rebase in progress and ref cleaned up.
    assert not (Path(task.clone_path) / ".git" / "rebase-merge").exists()
    assert not (Path(task.clone_path) / ".git" / "rebase-apply").exists()
    result = subprocess.run(
        [
            "git",
            "-C",
            task.clone_path,
            "rev-parse",
            "--verify",
            "refs/ompire/ship-orig",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0


async def test_commit_and_ship_retain_verification_failure_restores_head(
    tmp_root, engine, ships, gpg, monkeypatch
):
    monkeypatch.setattr(gpg, "probe", _ready_gpg_probe())

    origin = tmp_root / "origin.git"
    _project, task = _make_project_and_task(engine, tmp_root)

    _setup_git_clone(origin, Path(task.clone_path))
    _wrapper, _fingerprint = _setup_signing_gpg(tmp_root / "bin", monkeypatch)
    monkeypatch.setattr(gpg, "probe", _ready_gpg_probe(_fingerprint))
    _run_git(Path(task.clone_path), "add", "file3.txt")
    _run_git(Path(task.clone_path), "commit", "-m", "stage working edit")

    from ompire_daemon import ship as ship_module

    real_run = ship_module._run_git_output

    async def fake_sigs(argv, cwd, timeout, step_name):
        if step_name == "ship-verify-signatures":
            return f"{'a' * 40} G {_fingerprint}\n{'b' * 40} X {_fingerprint}\n"
        return await real_run(argv, cwd, timeout, step_name)

    monkeypatch.setattr(ship_module, "_run_git_output", fake_sigs)

    orig_sha = subprocess.run(
        ["git", "-C", task.clone_path, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    state = await ships.commit_and_ship(
        task,
        message="m",
        pr_title="t",
        pr_body="b",
        mode="retain",
    )

    assert state.status == "error"
    assert "signature" in state.error.lower()

    head_sha = subprocess.run(
        ["git", "-C", task.clone_path, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert head_sha == orig_sha


# --- push routing ----------------------------------------------------------


async def test_push_and_pr_routes_to_fork(tmp_root, engine, ships, gpg, monkeypatch):
    monkeypatch.setattr(gpg, "probe", _ready_gpg_probe())

    project, task = _make_project_and_task(
        engine,
        tmp_root,
        upstream_url="https://github.com/upowner/uprepo",
        fork_url="git@github.com:forkowner/uprepo.git",
    )

    pushed: list = []
    created: dict = {}

    async def fake_push(clone_path, remote_url, branch):
        pushed.append((remote_url, branch))

    async def fake_create_pr(clone_path, upstream_slug, base_branch, head, title, body):
        created.update(
            {
                "upstream": upstream_slug,
                "base": base_branch,
                "head": head,
                "title": title,
                "body": body,
            }
        )
        return "https://github.com/upowner/uprepo/pull/7"

    monkeypatch.setattr(ships, "_push", fake_push)
    monkeypatch.setattr(ships, "_create_pr", fake_create_pr)

    ships._set_state(task.id, status="committing")
    ships._set_state(task.id, commit_sha="abc123")
    url = await ships._push_and_pr(
        task, project, "main", pr_title="Title", pr_body="Body"
    )

    assert url == "https://github.com/upowner/uprepo/pull/7"
    assert pushed == [("git@github.com:forkowner/uprepo.git", "ompire/task-1")]
    assert created["upstream"] == "upowner/uprepo"
    assert created["head"] == "forkowner:ompire/task-1"
    assert created["base"] == "main"


async def test_push_and_pr_routes_to_upstream_without_fork(
    tmp_root, engine, ships, gpg, monkeypatch
):
    """Task clones have origin = the local checkout path, so a non-fork push
    must target the project's upstream URL, never the `origin` name (found via
    dogfooding: pushes to `origin` updated only the local checkout)."""
    monkeypatch.setattr(gpg, "probe", _ready_gpg_probe())

    project, task = _make_project_and_task(
        engine,
        tmp_root,
        upstream_url="git@github.com:upowner/uprepo.git",
    )

    pushed: list = []

    async def fake_push(clone_path, remote_url, branch):
        pushed.append((remote_url, branch))

    async def fake_create_pr(clone_path, upstream_slug, base_branch, head, title, body):
        assert head == task.branch
        return "https://github.com/upowner/uprepo/pull/9"

    monkeypatch.setattr(ships, "_push", fake_push)
    monkeypatch.setattr(ships, "_create_pr", fake_create_pr)

    ships._set_state(task.id, status="committing")
    ships._set_state(task.id, commit_sha="abc123")
    url = await ships._push_and_pr(
        task, project, "main", pr_title="Title", pr_body="Body"
    )

    assert url == "https://github.com/upowner/uprepo/pull/9"
    assert pushed == [("git@github.com:upowner/uprepo.git", "ompire/task-1")]


async def test_push_and_pr_revalidates_before_pull_request_creation(
    tmp_root, engine, ships, monkeypatch
):
    project, task = _make_project_and_task(engine, tmp_root)
    error = _blocked_preflight_error(project.upstream_url)
    calls: list[str] = []

    async def fake_push(*_args):
        calls.append("push")

    async def blocked_target(_upstream_url: str):
        calls.append("preflight")
        return error.status, error.target

    async def should_not_create(*_args):
        calls.append("create")
        raise AssertionError(
            "pull request creation must be blocked by the second preflight"
        )

    monkeypatch.setattr(ships, "_push", fake_push)
    monkeypatch.setattr(ships._gh, "probe_target", blocked_target)
    monkeypatch.setattr(ships, "_create_pr", should_not_create)

    with pytest.raises(GitHubPreflightError):
        await ships._push_and_pr(task, project, "main", "title", "body")
    assert calls == ["push", "preflight"]


async def test_push_uses_force_with_lease(tmp_root, engine, ships):
    origin = tmp_root / "origin-push.git"
    origin.mkdir()
    _run_git(origin, "init", "--bare")

    clone = tmp_root / "push-clone"
    _run_git(tmp_root, "clone", str(origin), str(clone.name))
    _run_git(clone, "config", "user.email", "t@e.com")
    _run_git(clone, "config", "user.name", "T")
    (clone / "a").write_text("a")
    _run_git(clone, "add", ".")
    _run_git(clone, "commit", "-m", "initial")
    _run_git(clone, "push", "origin", "HEAD:main")

    await ships._push(clone, str(origin), "feature")

    branches = subprocess.run(
        ["git", "-C", str(origin), "branch", "--list"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "feature" in branches


async def test_push_failure_surfaces_remote_stderr(tmp_root, engine, ships):
    origin = tmp_root / "origin-reject.git"
    origin.mkdir()
    _run_git(origin, "init", "--bare")
    hook = origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'forge: rejecting push (injected)' >&2\nexit 1\n")
    hook.chmod(0o755)

    clone = tmp_root / "reject-clone"
    _run_git(tmp_root, "clone", str(origin), str(clone.name))
    _run_git(clone, "config", "user.email", "t@e.com")
    _run_git(clone, "config", "user.name", "T")
    (clone / "a").write_text("a")
    _run_git(clone, "add", ".")
    _run_git(clone, "commit", "-m", "initial")

    with pytest.raises(ShipError, match="forge: rejecting push"):
        await ships._push(clone, str(origin), "feature")


async def test_ssh_authentication_classification_is_narrow(
    tmp_root, engine, ships, monkeypatch
):
    async def named_remote(_clone_path, _remote_url, _timeout):
        return "ship-target"

    async def rejected(step):
        raise StepFailedError(step.name, "Permission denied (publickey)")

    monkeypatch.setattr(ships, "_ensure_ship_remote", named_remote)
    monkeypatch.setattr("ompire_daemon.ship._run_step", rejected)

    with pytest.raises(SshAuthenticationError, match="Permission denied"):
        await ships._push("/irrelevant", "git@github.com:owner/repo.git", "feature")
    with pytest.raises(PushError) as ordinary:
        await ships._push("/irrelevant", "https://github.com/owner/repo.git", "feature")
    assert not isinstance(ordinary.value, SshAuthenticationError)

async def test_ssh_authentication_during_ship_remote_setup_is_a_push_failure(
    tmp_root, engine, ships, monkeypatch
):
    async def denied_setup(_clone_path, _remote_url, _timeout):
        raise StepFailedError("ship-fetch-target", "Permission denied (publickey)")

    monkeypatch.setattr(ships, "_ensure_ship_remote", denied_setup)

    with pytest.raises(SshAuthenticationError, match="Permission denied"):
        await ships._push("/irrelevant", "git@github.com:owner/repo.git", "feature")


async def test_commit_records_push_failure_as_push_stage(
    tmp_root, engine, ships, gpg, monkeypatch
):
    monkeypatch.setattr(gpg, "probe", _ready_gpg_probe())
    project, task = _make_project_and_task(
        engine, tmp_root, upstream_url="git@github.com:owner/repo.git"
    )
    origin = tmp_root / "push-stage-origin.git"
    _setup_git_clone(origin, Path(task.clone_path))
    hook = origin / "hooks" / "pre-receive"
    hook.write_text("#!/bin/sh\necho 'forge: rejecting push (injected)' >&2\nexit 1\n")
    hook.chmod(0o755)
    _wrapper, _fingerprint = _setup_signing_gpg(tmp_root / "bin", monkeypatch)
    monkeypatch.setattr(gpg, "probe", _ready_gpg_probe(_fingerprint))

    real_ensure = ships._ensure_ship_remote

    async def ensure_local(clone_path, url, timeout):
        assert url == project.upstream_url
        return await real_ensure(clone_path, str(origin), timeout)

    monkeypatch.setattr(ships, "_ensure_ship_remote", ensure_local)
    state = await ships.commit_and_ship(task, "message", "title", "body")

    assert state.status == "error"
    assert state.error is not None and state.error.startswith("push failed:")
    assert "forge: rejecting push" in state.error
    assert state.last_step is not None
    assert (state.last_step.step, state.last_step.status) == ("push", "failed")


async def test_pr_creation_failure_sanitizes_state_and_events(
    tmp_root, engine, ships, gpg, hub, monkeypatch
):
    monkeypatch.setattr(gpg, "probe", _ready_gpg_probe())
    project, task = _make_project_and_task(
        engine, tmp_root, upstream_url="git@github.com:owner/repo.git"
    )
    origin = tmp_root / "pr-stage-origin.git"
    bin_dir = tmp_root / "bin"
    bin_dir.mkdir(exist_ok=True)
    secret = "exact-pr-secret"
    _write_script(
        bin_dir,
        "gh",
        "#!/bin/sh\n"
        "printf 'ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 %s\\n' \"$GH_TOKEN\"\n"
        'printf \'Authorization: Bearer %s\\nhttps://user:%s@github.com/owner/repo\\n\' "$GH_TOKEN" "$GH_TOKEN" >&2\n'
        "exit 1\n",
    )
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_TOKEN", secret)
    _setup_git_clone(origin, Path(task.clone_path))
    _wrapper, _fingerprint = _setup_signing_gpg(bin_dir, monkeypatch)
    monkeypatch.setattr(gpg, "probe", _ready_gpg_probe(_fingerprint))
    real_ensure = ships._ensure_ship_remote

    async def ensure_local(clone_path, url, timeout):
        assert url == project.upstream_url
        return await real_ensure(clone_path, str(origin), timeout)

    monkeypatch.setattr(ships, "_ensure_ship_remote", ensure_local)
    events = hub.subscribe()
    try:
        state = await ships.commit_and_ship(task, "message", "title", "body")
        payloads = []
        while not events.empty():
            payloads.append(events.get_nowait().payload)
    finally:
        hub.unsubscribe(events)

    published = "\n".join([state.error or "", *(str(payload) for payload in payloads)])
    assert state.status == "error"
    assert state.error is not None and state.error.startswith(
        "pull-request creation failed:"
    )
    assert state.last_step is not None
    assert (state.last_step.step, state.last_step.status) == ("pr", "failed")
    for credential in (
        secret,
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        "Bearer",
        "user",
    ):
        assert credential not in published
    assert "[redacted]" in published


async def test_create_pr_parses_url_and_adopts_existing_pr(
    tmp_root, engine, ships, monkeypatch
):
    tmp_root.joinpath("bin").mkdir(exist_ok=True)
    _write_script(
        tmp_root / "bin",
        "gh",
        "#!/bin/sh\n"
        "echo 'a pull request for branch \"feature\" already exists:' >&2\n"
        "echo 'https://github.com/owner/repo/pull/99' >&2\n"
        "exit 1\n",
    )
    monkeypatch.setenv("PATH", f"{tmp_root / 'bin'}{os.pathsep}{os.environ['PATH']}")

    clone = tmp_root / "pr-clone"
    clone.mkdir()
    url = await ships._create_pr(
        str(clone),
        upstream_slug="owner/repo",
        base_branch="main",
        head="forkowner:feature",
        title="Title",
        body="Body",
    )
    assert url == "https://github.com/owner/repo/pull/99"


def test_find_pr_url():
    text = "some text https://github.com/foo/bar/pull/123 more"
    assert _find_pr_url(text) == "https://github.com/foo/bar/pull/123"
    assert _find_pr_url("no url") is None


# --- draft via agent ------------------------------------------------------


class FakeAgentHandle:
    returncode = None

    def __init__(
        self,
        draft_text: str | None,
        *,
        prompt_started: asyncio.Event | None = None,
        prompt_release: asyncio.Event | None = None,
        request_error: Exception | None = None,
    ):
        self._draft = draft_text
        self._prompt_started = prompt_started
        self._prompt_release = prompt_release
        self._request_error = request_error
        self.prompt_count = 0

    async def prompt(self, message: str) -> dict:
        self.prompt_count += 1
        if self._prompt_started is not None:
            self._prompt_started.set()
        if self._prompt_release is not None:
            await self._prompt_release.wait()
        return {"ok": True}

    async def request(self, request_type: str, **fields) -> dict:
        if self._request_error is not None:
            raise self._request_error
        if request_type == "get_last_assistant_text":
            # Live omp wraps the text: {"success": true, "data": {"text": ...}}
            # (same shape advisories.py reads). A bare string here once masked
            # a production bug found in dogfooding.
            return {"success": True, "data": {"text": self._draft}}
        return {}


def _mark_session_idle(sessions: SessionTracker, task_id: int) -> None:
    sessions.recovering(task_id, "main")
    sessions.session_recovered(task_id, "main")


async def _fire_idle(hub: EventHub, task_id: int) -> None:
    await asyncio.sleep(0.01)
    hub.publish(
        "status_changed",
        {
            "task_id": task_id,
            "session": "main",
            "from": "working",
            "to": "idle",
            "reason": "test",
        },
    )


async def test_draft_via_agent_publishes_lifecycle(
    tmp_root, engine, ships, agents, sessions, hub
):
    _project, task = _make_project_and_task(engine, tmp_root)
    draft_text = textwrap.dedent(
        """\
        <<<COMMIT_MESSAGE>>>
        Commit msg

        <<<PR_TITLE>>>
        PR title

        <<<PR_BODY>>>
        PR body
        """
    )
    handle = FakeAgentHandle(draft_text)
    agents._handles[(task.id, "main")] = handle
    _mark_session_idle(sessions, task.id)
    queue = hub.subscribe()

    asyncio.create_task(_fire_idle(hub, task.id))
    state = await ships.draft(task)

    events = []
    while len(events) < 3:
        event = await asyncio.wait_for(queue.get(), timeout=1.0)
        if event.type in {"ship_step", "ship_draft"}:
            events.append(event)
    hub.unsubscribe(queue)

    assert state.status == "drafted"
    assert state.draft is not None
    assert state.draft.commit_message == "Commit msg"
    assert state.last_step is not None
    assert (state.last_step.step, state.last_step.status) == ("draft", "ok")
    assert handle.prompt_count == 1
    assert [(event.type, event.payload.get("status")) for event in events] == [
        ("ship_step", "started"),
        ("ship_draft", None),
        ("ship_step", "ok"),
    ]
    assert events[1].payload["draft"]["pr_title"] == "PR title"


async def test_draft_without_live_agent_raises(tmp_root, engine, ships, monkeypatch):
    _project, task = _make_project_and_task(engine, tmp_root)
    with pytest.raises(NoLiveAgentError):
        await ships.draft(task)


async def test_draft_requires_idle_primary_session(
    tmp_root, engine, ships, agents, sessions
):
    _project, task = _make_project_and_task(engine, tmp_root)
    handle = FakeAgentHandle("unused")
    agents._handles[(task.id, "main")] = handle
    sessions.recovering(task.id, "main")

    with pytest.raises(SessionNotIdleError, match="not idle"):
        await ships.draft(task)
    assert handle.prompt_count == 0


async def test_concurrent_ensure_coalesces_and_replace_conflicts(
    tmp_root, engine, ships, agents, sessions, hub
):
    _project, task = _make_project_and_task(engine, tmp_root)
    started = asyncio.Event()
    release = asyncio.Event()
    handle = FakeAgentHandle(
        textwrap.dedent(
            """\
            <<<COMMIT_MESSAGE>>>
            Commit msg
            <<<PR_TITLE>>>
            PR title
            <<<PR_BODY>>>
            PR body
            """
        ),
        prompt_started=started,
        prompt_release=release,
    )
    agents._handles[(task.id, "main")] = handle
    _mark_session_idle(sessions, task.id)

    first = asyncio.create_task(ships.draft(task))
    await asyncio.wait_for(started.wait(), timeout=1.0)

    duplicate = await ships.draft(task)
    assert duplicate.status == "drafting"
    with pytest.raises(ShipInProgressError):
        await ships.draft(task, replace=True)
    assert handle.prompt_count == 1

    release.set()
    asyncio.create_task(_fire_idle(hub, task.id))
    completed = await asyncio.wait_for(first, timeout=1.0)
    assert completed.status == "drafted"
    assert handle.prompt_count == 1


async def test_existing_draft_is_idempotent_and_explicitly_replaceable(
    tmp_root, engine, ships, agents, sessions, hub
):
    _project, task = _make_project_and_task(engine, tmp_root)
    first_text = textwrap.dedent(
        """\
        <<<COMMIT_MESSAGE>>>
        First
        <<<PR_TITLE>>>
        First title
        <<<PR_BODY>>>
        First body
        """
    )
    handle = FakeAgentHandle(first_text)
    agents._handles[(task.id, "main")] = handle
    _mark_session_idle(sessions, task.id)

    asyncio.create_task(_fire_idle(hub, task.id))
    first = await ships.draft(task)
    assert (await ships.draft(task)) is first
    assert handle.prompt_count == 1

    handle._draft = first_text.replace("First", "Second")
    asyncio.create_task(_fire_idle(hub, task.id))
    replaced = await ships.draft(task, replace=True)
    assert replaced.draft is not None
    assert replaced.draft.commit_message == "Second"
    assert handle.prompt_count == 2


async def test_failed_replacement_preserves_previous_draft_and_is_retryable(
    tmp_root, engine, ships, agents, sessions, hub
):
    _project, task = _make_project_and_task(engine, tmp_root)
    handle = FakeAgentHandle(
        "<<<COMMIT_MESSAGE>>>\nFirst\n<<<PR_TITLE>>>\nTitle\n<<<PR_BODY>>>\nBody"
    )
    agents._handles[(task.id, "main")] = handle
    _mark_session_idle(sessions, task.id)

    asyncio.create_task(_fire_idle(hub, task.id))
    original = await ships.draft(task)
    assert original.draft is not None

    handle._draft = "missing markers"
    asyncio.create_task(_fire_idle(hub, task.id))
    failed = await ships.draft(task, replace=True)
    assert failed.status == "error"
    assert failed.draft is original.draft
    assert failed.last_step is not None
    assert failed.last_step.step == "draft"
    assert failed.last_step.status == "failed"
    assert "parse draft markers" in (failed.error or "")

    handle._draft = (
        "<<<COMMIT_MESSAGE>>>\nRetry\n<<<PR_TITLE>>>\nRetry title\n"
        "<<<PR_BODY>>>\nRetry body"
    )
    asyncio.create_task(_fire_idle(hub, task.id))
    retried = await ships.draft(task, replace=True)
    assert retried.status == "drafted"
    assert retried.error is None
    assert retried.draft is not None
    assert retried.draft.commit_message == "Retry"


@pytest.mark.parametrize(
    ("draft_text", "request_error", "expected"),
    [
        (None, None, "did not return text"),
        ("missing markers", None, "could not parse draft markers"),
        (None, RuntimeError("rpc broke"), "agent request failed: rpc broke"),
    ],
)
async def test_draft_failures_publish_retryable_error(
    tmp_root,
    engine,
    ships,
    agents,
    sessions,
    hub,
    draft_text,
    request_error,
    expected,
):
    _project, task = _make_project_and_task(engine, tmp_root)
    handle = FakeAgentHandle(draft_text, request_error=request_error)
    agents._handles[(task.id, "main")] = handle
    _mark_session_idle(sessions, task.id)
    queue = hub.subscribe()

    asyncio.create_task(_fire_idle(hub, task.id))
    state = await ships.draft(task)

    failed = None
    while failed is None:
        event = await asyncio.wait_for(queue.get(), timeout=1.0)
        if event.type == "ship_step" and event.payload.get("status") == "failed":
            failed = event
    hub.unsubscribe(queue)
    assert state.status == "error"
    assert expected in (state.error or "")
    assert expected in failed.payload["detail"]
    assert ships.snapshot()[task.id]["last_step"]["step"] == "draft"


async def test_draft_timeout_publishes_retryable_error(
    tmp_root, engine, ships, agents, sessions, hub, monkeypatch
):
    _project, task = _make_project_and_task(engine, tmp_root)
    agents._handles[(task.id, "main")] = FakeAgentHandle("unused")
    _mark_session_idle(sessions, task.id)

    async def timeout(*args, **kwargs):
        raise TimeoutError

    monkeypatch.setattr("ompire_daemon.ship.wait_for_idle", timeout)
    state = await ships.draft(task)
    assert state.status == "error"
    assert state.error == "timed out waiting for agent draft"
    assert state.last_step is not None
    assert state.last_step.status == "failed"


@pytest.mark.parametrize("published", ["archived", "pr", "shipped"])
async def test_explicit_draft_refuses_published_tasks(
    tmp_root, engine, ships, agents, sessions, published
):
    _project, task = _make_project_and_task(engine, tmp_root)
    handle = FakeAgentHandle("unused")
    agents._handles[(task.id, "main")] = handle
    _mark_session_idle(sessions, task.id)
    if published == "archived":
        task = mark_archived(engine, task.id)
    elif published == "pr":
        task = mark_pr_url(engine, task.id, "https://github.com/owner/repo/pull/1")
    else:
        ships._ships[task.id] = ShipState(status="shipped")

    with pytest.raises(ShipAlreadyPublishedError):
        await ships.draft(task, replace=True)
    assert handle.prompt_count == 0


# --- REST guards ----------------------------------------------------------


def test_draft_route_409_without_live_agent(client, auth_header, engine, tmp_root):
    _project, task = _make_project_and_task(engine, tmp_root)
    response = client.post(f"/api/tasks/{task.id}/ship/draft", headers=auth_header)
    assert response.status_code == 409


def test_draft_route_keeps_bodyless_ensure_and_explicit_replace(
    client, auth_header, engine, tmp_root, app, monkeypatch
):
    _project, task = _make_project_and_task(engine, tmp_root)
    replacements: list[bool] = []
    state = ShipState(
        status="drafted",
        draft=ShipDraft("Commit", "Title", "Body"),
    )

    async def draft(_task, *, replace: bool = False):
        replacements.append(replace)
        return state

    monkeypatch.setattr(app.state.ships, "draft", draft)

    ensured = client.post(f"/api/tasks/{task.id}/ship/draft", headers=auth_header)
    replaced = client.post(
        f"/api/tasks/{task.id}/ship/draft",
        headers=auth_header,
        json={"replace": True},
    )

    assert ensured.status_code == 200
    assert replaced.status_code == 200
    assert replacements == [False, True]


@pytest.mark.parametrize("mode", ["squash", "retain"])
def test_commit_route_preflight_409_precedes_seed_jobs_and_all_git_mutation(
    client, auth_header, engine, tmp_root, app, monkeypatch, mode
):
    project, task = _make_project_and_task(engine, tmp_root)
    origin = tmp_root / "route-preflight-origin.git"
    _setup_git_clone(origin, Path(task.clone_path))
    clone = Path(task.clone_path)
    original_head = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    original_tree = (clone / "file3.txt").read_text(encoding="utf-8")
    error = _blocked_preflight_error(project.upstream_url)

    async def unavailable(_task):
        raise error

    monkeypatch.setattr(app.state.ships, "preflight", unavailable)
    events = app.state.events.subscribe()
    try:
        response = client.post(
            f"/api/tasks/{task.id}/ship/commit",
            headers=auth_header,
            json={"message": "m", "pr_title": "t", "pr_body": "b", "mode": mode},
        )
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert detail["message"] == str(error)
        assert detail["gh"]["identity"]["state"] == "unauthenticated"
        assert (
            subprocess.run(
                ["git", "-C", str(clone), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            == original_head
        )
        assert (clone / "file3.txt").read_text(encoding="utf-8") == original_tree
        assert (
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(clone),
                    "rev-parse",
                    "--verify",
                    "refs/ompire/ship-orig",
                ],
                capture_output=True,
                text=True,
                check=False,
            ).returncode
            != 0
        )
        assert app.state.ships.get(task.id) is None
        assert app.state.spawn_jobs == set()
        assert events.empty()
    finally:
        app.state.events.unsubscribe(events)


def test_commit_preflight_refusal_redacts_rest_and_websocket(
    client, auth_header, engine, tmp_root, app, monkeypatch
):
    _project, task = _make_project_and_task(engine, tmp_root)
    secret = "ship-rest-exact-secret"
    _write_script(
        tmp_root / "bin",
        "gh",
        "#!/bin/sh\n"
        'case "$*" in\n'
        "'--version') echo 'gh version 2.97.0 (test)' ;;\n"
        "'api --hostname github.com user')\n"
        "  printf 'HTTP 401: Bad credentials\\nAuthorization: Token %s\\nghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789\\n' \"$GH_TOKEN\" >&2\n"
        "  exit 1 ;;\n"
        "*) echo unsupported >&2; exit 1 ;;\n"
        "esac\n",
    )
    monkeypatch.setenv("GH_TOKEN", secret)

    with client.websocket_connect(f"/api/ws?token={app.state.auth_token}") as ws:
        ws.receive_json()
        response = client.post(
            f"/api/tasks/{task.id}/ship/commit",
            headers=auth_header,
            json={"message": "m", "pr_title": "t", "pr_body": "b"},
        )
        event = ws.receive_json()

    published = f"{response.text}\n{event}"
    assert response.status_code == 409
    assert response.json()["detail"]["gh"]["identity"]["state"] == "unauthenticated"
    assert event["type"] == "gh_status"
    for credential in (
        secret,
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        "Authorization: Token",
    ):
        assert credential not in published
    assert app.state.ships.get(task.id) is None


def test_commit_route_409_gpg_not_ready(
    client, auth_header, engine, tmp_root, app, monkeypatch
):
    _project, task = _make_project_and_task(engine, tmp_root)
    monkeypatch.setattr(app.state.gpg, "probe", _blocked_gpg_probe())
    response = client.post(
        f"/api/tasks/{task.id}/ship/commit",
        headers=auth_header,
        json={
            "message": "m",
            "pr_title": "t",
            "pr_body": "b",
        },
    )
    assert response.status_code == 409
    data = response.json()
    assert data["detail"]["gpg"]["state"] == "locked"


@pytest.mark.parametrize(
    "state",
    ["locked", "ambiguous", "no_key", "missing", "agent_unavailable", "unknown", "error"],
)
def test_commit_route_refuses_every_state_but_ready_without_touching_the_clone(
    client, auth_header, engine, tmp_root, app, monkeypatch, state
):
    """Fail closed, and prove the refusal happened before any Git mutation."""
    origin = tmp_root / "origin.git"
    _project, task = _make_project_and_task(engine, tmp_root)
    _setup_git_clone(origin, Path(task.clone_path))
    monkeypatch.setattr(app.state.gpg, "probe", _blocked_gpg_probe(state))

    before = subprocess.run(
        ["git", "-C", task.clone_path, "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    response = client.post(
        f"/api/tasks/{task.id}/ship/commit",
        headers=auth_header,
        json={"message": "m", "pr_title": "t", "pr_body": "b"},
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["gpg"]["state"] == state
    # The message names the actual condition, not a generic "not cached".
    assert detail["message"] and "not cached" not in detail["message"]

    after = subprocess.run(
        ["git", "-C", task.clone_path, "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert after == before
    orig_ref = subprocess.run(
        ["git", "-C", task.clone_path, "rev-parse", "--verify", "refs/ompire/ship-orig"],
        capture_output=True, text=True, check=False,
    )
    assert orig_ref.returncode != 0


def test_commit_route_409_unsupported_mode(
    client, auth_header, engine, tmp_root, app, monkeypatch
):
    _project, task = _make_project_and_task(engine, tmp_root)
    monkeypatch.setattr(app.state.gpg, "probe", _ready_gpg_probe())
    response = client.post(
        f"/api/tasks/{task.id}/ship/commit",
        headers=auth_header,
        json={
            "message": "m",
            "pr_title": "t",
            "pr_body": "b",
            "mode": "merge",
        },
    )
    assert response.status_code == 409


def test_commit_route_409_retain_dirty_tree(
    client, auth_header, engine, tmp_root, app, monkeypatch
):
    _project, task = _make_project_and_task(engine, tmp_root)
    monkeypatch.setattr(app.state.gpg, "probe", _ready_gpg_probe())
    origin = tmp_root / "origin.git"
    _setup_git_clone(origin, Path(task.clone_path))

    response = client.post(
        f"/api/tasks/{task.id}/ship/commit",
        headers=auth_header,
        json={
            "message": "m",
            "pr_title": "t",
            "pr_body": "b",
            "mode": "retain",
        },
    )
    assert response.status_code == 409
    assert "dirty" in response.json()["detail"].lower()


def test_commit_route_409_retain_merge_commits(
    client, auth_header, engine, tmp_root, app, monkeypatch
):
    _project, task = _make_project_and_task(engine, tmp_root)
    monkeypatch.setattr(app.state.gpg, "probe", _ready_gpg_probe())
    origin = tmp_root / "origin.git"
    _setup_git_clone(origin, Path(task.clone_path))
    _run_git(Path(task.clone_path), "add", "file3.txt")
    _run_git(Path(task.clone_path), "commit", "-m", "stage working edit")

    clone = Path(task.clone_path)
    _run_git(clone, "checkout", "-b", "side")
    (clone / "side.txt").write_text("side\n", encoding="utf-8")
    _run_git(clone, "add", ".")
    _run_git(clone, "commit", "-m", "side commit")
    _run_git(clone, "checkout", "ompire/task-1")
    _run_git(clone, "merge", "--no-ff", "side", "-m", "merge side")

    response = client.post(
        f"/api/tasks/{task.id}/ship/commit",
        headers=auth_header,
        json={
            "message": "m",
            "pr_title": "t",
            "pr_body": "b",
            "mode": "retain",
        },
    )
    assert response.status_code == 409
    assert "merge" in response.json()["detail"].lower()


def test_commit_route_409_retain_empty_range(
    client, auth_header, engine, tmp_root, app, monkeypatch
):
    _project, task = _make_project_and_task(engine, tmp_root)
    monkeypatch.setattr(app.state.gpg, "probe", _ready_gpg_probe())
    origin = tmp_root / "origin.git"
    _setup_git_clone(origin, Path(task.clone_path))
    (Path(task.clone_path) / "file3.txt").unlink()
    _run_git(Path(task.clone_path), "checkout", "main")
    _run_git(Path(task.clone_path), "checkout", "-B", "ompire/task-1")

    response = client.post(
        f"/api/tasks/{task.id}/ship/commit",
        headers=auth_header,
        json={
            "message": "m",
            "pr_title": "t",
            "pr_body": "b",
            "mode": "retain",
        },
    )
    assert response.status_code == 409
    assert "no commits" in response.json()["detail"].lower()


def test_commit_route_accepts_retain_mode(
    client, auth_header, engine, tmp_root, app, monkeypatch
):
    _project, task = _make_project_and_task(engine, tmp_root)
    monkeypatch.setattr(app.state.gpg, "probe", _ready_gpg_probe())
    origin = tmp_root / "origin.git"
    _setup_git_clone(origin, Path(task.clone_path))
    _run_git(Path(task.clone_path), "add", "file3.txt")
    _run_git(Path(task.clone_path), "commit", "-m", "stage working edit")

    response = client.post(
        f"/api/tasks/{task.id}/ship/commit",
        headers=auth_header,
        json={
            "message": "m",
            "pr_title": "t",
            "pr_body": "b",
            "mode": "retain",
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "committing"
    assert data["mode"] == "retain"


def test_commit_route_409_when_ship_in_flight(
    client, auth_header, engine, tmp_root, app, monkeypatch
):
    _project, task = _make_project_and_task(engine, tmp_root)
    monkeypatch.setattr(app.state.gpg, "probe", _ready_gpg_probe())
    app.state.ships.seed_commit(task.id)
    response = client.post(
        f"/api/tasks/{task.id}/ship/commit",
        headers=auth_header,
        json={
            "message": "m",
            "pr_title": "t",
            "pr_body": "b",
        },
    )
    assert response.status_code == 409


def test_snapshot_carries_ships_and_gpg(client, auth_header, engine, tmp_root, app):
    _project, task = _make_project_and_task(engine, tmp_root)
    app.state.ships._set_state(
        task.id,
        status="drafted",
        draft=ShipDraft("msg", "title", "body"),
    )
    with client.websocket_connect(f"/api/ws?token={app.state.auth_token}") as ws:
        message = ws.receive_json()

    assert message["type"] == "snapshot"
    assert "ships" in message["payload"]
    assert str(task.id) in message["payload"]["ships"]
    assert "gpg" in message["payload"]
    assert message["payload"]["gpg"]["state"] in (
        "ready",
        "locked",
        "ambiguous",
        "no_key",
        "missing",
        "agent_unavailable",
        "unknown",
        "error",
    )
    assert "candidates" in message["payload"]["gpg"]


# --- cleanup/purge hooks --------------------------------------------------


async def test_cleanup_calls_cancel_and_drop(
    tmp_root, engine, client, auth_header, app, monkeypatch
):
    _project, task = _make_project_and_task(engine, tmp_root)
    dropped: list[int] = []

    async def fake_cancel_and_drop(tid: int) -> None:
        dropped.append(tid)

    monkeypatch.setattr(app.state.ships, "cancel_and_drop", fake_cancel_and_drop)

    # Cleanup needs a directory inside task root so the path check passes.
    clone_dir = Path(task.clone_path)
    clone_dir.mkdir(parents=True, exist_ok=True)
    (clone_dir / ".git").mkdir(exist_ok=True)

    response = client.post(f"/api/tasks/{task.id}/cleanup", headers=auth_header)
    assert response.status_code == 200
    assert dropped == [task.id]


async def test_purge_calls_drop_ship(
    tmp_root, engine, client, auth_header, app, monkeypatch
):
    _project, task = _make_project_and_task(engine, tmp_root)
    mark_archived(engine, task.id)
    dropped: list[int] = []
    monkeypatch.setattr(app.state.ships, "drop_ship", lambda tid: dropped.append(tid))

    response = client.delete(f"/api/tasks/{task.id}", headers=auth_header)
    assert response.status_code == 200
    assert dropped == [task.id]


def test_gpg_get_returns_current_status(client, auth_header, app, monkeypatch):
    status = GpgStatus(state="ready", selected=_selection(_TEST_FINGERPRINT))
    monkeypatch.setattr(app.state.gpg, "current", lambda: status)

    response = client.get("/api/gpg", headers=auth_header)
    assert response.status_code == 200
    data = response.json()
    assert data["state"] == "ready"
    assert data["selected"]["fingerprint"] == _TEST_FINGERPRINT


def test_gpg_recheck_returns_probed_status(client, auth_header, app, monkeypatch):
    status = GpgStatus(
        state="locked", selected=_selection(_TEST_FINGERPRINT), detail="cold cache"
    )

    async def fake_probe() -> GpgStatus:
        return status

    monkeypatch.setattr(app.state.gpg, "probe", fake_probe)

    response = client.post("/api/gpg/recheck", headers=auth_header)
    assert response.status_code == 200
    data = response.json()
    assert data["state"] == "locked"
    assert data["selected"]["fingerprint"] == _TEST_FINGERPRINT
