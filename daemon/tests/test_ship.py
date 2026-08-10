"""Tests for `ompire_daemon.ship`."""

from __future__ import annotations

import asyncio
import os
import subprocess
import textwrap
from dataclasses import asdict
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ompire_daemon.agent import AgentSupervisor
from ompire_daemon.app import create_app
from ompire_daemon.config import Config
from ompire_daemon.db import db_path_for, make_engine
from ompire_daemon.events import EventHub
from ompire_daemon.gpg import GpgProbe, GpgStatus
from ompire_daemon.registry.projects import create_project
from ompire_daemon.registry.tasks import (
    create_task,
    get_task,
    mark_archived,
    mark_pr_url,
)
from ompire_daemon.sessions import SessionTracker
from ompire_daemon.ship import (
    ShipDraft,
    ShipManager,
    NoLiveAgentError,
    parse_github_owner,
    parse_github_slug,
    _find_pr_url,
    _parse_draft,
)


@pytest.fixture
def tmp_root(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def config(tmp_root: Path) -> Config:
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


@pytest.fixture
def ships(
    config: Config,
    engine,
    hub: EventHub,
    sessions: SessionTracker,
    agents: AgentSupervisor,
    gpg: GpgProbe,
) -> ShipManager:
    return ShipManager(config, engine, hub, sessions, agents, gpg)


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


def _setup_signing_gpg(clone_path: Path, bin_dir: Path) -> Path:
    """Generate a throwaway GPG key and return a wrapper script that uses it."""
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
            Subkey-Type: RSA
            Subkey-Length: 2048
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
        f"#!/bin/sh\nexport GNUPGHOME={gnupg_home}\nexec {gpg_bin} \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    _run_git(clone_path, "config", "gpg.program", str(wrapper))
    _run_git(clone_path, "config", "user.signingkey", "test@example.com")
    return wrapper


def _setup_git_clone(
    origin_path: Path,
    clone_path: Path,
    fake_gpg: Path | None = None,
) -> None:
    """Create a bare `origin`, clone it, put a base branch and a task branch
    with multiple commits plus an uncommitted working-tree edit.
    """
    origin_path.mkdir(parents=True, exist_ok=True)
    _run_git(origin_path, "init", "--bare")
    clone_path.parent.mkdir(parents=True, exist_ok=True)
    _run_git(clone_path.parent, "clone", str(origin_path), clone_path.name)
    _run_git(clone_path, "config", "user.email", "test@example.com")
    _run_git(clone_path, "config", "user.name", "Test User")
    if fake_gpg is not None:
        _run_git(clone_path, "config", "gpg.program", str(fake_gpg))
        _run_git(clone_path, "config", "user.signingkey", "test@example.com")

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


def _cached_gpg_probe():
    async def probe():
        return GpgStatus(
            state="cached",
            key="test@example.com",
            keygrip="8C9301DF2FFD432192448A04C8F2A6BA372A1830",
            detail=None,
        )

    return probe


def _locked_gpg_probe():
    async def probe():
        return GpgStatus(
            state="locked",
            key="test@example.com",
            keygrip="8C9301DF2FFD432192448A04C8F2A6BA372A1830",
            detail=None,
        )

    return probe


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
        base_branch="main",
        default_branch_pattern="ompire/<slug>",
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
    monkeypatch.setattr(gpg, "probe", _cached_gpg_probe())

    origin = tmp_root / "origin.git"
    project, task = _make_project_and_task(engine, tmp_root)

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
    monkeypatch.setenv(
        "PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    )

    _setup_git_clone(origin, Path(task.clone_path), fake_gpg=None)
    _setup_signing_gpg(Path(task.clone_path), bin_dir)

    state = await ships.commit_and_ship(
        task,
        message="Final squash commit",
        pr_title="Final PR",
        pr_body="Body text",
    )

    assert state.status == "shipped", state.error
    assert state.commit_sha is not None
    assert state.pr_url == "https://github.com/owner/repo/pull/42"

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


async def test_commit_failure_restores_head_and_cleans_ref(
    tmp_root, engine, ships, gpg, monkeypatch
):
    monkeypatch.setattr(gpg, "probe", _cached_gpg_probe())

    origin = tmp_root / "origin.git"
    project, task = _make_project_and_task(engine, tmp_root)

    # Fake gpg that fails signing.
    tmp_root.joinpath("bin").mkdir(exist_ok=True)
    fake_gpg = _write_script(
        tmp_root / "bin",
        "fake-gpg-fail",
        "#!/bin/sh\necho 'signing failed' >&2\nexit 2\n",
    )
    monkeypatch.setenv(
        "PATH", f"{tmp_root / 'bin'}{os.pathsep}{os.environ['PATH']}"
    )

    _setup_git_clone(origin, Path(task.clone_path), fake_gpg=None)
    _run_git(Path(task.clone_path), "config", "gpg.program", str(fake_gpg))

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
        ["git", "-C", task.clone_path, "rev-parse", "--verify", "refs/ompire/ship-orig"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0


# --- push routing ---------------------------------------------------------


async def test_push_and_pr_routes_to_fork(
    tmp_root, engine, ships, gpg, monkeypatch
):
    monkeypatch.setattr(gpg, "probe", _cached_gpg_probe())

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

    async def fake_create_pr(
        clone_path, upstream_slug, base_branch, head, title, body
    ):
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
        task, project, pr_title="Title", pr_body="Body"
    )

    assert url == "https://github.com/upowner/uprepo/pull/7"
    assert pushed == [("git@github.com:forkowner/uprepo.git", "ompire/task-1")]
    assert created["upstream"] == "upowner/uprepo"
    assert created["head"] == "forkowner:ompire/task-1"
    assert created["base"] == "main"


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
    monkeypatch.setenv(
        "PATH", f"{tmp_root / 'bin'}{os.pathsep}{os.environ['PATH']}"
    )

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

    def __init__(self, draft_text: str):
        self._draft = draft_text

    async def prompt(self, message: str) -> dict:
        return {"ok": True}

    async def request(self, request_type: str, **fields) -> dict:
        if request_type == "get_last_assistant_text":
            return {"data": self._draft}
        return {}


async def test_draft_via_agent_publishes_draft(
    tmp_root, engine, ships, agents, hub, monkeypatch
):
    project, task = _make_project_and_task(engine, tmp_root)
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
    agents._handles[task.id] = FakeAgentHandle(draft_text)

    queue = hub.subscribe()

    async def _next_event_of(type_name: str):
        while True:
            event = await asyncio.wait_for(queue.get(), timeout=1.0)
            if event.type == type_name:
                return event

    async def fire_idle():
        await asyncio.sleep(0.05)
        hub.publish(
            "status_changed",
            {
                "task_id": task.id,
                "from": "working",
                "to": "idle",
                "reason": "test",
            },
        )

    asyncio.create_task(fire_idle())
    state = await ships.draft(task)

    hub.unsubscribe(queue)
    assert state.status == "drafted"
    assert state.draft.commit_message == "Commit msg"

    event = await _next_event_of("ship_draft")
    assert event.payload["draft"]["pr_title"] == "PR title"


async def test_draft_without_live_agent_raises(
    tmp_root, engine, ships, monkeypatch
):
    project, task = _make_project_and_task(engine, tmp_root)
    with pytest.raises(NoLiveAgentError):
        await ships.draft(task)


# --- REST guards ----------------------------------------------------------


def test_draft_route_409_without_live_agent(client, auth_header, engine, tmp_root):
    project, task = _make_project_and_task(engine, tmp_root)
    response = client.post(
        f"/api/tasks/{task.id}/ship/draft", headers=auth_header
    )
    assert response.status_code == 409


def test_commit_route_409_gpg_locked(client, auth_header, engine, tmp_root, app, monkeypatch):
    project, task = _make_project_and_task(engine, tmp_root)
    monkeypatch.setattr(app.state.gpg, "probe", _locked_gpg_probe())
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


def test_commit_route_409_non_squash_mode(
    client, auth_header, engine, tmp_root, app, monkeypatch
):
    project, task = _make_project_and_task(engine, tmp_root)
    monkeypatch.setattr(app.state.gpg, "probe", _cached_gpg_probe())
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


def test_commit_route_409_when_ship_in_flight(
    client, auth_header, engine, tmp_root, app, monkeypatch
):
    project, task = _make_project_and_task(engine, tmp_root)
    monkeypatch.setattr(app.state.gpg, "probe", _cached_gpg_probe())
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
    project, task = _make_project_and_task(engine, tmp_root)
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
    assert message["payload"]["gpg"]["state"] in ("cached", "locked", "unknown")


# --- cleanup/purge hooks --------------------------------------------------


async def test_cleanup_calls_cancel_and_drop(
    tmp_root, engine, client, auth_header, app, monkeypatch
):
    project, task = _make_project_and_task(engine, tmp_root)
    dropped: list[int] = []
    async def fake_cancel_and_drop(tid: int) -> None:
        dropped.append(tid)

    monkeypatch.setattr(app.state.ships, "cancel_and_drop", fake_cancel_and_drop)

    # Cleanup needs a directory inside task root so the path check passes.
    clone_dir = Path(task.clone_path)
    clone_dir.mkdir(parents=True, exist_ok=True)
    (clone_dir / ".git").mkdir(exist_ok=True)

    response = client.post(
        f"/api/tasks/{task.id}/cleanup", headers=auth_header
    )
    assert response.status_code == 200
    assert dropped == [task.id]


async def test_purge_calls_drop_ship(
    tmp_root, engine, client, auth_header, app, monkeypatch
):
    project, task = _make_project_and_task(engine, tmp_root)
    archived = mark_archived(engine, task.id)
    dropped: list[int] = []
    monkeypatch.setattr(app.state.ships, "drop_ship", lambda tid: dropped.append(tid))

    response = client.delete(f"/api/tasks/{task.id}", headers=auth_header)
    assert response.status_code == 200
    assert dropped == [task.id]


def test_gpg_get_returns_current_status(client, auth_header, app, monkeypatch):
    status = GpgStatus(state="cached", key="test-key", keygrip="AB", detail=None)
    monkeypatch.setattr(app.state.gpg, "current", lambda: status)

    response = client.get("/api/gpg", headers=auth_header)
    assert response.status_code == 200
    data = response.json()
    assert data["state"] == "cached"
    assert data["key"] == "test-key"


def test_gpg_recheck_returns_probed_status(client, auth_header, app, monkeypatch):
    status = GpgStatus(state="locked", key="test-key", keygrip="AB", detail="locked")

    async def fake_probe() -> GpgStatus:
        return status

    monkeypatch.setattr(app.state.gpg, "probe", fake_probe)

    response = client.post("/api/gpg/recheck", headers=auth_header)
    assert response.status_code == 200
    data = response.json()
    assert data["state"] == "locked"
    assert data["key"] == "test-key"
