"""Tests for `ompire_daemon.ship`: content-bound, selectable, recoverable
delivery (ADR-0032).

Everything here runs against real disposable Git repositories and a real
throwaway GPG key. What is faked is only what sits outside the trust boundary:
the GitHub CLI and the forge it talks to.
"""

from __future__ import annotations

import asyncio
import json
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
from ompire_daemon.delivery import (
    DeliveryWorkspaceError,
    ProtectedPathError,
    WorkspaceGuard,
    capture_candidate,
    compute_candidate_id,
)
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
from ompire_daemon.registry.reviews import clear_process_marker
from ompire_daemon.registry.ships import (
    get_delivery,
    get_latest_delivery,
    list_deliveries,
)
from ompire_daemon.registry.tasks import (
    create_task,
    get_task,
    mark_pr_url,
)
from ompire_daemon.registry.workflows import (
    WorkflowGateChoiceError,
    WorkflowWaitConflictError,
    list_step_records,
)
from ompire_daemon.sessions import SessionTracker
from ompire_daemon.ship import (
    DeliveryBlockedError,
    GitHubPreflightError,
    PreviewMismatchError,
    PushError,
    ShipDraft,
    ShipManager,
    SshAuthenticationError,
    _find_pr_url,
    _parse_draft,
    body_with_marker,
    correlation_marker,
)
from ompire_daemon.taskdefinition import resolve_task_definition
from ompire_daemon.workflows import WorkflowNotWaitingError, WorkflowRunner
from tests.conftest import (
    install_delivery_workflow,
    make_execution_inputs,
    park_at_delivery_gate,
    register_builtin_workflows,
)


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
    register_builtin_workflows(eng)
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
def guard() -> WorkspaceGuard:
    return WorkspaceGuard()


@pytest.fixture
def ships(
    config: Config,
    engine,
    hub: EventHub,
    sessions: SessionTracker,
    agents: AgentSupervisor,
    gpg: GpgProbe,
    gh: _AllowedGitHub,
    guard: WorkspaceGuard,
) -> ShipManager:
    return ShipManager(config, engine, hub, sessions, agents, gpg, gh, guard)


@pytest.fixture
def runner(config: Config, engine, hub: EventHub, sessions: SessionTracker):
    return _StubRunner(engine, config, hub, sessions)


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
    ending: str = "pr",
    mode: str = "squash",
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
    # The task is pinned to a workflow that actually declares the delivery
    # under test. Nothing else can authorize one: publication is a step an
    # author wrote, not something a task acquires by being finished.
    revision = install_delivery_workflow(engine, ending=ending, mode=mode)
    task = create_task(
        engine,
        project_name="myproject",
        slug="task-1",
        branch="ompire/task-1",
        clone_path=str(clone_path),
        prompt="do the thing",
        workflow_name=revision.name,
        execution_inputs=make_execution_inputs(
            checkout_path=str(checkout_dir),
            project_name="myproject",
            branch="ompire/task-1",
            upstream_url=upstream_url,
            fork_url=fork_url,
            workflow_name=revision.name,
            revision=revision,
        ),
    )
    return project, task



# --- shared delivery helpers ------------------------------------------------


async def _approve_current(config, engine, task, base_branch: str = "main"):
    """Capture the task's current content and reach its approval with it.

    Exactly what a real run leaves behind: the candidate the review graded, a
    terminal `approved` iteration naming that candidate, and a parked question
    whose frozen evidence names that review attempt. Delivery reads the
    binding, never the status alone — and never a question nobody is at.
    """
    candidate = await capture_candidate(
        config, engine, task, base_branch=base_branch
    )
    park_at_delivery_gate(engine, task, candidate_id=candidate.candidate_id)
    clear_process_marker(engine, task.id)
    return candidate


def _repin_workflow(engine, task, workflow_name: str) -> None:
    """Repin a task to a different workflow, as an older launch would have.

    Used to build the compatibility case: a task whose accepted procedure has
    no publication vocabulary at all.
    """
    from ompire_daemon.execution_inputs import encode_execution_inputs
    from ompire_daemon.registry.tasks import _update
    from tests.conftest import install_plain_workflow

    if workflow_name == "plain":
        install_plain_workflow(engine)
    inputs = make_execution_inputs(
        engine=engine,
        checkout_path=str(Path(task.clone_path).parent),
        project_name="myproject",
        branch=task.branch,
        workflow_name=workflow_name,
    )
    _update(
        engine,
        task.id,
        execution_inputs_json=encode_execution_inputs(inputs),
        workflow_name=workflow_name,
    )


def _reset_run(engine, task) -> None:
    """Clear the run's records so a test can reach its approval differently."""
    from ompire_daemon.registry.reviews import delete_review
    from ompire_daemon.registry.workflows import delete_step_records

    delete_step_records(engine, task.id)
    delete_review(engine, task.id)


def _gate(engine, task) -> dict:
    """The pending approval's identity, as preview keyword arguments.

    Empty when the run is not at one, so a test that expects a refusal passes
    exactly what a caller with nothing to name would.
    """
    from ompire_daemon.runauthority import resolve_authority

    authority = resolve_authority(engine, get_task(engine, task.id))
    if authority.approval is None:
        return {}
    return {"gate_seq": authority.approval.seq, "choice_id": "publish"}


def _local_destination(ships: ShipManager, remote: Path, monkeypatch) -> None:
    """Point the accepted destination at a local bare repository.

    The GitHub identity and eligibility preflight still runs against the
    project's real upstream URL; only the Git transport target is local, so a
    push can be observed as an actual ref write.
    """
    original = ships._destination

    def local(task):
        return {**original(task), "remote_url": str(remote)}

    monkeypatch.setattr(ships, "_destination", local)


def _bare(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _run_git(path, "init", "--bare")
    return path


def _git_out(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


class _StubRunner:
    """The real gate transaction, without the loop that would then run it.

    `answer_gate` is what commits the decision, the grant, and the run's move
    to its first action together, and it is the real method here — including
    its check that the confirmed chain is the one the choice declares. What is
    held back is only the scheduling: this file exercises the trusted
    operations themselves, and drives them explicitly so a failure names the
    operation rather than the loop.
    """

    def __init__(self, engine, config, hub, sessions) -> None:
        self._runner = WorkflowRunner(
            engine, config, hub, AgentSupervisor(config, hub, sessions), sessions
        )

    def answer_gate(self, task, revision, **kwargs):
        loop = asyncio.get_running_loop()
        # A pending wait is what a parked run has; with one present the answer
        # is committed and handed to that run instead of starting a new one.
        self._runner._gate_waits[task.id] = loop.create_future()
        try:
            return self._runner.answer_gate(task, revision, **kwargs)
        finally:
            self._runner._gate_waits.pop(task.id, None)


async def _deliver(
    ships: ShipManager,
    task,
    *,
    engine,
    runner,
    message: str = "ship: the work",
    pr_title: str = "The work",
    pr_body: str = "why",
    request_id: str = "req-1",
):
    """Preview the decision this run is at, then confirm exactly that.

    No ending is requested: the pinned chain decides how far the delivery
    goes, and asking for a different one is a refusal rather than an override.
    """
    preview = await ships.preview(
        task,
        **_gate(engine, task),
        commit_message=message,
        pr_title=pr_title,
        pr_body=pr_body,
        request_id=request_id,
    )
    assert preview.deliverable, [b.code for b in preview.blockers]
    projection = await ships.deliver(
        task,
        **_gate(engine, task),
        commit_message=message,
        pr_title=pr_title,
        pr_body=pr_body,
        request_id=request_id,
        preview_token=preview.fingerprint,
        runner=runner,
    )
    # The run would perform its authorized actions as steps; here they are
    # driven explicitly, against the delivery the confirmation just recorded.
    delivery = get_latest_delivery(engine, task.id)
    assert delivery is not None
    await ships._run_prefix(task, delivery.id, request_id)
    return ships.projection(get_task(engine, task.id)) or projection


# --- parsing (unchanged contracts) ------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/owner/repo", "owner/repo"),
        ("https://github.com/owner/repo.git", "owner/repo"),
        ("git@github.com:owner/repo.git", "owner/repo"),
    ],
)
def test_parse_github_slug(url, expected):
    assert parse_github_slug(url) == expected


@pytest.mark.parametrize(
    "url",
    ["https://gitlab.com/owner/repo", "https://github.com/owner", "not-a-url"],
)
def test_parse_github_slug_rejects_non_github(url):
    with pytest.raises(ValueError):
        parse_github_slug(url)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/fork-owner/repo", "fork-owner"),
        ("git@github.com:fork-owner/repo.git", "fork-owner"),
    ],
)
def test_parse_github_owner(url, expected):
    assert parse_github_owner(url) == expected


def test_parse_draft_extracts_sections():
    text = (
        "preamble\n"
        "<<<COMMIT_MESSAGE>>>\nfeat: thing\n\nbody line\n"
        "<<<PR_TITLE>>>\nAdd thing\n"
        "<<<PR_BODY>>>\n- did the thing\n"
    )
    draft = _parse_draft(text)
    assert draft == ShipDraft(
        commit_message="feat: thing\n\nbody line",
        pr_title="Add thing",
        pr_body="- did the thing",
    )


def test_parse_draft_returns_none_on_missing_marker():
    assert _parse_draft("<<<COMMIT_MESSAGE>>>\nonly one section\n") is None


def test_find_pr_url_extracts_the_first_pull_request_url():
    assert (
        _find_pr_url("noise\nhttps://github.com/o/r/pull/7\nmore")
        == "https://github.com/o/r/pull/7"
    )


def test_the_authorized_pr_body_carries_the_correlation_marker():
    """The preview shows the body Ompire will write, marker included, so the
    body the operator authorizes is the body a lost response can be found by."""
    marker = correlation_marker(7, "req-abc")
    body = body_with_marker("why this change", marker)
    assert "why this change" in body
    assert f"ompire-delivery: {marker}" in body
    # Deterministic from task and request identity alone.
    assert correlation_marker(7, "req-abc") == marker
    assert correlation_marker(8, "req-abc") != marker


# --- candidate identity -----------------------------------------------------


async def test_candidate_identity_is_content_and_survives_recapture(
    tmp_root, engine, config
):
    """An unchanged workspace captures to the same candidate; any change to
    what would be published captures to a different one."""
    _project, task = _make_project_and_task(engine, tmp_root)
    _setup_git_clone(tmp_root / "identity-origin.git", Path(task.clone_path))
    clone = Path(task.clone_path)

    first = await capture_candidate(config, engine, task, base_branch="main")
    again = await capture_candidate(config, engine, task, base_branch="main")
    assert again.candidate_id == first.candidate_id

    # An untracked file an agent leaves behind is part of what would be
    # published, so it changes the identity.
    (clone / "sneaky.txt").write_text("added later\n", encoding="utf-8")
    changed = await capture_candidate(config, engine, task, base_branch="main")
    assert changed.candidate_id != first.candidate_id
    assert changed.tree_id != first.tree_id


async def test_capture_leaves_the_task_index_and_worktree_untouched(
    tmp_root, engine, config
):
    _project, task = _make_project_and_task(engine, tmp_root)
    _setup_git_clone(tmp_root / "untouched-origin.git", Path(task.clone_path))
    clone = Path(task.clone_path)
    # Something deliberately staged, which capture must not disturb.
    (clone / "staged.txt").write_text("staged\n", encoding="utf-8")
    _run_git(clone, "add", "staged.txt")

    before_head = _git_out(clone, "rev-parse", "HEAD")
    before_status = _git_out(clone, "status", "--porcelain")

    await capture_candidate(config, engine, task, base_branch="main")

    assert _git_out(clone, "rev-parse", "HEAD") == before_head
    assert _git_out(clone, "status", "--porcelain") == before_status


# --- admission --------------------------------------------------------------


async def test_a_workflow_without_delivery_steps_cannot_publish_at_all(
    tmp_root, engine, ships, config
):
    """The compatibility rule, at the service boundary.

    A task pinned to a definition that never declared publication does not
    acquire it by finishing, by being reviewed, or by an operator asking
    firmly. There is no ending to name, so there is nothing to preview.
    """
    _project, task = _make_project_and_task(engine, tmp_root, ending="pr")
    _setup_git_clone(tmp_root / "legacyfmt-origin.git", Path(task.clone_path))
    _repin_workflow(engine, task, "plain")
    await _approve_current(config, engine, task)

    with pytest.raises(DeliveryBlockedError) as excinfo:
        await ships.preview(
            task,
            commit_message="m",
            pr_title="t",
            pr_body="b",
            request_id="r1",
        )
    assert [b.code for b in excinfo.value.blockers] == ["no-delivery-vocabulary"]


async def test_delivery_requires_an_approval_bound_to_the_current_content(
    tmp_root, engine, ships, config, monkeypatch
):
    _project, task = _make_project_and_task(engine, tmp_root, ending="commit")
    _setup_git_clone(tmp_root / "review-origin.git", Path(task.clone_path))
    clone = Path(task.clone_path)
    monkeypatch.setattr(ships._gpg, "probe", _ready_gpg_probe())

    # The run reached its approval, but the review it is bound to did not
    # approve. Reaching the question is not the same as passing review.
    candidate = await capture_candidate(config, engine, task, base_branch="main")
    park_at_delivery_gate(
        engine,
        task,
        candidate_id=candidate.candidate_id,
        outcome="comments",
        findings="> fix it",
    )
    unreviewed = await ships.preview(
        task,
        **_gate(engine, task),
        commit_message="m",
        pr_title="",
        pr_body="",
        request_id="r1",
    )
    assert "review-missing" in [b.code for b in unreviewed.blockers]

    _reset_run(engine, task)
    await _approve_current(config, engine, task)
    ready = await ships.preview(
        task,
        **_gate(engine, task),
        commit_message="m",
        pr_title="",
        pr_body="",
        request_id="r1",
    )
    assert ready.deliverable, [b.code for b in ready.blockers]

    # The agent keeps working. The approval survives as history and stops
    # covering current work.
    (clone / "after-approval.txt").write_text("new\n", encoding="utf-8")
    stale = await ships.preview(
        task,
        **_gate(engine, task),
        commit_message="m",
        pr_title="",
        pr_body="",
        request_id="r1",
    )
    assert "review-stale" in [b.code for b in stale.blockers]
    assert stale.review["status"] == "approved"
    assert stale.review["stale"] is True


async def test_a_legacy_unbound_approval_is_history_not_authorization(
    tmp_root, engine, ships
):
    """An approval recorded before content binding cannot deliver, and is not
    backfilled from today's workspace."""
    _project, task = _make_project_and_task(engine, tmp_root, ending="commit")
    _setup_git_clone(tmp_root / "legacy-origin.git", Path(task.clone_path))
    park_at_delivery_gate(engine, task, candidate_id=None)
    clear_process_marker(engine, task.id)

    preview = await ships.preview(
        task,
        **_gate(engine, task),
        commit_message="m",
        pr_title="",
        pr_body="",
        request_id="r1",
    )
    assert "review-unbound" in [b.code for b in preview.blockers]
    assert preview.review["content_bound"] is False


async def test_a_blocked_preview_cannot_be_confirmed(
    tmp_root, engine, ships, config, runner
):
    _project, task = _make_project_and_task(engine, tmp_root, ending="commit")
    _setup_git_clone(tmp_root / "blocked-origin.git", Path(task.clone_path))
    # At the approval, with an unapproved review behind it: deliverable is
    # false, and confirming anyway is refused rather than authorized.
    candidate = await capture_candidate(config, engine, task, base_branch="main")
    park_at_delivery_gate(
        engine, task, candidate_id=candidate.candidate_id, outcome="comments"
    )
    preview = await ships.preview(
        task,
        **_gate(engine, task),
        commit_message="m",
        pr_title="",
        pr_body="",
        request_id="r1",
    )
    with pytest.raises(DeliveryBlockedError):
        await ships.deliver(
            task,
            **_gate(engine, task),
            commit_message="m",
            pr_title="",
            pr_body="",
            request_id="r1",
            preview_token=preview.fingerprint,
            runner=runner,
        )
    assert list_deliveries(engine, task.id)[-1].authorized_at is None


async def test_changing_the_inputs_invalidates_the_preview_token(
    tmp_root, engine, ships, config, runner
):
    _project, task = _make_project_and_task(engine, tmp_root, ending="commit")
    _setup_git_clone(tmp_root / "token-origin.git", Path(task.clone_path))
    await _approve_current(config, engine, task)
    preview = await ships.preview(
        task,
        **_gate(engine, task),
        commit_message="one message",
        pr_title="",
        pr_body="",
        request_id="r1",
    )
    with pytest.raises(PreviewMismatchError):
        await ships.deliver(
            task,
            **_gate(engine, task),
            commit_message="a different message",
            pr_title="",
            pr_body="",
            request_id="r1",
            preview_token=preview.fingerprint,
            runner=runner,
        )


@pytest.mark.parametrize(
    ("ending", "expect_github"),
    [("commit", False), ("pr", True)],
)
async def test_a_local_ending_needs_no_github_availability(
    tmp_root, engine, ships, config, monkeypatch, ending, expect_github
):
    """Signing locally is not publishing, and does not wait on the forge.

    Two workflows, because how far a delivery goes is a property of the
    procedure the task accepted — not a choice made at the confirmation.
    """
    project, task = _make_project_and_task(engine, tmp_root, ending=ending)
    _setup_git_clone(tmp_root / "offline-origin.git", Path(task.clone_path))
    await _approve_current(config, engine, task)
    error = _blocked_preflight_error(project.upstream_url)

    async def blocked(_upstream_url: str):
        return error.status, error.target

    monkeypatch.setattr(ships._gh, "probe_target", blocked)
    monkeypatch.setattr(ships._gpg, "probe", _ready_gpg_probe())

    preview = await ships.preview(
        task,
        **_gate(engine, task),
        commit_message="m",
        pr_title="t",
        pr_body="b",
        request_id="r1",
    )
    codes = [b.code for b in preview.blockers]
    assert ("github-unavailable" in codes) is expect_github
    if not expect_github:
        assert preview.deliverable, codes


# --- endings ----------------------------------------------------------------


async def test_commit_ending_signs_the_reviewed_content_and_stops(
    tmp_root, engine, ships, config, monkeypatch, runner
):
    """A local signed commit is a complete delivery: nothing is pushed and no
    pull request is created."""
    _project, task = _make_project_and_task(engine, tmp_root, ending="commit")
    _setup_git_clone(tmp_root / "commit-origin.git", Path(task.clone_path))
    clone = Path(task.clone_path)
    _wrapper, fingerprint = _setup_signing_gpg(tmp_root / "bin", monkeypatch)
    monkeypatch.setattr(ships._gpg, "probe", _ready_gpg_probe(fingerprint))
    candidate = await _approve_current(config, engine, task)

    projection = await _deliver(ships, task, engine=engine, runner=runner)

    assert projection["ending"] == "commit"
    assert projection["disposition"] == "completed"
    assert projection["completed_actions"] == ["commit"]
    assert projection["pr_url"] is None
    result = projection["results"]["commit"]
    assert result["commit_count"] == 1
    assert result["installed"] is True
    # Ompire's own rewrite is not the operator's workspace moving: a clean
    # install carries no "the workspace changed" caveat.
    assert result["note"] is None

    # The signed commit carries exactly the reviewed tree, one commit on the
    # captured base, signed by the selected key.
    head = _git_out(clone, "rev-parse", "HEAD")
    assert head == result["signed_tip"]
    assert _git_out(clone, "rev-parse", "HEAD^{tree}") == candidate.tree_id
    assert _git_out(clone, "rev-parse", "HEAD^") == candidate.base_commit
    assert _git_out(clone, "log", "-1", "--format=%s") == "ship: the work"
    # The working tree survives and reads clean against the signed commit.
    assert (clone / "file3.txt").read_text(encoding="utf-8") == "three\n"
    assert _git_out(clone, "status", "--porcelain") == ""

    # Nothing reached the remote.
    origin_branches = _git_out(
        Path(tmp_root / "commit-origin.git"), "branch", "--list"
    )
    assert "ompire/task-1" not in origin_branches


async def test_a_push_reuses_the_signed_result_and_signs_nothing_again(
    tmp_root, engine, ships, config, monkeypatch, runner
):
    """One signature per chain. The push writes the object the commit made.

    This is what the old manual "extend a finished commit into a push" path
    was for; the extension itself is gone — a completed ending is the ending
    the operator authorized — but the property it protected is not, so it is
    checked here on the chain that actually declares both actions.
    """
    _project, task = _make_project_and_task(engine, tmp_root, ending="push")
    _setup_git_clone(tmp_root / "later-origin.git", Path(task.clone_path))
    remote = _bare(tmp_root / "later-remote.git")
    _local_destination(ships, remote, monkeypatch)
    _wrapper, fingerprint = _setup_signing_gpg(tmp_root / "bin", monkeypatch)
    monkeypatch.setattr(ships._gpg, "probe", _ready_gpg_probe(fingerprint))
    await _approve_current(config, engine, task)

    signings: list[int] = []
    original_sign = ships._sign

    async def counted(*args, **kwargs):
        signings.append(1)
        return await original_sign(*args, **kwargs)

    monkeypatch.setattr(ships, "_sign", counted)

    projection = await _deliver(ships, task, engine=engine, runner=runner)
    record = get_delivery(engine, projection["delivery_id"])

    assert len(signings) == 1
    signed_tip = record.succeeded("commit").result["signed_tip"]
    assert record.succeeded("push").result["head"] == signed_tip
    assert record.succeeded("pr") is None
    assert _git_out(remote, "rev-parse", "refs/heads/ompire/task-1") == signed_tip
    # One decision authorized the whole chain; there is no second grant.
    kinds = [d.kind for d in record.decisions]
    assert kinds.count("authorize") == 1
    assert "extend" not in kinds


async def test_a_completed_ending_cannot_be_widened_afterwards(
    tmp_root, engine, ships, config, monkeypatch, runner
):
    """A local-commit workflow stays a local-commit workflow.

    Once its chain is done there is nothing left to authorize, and no request
    turns "this was signed" into permission to push it. Widening an ending is
    a decision only an author can make, in a new procedure.
    """
    _project, task = _make_project_and_task(engine, tmp_root, ending="commit")
    _setup_git_clone(tmp_root / "narrow-origin.git", Path(task.clone_path))
    _wrapper, fingerprint = _setup_signing_gpg(tmp_root / "bin", monkeypatch)
    monkeypatch.setattr(ships._gpg, "probe", _ready_gpg_probe(fingerprint))
    await _approve_current(config, engine, task)

    projection = await _deliver(ships, task, engine=engine, runner=runner)
    assert projection["completed_actions"] == ["commit"]

    exhausted = await ships.preview(
        task,
        commit_message="m",
        pr_title="t",
        pr_body="b",
        request_id="req-widen",
        delivery_id=projection["delivery_id"],
    )
    assert exhausted.ending == "commit"
    assert "already-delivered" in [b.code for b in exhausted.blockers]
    # And asking for a longer ending by name is refused rather than obeyed.
    with pytest.raises(PreviewMismatchError, match="not 'pr'"):
        await ships.preview(
            task,
            ending="pr",
            commit_message="m",
            pr_title="t",
            pr_body="b",
            request_id="req-widen-2",
        )


async def test_pr_ending_signs_pushes_and_opens_one_pull_request(
    tmp_root, engine, ships, config, monkeypatch, runner
):
    _project, task = _make_project_and_task(engine, tmp_root, ending="pr")
    _setup_git_clone(tmp_root / "pr-origin.git", Path(task.clone_path))
    remote = _bare(tmp_root / "pr-remote.git")
    _local_destination(ships, remote, monkeypatch)
    _wrapper, fingerprint = _setup_signing_gpg(tmp_root / "bin", monkeypatch)
    monkeypatch.setattr(ships._gpg, "probe", _ready_gpg_probe(fingerprint))
    await _approve_current(config, engine, task)

    seen: list[list[str]] = []
    original_run = ships._gh.run

    async def recording(args, cwd, timeout):
        seen.append(args)
        return await original_run(args, cwd, timeout)

    monkeypatch.setattr(ships._gh, "run", recording)

    projection = await _deliver(ships, task, engine=engine, runner=runner)

    assert projection["disposition"] == "completed"
    assert projection["completed_actions"] == ["commit", "push", "pr"]
    assert projection["pr_url"] == "https://github.com/owner/repo/pull/42"
    assert get_task(engine, task.id).pr_url == projection["pr_url"]
    assert sum(1 for args in seen if args[:2] == ["pr", "create"]) == 1

    body_path = seen[[args[:2] for args in seen].index(["pr", "create"])][-1]
    # The body file is written outside the clone, so it can never be captured
    # into a later candidate.
    assert not str(body_path).startswith(str(Path(task.clone_path)))


async def test_retain_preserves_messages_trees_and_count_under_new_signatures(
    tmp_root, engine, ships, config, monkeypatch, runner
):
    _project, task = _make_project_and_task(engine, tmp_root, ending="commit", mode="retain")
    _setup_git_clone(tmp_root / "retain-origin.git", Path(task.clone_path))
    clone = Path(task.clone_path)
    # Retain publishes existing commits, so the pending edit is committed.
    _run_git(clone, "add", "file3.txt")
    _run_git(clone, "commit", "-m", "third task commit")
    _wrapper, fingerprint = _setup_signing_gpg(tmp_root / "bin", monkeypatch)
    monkeypatch.setattr(ships._gpg, "probe", _ready_gpg_probe(fingerprint))
    candidate = await _approve_current(config, engine, task)
    assert candidate.commit_count == 3

    projection = await _deliver(ships, task, engine=engine, runner=runner)
    assert projection["results"]["commit"]["commit_count"] == 3

    log = _git_out(
        clone, "log", "--format=%s", f"{candidate.base_commit}..HEAD"
    ).splitlines()
    assert log == ["third task commit", "second task commit", "first task commit"]
    trees = _git_out(
        clone, "log", "--format=%T", f"{candidate.base_commit}..HEAD"
    ).splitlines()
    assert trees == [c.tree_id for c in reversed(candidate.source_commits)]
    signers = _git_out(
        clone,
        "-c",
        "gpg.program=" + str(_wrapper),
        "log",
        "--format=%G? %GF",
        f"{candidate.base_commit}..HEAD",
    ).splitlines()
    assert all(line.split()[0] in ("G", "U") for line in signers)
    assert all(line.split()[1].upper() == fingerprint.upper() for line in signers)


async def test_retain_refuses_a_dirty_tree_and_an_empty_range(
    tmp_root, engine, ships, config, monkeypatch
):
    _project, task = _make_project_and_task(engine, tmp_root, ending="commit", mode="retain")
    _setup_git_clone(tmp_root / "retain-refuse-origin.git", Path(task.clone_path))
    monkeypatch.setattr(ships._gpg, "probe", _ready_gpg_probe())
    await _approve_current(config, engine, task)

    dirty = await ships.preview(
        task,
        **_gate(engine, task),
        commit_message="m",
        pr_title="",
        pr_body="",
        request_id="r1",
    )
    assert "retain-dirty" in [b.code for b in dirty.blockers]


async def test_an_empty_delta_is_refused_rather_than_manufactured(
    tmp_root, engine, ships
):
    _project, task = _make_project_and_task(engine, tmp_root, ending="commit")
    clone = Path(task.clone_path)
    origin = tmp_root / "empty-origin.git"
    origin.mkdir(parents=True, exist_ok=True)
    _run_git(origin, "init", "--bare")
    clone.parent.mkdir(parents=True, exist_ok=True)
    _run_git(clone.parent, "clone", str(origin), clone.name)
    _run_git(clone, "config", "user.email", "t@e.com")
    _run_git(clone, "config", "user.name", "T")
    (clone / "base.txt").write_text("base\n", encoding="utf-8")
    _run_git(clone, "add", ".")
    _run_git(clone, "commit", "-m", "base")
    _run_git(clone, "push", "origin", "HEAD:main")
    # The run is at its approval; what is missing is content, not authority.
    park_at_delivery_gate(engine, task, candidate_id="never-captured")

    preview = await ships.preview(
        task,
        **_gate(engine, task),
        commit_message="m",
        pr_title="",
        pr_body="",
        request_id="r1",
    )
    assert "empty-candidate" in [b.code for b in preview.blockers]


# --- replay and exclusivity -------------------------------------------------


async def test_a_replayed_confirmation_does_not_start_a_second_delivery(
    tmp_root, engine, ships, config, monkeypatch, runner
):
    _project, task = _make_project_and_task(engine, tmp_root, ending="commit")
    _setup_git_clone(tmp_root / "replay-origin.git", Path(task.clone_path))
    _wrapper, fingerprint = _setup_signing_gpg(tmp_root / "bin", monkeypatch)
    monkeypatch.setattr(ships._gpg, "probe", _ready_gpg_probe(fingerprint))
    await _approve_current(config, engine, task)

    first = await _deliver(ships, task, engine=engine, runner=runner, request_id="same-request")
    signed_tip = first["results"]["commit"]["signed_tip"]

    # The same confirmation arrives again — a double submit, or a retried
    # request. It must not sign a second time.
    preview = await ships.preview(
        task,
        **_gate(engine, task),
        commit_message="ship: the work",
        pr_title="The work",
        pr_body="why",
        request_id="same-request",
    )
    assert "review-stale" in [b.code for b in preview.blockers] or not preview.deliverable
    deliveries = list_deliveries(engine, task.id)
    assert len(deliveries) == 1
    assert deliveries[0].succeeded("commit").result["signed_tip"] == signed_tip


async def test_a_delivery_is_refused_while_another_writer_owns_the_workspace(
    tmp_root, engine, ships, config, guard, monkeypatch
):
    _project, task = _make_project_and_task(engine, tmp_root)
    _setup_git_clone(tmp_root / "busy-origin.git", Path(task.clone_path))
    monkeypatch.setattr(ships._gpg, "probe", _ready_gpg_probe())
    await _approve_current(config, engine, task)

    guard.acquire(task.id, "review")
    try:
        preview = await ships.preview(
            task,
            **_gate(engine, task),
            commit_message="m",
            pr_title="",
            pr_body="",
            request_id="r1",
        )
    finally:
        guard.release(task.id, "review")
    assert "workspace-busy" in [b.code for b in preview.blockers]


# --- signing safety ---------------------------------------------------------


async def test_clone_local_signing_config_cannot_redirect_the_commit(
    tmp_root, engine, ships, config, monkeypatch
):
    """The clone is agent-writable, so it must not choose the signing program.

    A clone that tries is refused outright at capture, which is the earliest
    point the daemon can see it and the only one where refusing costs nothing.
    """
    from ompire_daemon.delivery import UnsafeCloneConfigError

    _project, task = _make_project_and_task(engine, tmp_root)
    _setup_git_clone(tmp_root / "evil-origin.git", Path(task.clone_path))
    clone = Path(task.clone_path)
    evil = tmp_root / "evil-gpg"
    evil.write_text("#!/bin/sh\ntouch " + str(tmp_root / "PWNED") + "\nexit 0\n")
    evil.chmod(0o755)
    _run_git(clone, "config", "gpg.program", str(evil))

    with pytest.raises(UnsafeCloneConfigError):
        await capture_candidate(config, engine, task, base_branch="main")
    assert not (tmp_root / "PWNED").exists()


async def test_a_workspace_that_moved_on_blocks_installation_of_the_signed_result(
    tmp_root, engine, ships, config, monkeypatch, runner
):
    """The signed content is still the reviewed content, so it is kept — but
    a branch that moved under the delivery is not overwritten."""
    _project, task = _make_project_and_task(engine, tmp_root, ending="commit")
    _setup_git_clone(tmp_root / "moved-origin.git", Path(task.clone_path))
    clone = Path(task.clone_path)
    _wrapper, fingerprint = _setup_signing_gpg(tmp_root / "bin", monkeypatch)
    monkeypatch.setattr(ships._gpg, "probe", _ready_gpg_probe(fingerprint))
    candidate = await _approve_current(config, engine, task)

    original_sign = ships._sign

    async def sign_then_move(*args, **kwargs):
        result = await original_sign(*args, **kwargs)
        # An agent lands a commit while the signature is being produced.
        (clone / "raced.txt").write_text("raced\n", encoding="utf-8")
        _run_git(clone, "add", "raced.txt")
        _run_git(clone, "commit", "-m", "agent kept working")
        return result

    monkeypatch.setattr(ships, "_sign", sign_then_move)

    preview = await ships.preview(
        task,
        **_gate(engine, task),
        commit_message="m",
        pr_title="",
        pr_body="",
        request_id="r1",
    )
    delivery_id, _p = await ships.confirm(task, preview, runner=runner)
    await ships._run_prefix(task, delivery_id, "r1")

    record = get_delivery(engine, delivery_id)
    commit = record.succeeded("commit")
    assert commit is not None
    assert commit.result["installed"] is False
    assert record.disposition == "blocked"
    # The agent's commit is untouched, and the reviewed signed result is
    # retained in the candidate's own repository.
    assert _git_out(clone, "log", "-1", "--format=%s") == "agent kept working"
    store = Path(candidate.storage_path)
    assert _git_out(store, "rev-parse", commit.result["signed_ref"]) == commit.result[
        "signed_tip"
    ]


# --- push safety ------------------------------------------------------------


async def test_push_writes_the_authorized_object_under_the_recorded_lease(
    tmp_root, engine, ships, config, monkeypatch, runner
):
    _project, task = _make_project_and_task(engine, tmp_root, ending="push")
    _setup_git_clone(tmp_root / "lease-origin.git", Path(task.clone_path))
    remote = _bare(tmp_root / "lease-remote.git")
    _local_destination(ships, remote, monkeypatch)
    _wrapper, fingerprint = _setup_signing_gpg(tmp_root / "bin", monkeypatch)
    monkeypatch.setattr(ships._gpg, "probe", _ready_gpg_probe(fingerprint))
    await _approve_current(config, engine, task)

    projection = await _deliver(ships, task, engine=engine, runner=runner)
    push = projection["results"]["push"]
    assert push["pre_push_oid"] is None
    assert push["head"] == projection["results"]["commit"]["signed_tip"]
    assert _git_out(remote, "rev-parse", "refs/heads/ompire/task-1") == push["head"]
    assert projection["results"].get("pr") is None


async def test_an_unexpected_remote_head_is_a_conflict_not_permission_to_force(
    tmp_root, engine, ships, config, monkeypatch, runner
):
    """The lease is the head Ompire observed and recorded, not whatever the
    destination holds by the time the write goes out."""
    _project, task = _make_project_and_task(engine, tmp_root, ending="push")
    _setup_git_clone(tmp_root / "conflict-origin.git", Path(task.clone_path))
    remote = _bare(tmp_root / "conflict-remote.git")
    _local_destination(ships, remote, monkeypatch)
    _wrapper, fingerprint = _setup_signing_gpg(tmp_root / "bin", monkeypatch)
    monkeypatch.setattr(ships._gpg, "probe", _ready_gpg_probe(fingerprint))
    await _approve_current(config, engine, task)

    # Someone else already owns the destination branch.
    other = tmp_root / "other-publisher"
    _run_git(tmp_root, "clone", str(remote), other.name)
    _run_git(other, "config", "user.email", "other@e.com")
    _run_git(other, "config", "user.name", "Other")
    (other / "theirs.txt").write_text("theirs\n", encoding="utf-8")
    _run_git(other, "add", ".")
    _run_git(other, "commit", "-m", "someone else was here")
    _run_git(other, "push", str(remote), "HEAD:refs/heads/ompire/task-1")
    theirs = _git_out(remote, "rev-parse", "refs/heads/ompire/task-1")

    original_push = ships._push

    async def push_after_a_race(clone, destination, tip, observed, timeout):
        # Between the observation Ompire recorded and the write, they push
        # again. The recorded lease no longer matches.
        _run_git(other, "commit", "--allow-empty", "-m", "and again")
        _run_git(other, "push", str(remote), "HEAD:refs/heads/ompire/task-1")
        return await original_push(clone, destination, tip, observed, timeout)

    monkeypatch.setattr(ships, "_push", push_after_a_race)
    preview = await ships.preview(
        task,
        **_gate(engine, task),
        commit_message="m",
        pr_title="",
        pr_body="",
        request_id="r1",
    )
    delivery_id, _p = await ships.confirm(task, preview, runner=runner)
    await ships._run_prefix(task, delivery_id, "r1")

    record = get_delivery(engine, delivery_id)
    assert record.succeeded("commit") is not None
    push = record.action("push")
    assert push.expected["pre_push_oid"] == theirs
    assert push.phase == "failed"
    assert record.disposition == "blocked"
    # Nothing of theirs was overwritten.
    landed = _git_out(remote, "rev-parse", "refs/heads/ompire/task-1")
    assert landed != record.succeeded("commit").result["signed_tip"]


async def test_ssh_authentication_classification_is_narrow(
    tmp_root, engine, ships, monkeypatch
):
    async def denied(argv, **kwargs):
        return "", "Permission denied (publickey)", 128

    monkeypatch.setattr("ompire_daemon.ship.run_git", denied)
    ssh = {"remote_url": "git@github.com:owner/repo.git", "ref": "refs/heads/f"}
    https = {"remote_url": "https://github.com/owner/repo.git", "ref": "refs/heads/f"}
    with pytest.raises(SshAuthenticationError):
        await ships._push("/irrelevant", ssh, "deadbeef", None, 10)
    with pytest.raises(PushError) as ordinary:
        await ships._push("/irrelevant", https, "deadbeef", None, 10)
    assert not isinstance(ordinary.value, SshAuthenticationError)


# --- recovery ---------------------------------------------------------------


async def test_restart_adopts_a_push_that_actually_landed(
    tmp_root, engine, ships, config, monkeypatch, runner
):
    """A lost response is not a reason to push again."""
    _project, task = _make_project_and_task(engine, tmp_root, ending="push")
    _setup_git_clone(tmp_root / "adopt-origin.git", Path(task.clone_path))
    remote = _bare(tmp_root / "adopt-remote.git")
    _local_destination(ships, remote, monkeypatch)
    _wrapper, fingerprint = _setup_signing_gpg(tmp_root / "bin", monkeypatch)
    monkeypatch.setattr(ships._gpg, "probe", _ready_gpg_probe(fingerprint))
    await _approve_current(config, engine, task)

    original_push = ships._push

    async def push_then_die(clone, destination, tip, observed, timeout):
        await original_push(clone, destination, tip, observed, timeout)
        raise asyncio.CancelledError

    monkeypatch.setattr(ships, "_push", push_then_die)
    preview = await ships.preview(
        task,
        **_gate(engine, task),
        commit_message="m",
        pr_title="",
        pr_body="",
        request_id="r1",
    )
    delivery_id, _p = await ships.confirm(task, preview, runner=runner)
    with pytest.raises(asyncio.CancelledError):
        await ships._run_prefix(task, delivery_id, "r1")

    record = get_delivery(engine, delivery_id)
    assert record.action("push").phase == "executing"

    pushes: list[tuple] = []

    async def must_not_run(*args, **kwargs):
        pushes.append(args)
        raise AssertionError("startup must not push")

    monkeypatch.setattr(ships, "_push", must_not_run)
    blocked = await ships.restore()

    assert blocked == []
    assert pushes == []
    restored = get_delivery(engine, delivery_id)
    assert restored.succeeded("push") is not None
    assert restored.succeeded("push").result["adopted"] is True
    assert restored.disposition == "completed"


async def test_restart_proves_a_signing_attempt_never_produced_anything(
    tmp_root, engine, ships, config, monkeypatch, runner
):
    _project, task = _make_project_and_task(engine, tmp_root, ending="commit")
    _setup_git_clone(tmp_root / "nosig-origin.git", Path(task.clone_path))
    _wrapper, fingerprint = _setup_signing_gpg(tmp_root / "bin", monkeypatch)
    monkeypatch.setattr(ships._gpg, "probe", _ready_gpg_probe(fingerprint))
    await _approve_current(config, engine, task)

    async def die_before_signing(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(ships, "_sign", die_before_signing)
    preview = await ships.preview(
        task,
        **_gate(engine, task),
        commit_message="m",
        pr_title="",
        pr_body="",
        request_id="r1",
    )
    delivery_id, _p = await ships.confirm(task, preview, runner=runner)
    with pytest.raises(asyncio.CancelledError):
        await ships._run_prefix(task, delivery_id, "r1")
    assert get_delivery(engine, delivery_id).action("commit").phase == "executing"

    signed: list[tuple] = []

    async def must_not_sign(*args, **kwargs):
        signed.append(args)
        raise AssertionError("startup must not sign")

    monkeypatch.setattr(ships, "_sign", must_not_sign)
    blocked = await ships.restore()

    assert blocked == []
    assert signed == []
    record = get_delivery(engine, delivery_id)
    assert record.action("commit").phase == "failed"
    assert record.disposition == "blocked"
    assert "proven not to have happened" in record.action("commit").error


async def test_an_unreadable_destination_leaves_a_push_unresolved_and_blocks_the_task(
    tmp_root, engine, ships, config, guard, monkeypatch, runner
):
    """Not being able to look is not evidence that nothing happened."""
    _project, task = _make_project_and_task(engine, tmp_root, ending="push")
    _setup_git_clone(tmp_root / "unknown-origin.git", Path(task.clone_path))
    remote = _bare(tmp_root / "unknown-remote.git")
    _local_destination(ships, remote, monkeypatch)
    _wrapper, fingerprint = _setup_signing_gpg(tmp_root / "bin", monkeypatch)
    monkeypatch.setattr(ships._gpg, "probe", _ready_gpg_probe(fingerprint))
    await _approve_current(config, engine, task)

    preview = await ships.preview(
        task,
        **_gate(engine, task),
        commit_message="m",
        pr_title="",
        pr_body="",
        request_id="r1",
    )
    delivery_id, _p = await ships.confirm(task, preview, runner=runner)

    original_remote_head = ships._remote_head
    calls = {"n": 0}

    async def flaky(clone_path, remote_url, ref, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            return await original_remote_head(clone_path, remote_url, ref, timeout)
        raise PushError("the remote is unreachable")

    async def failing_push(*args, **kwargs):
        raise PushError("connection reset")

    monkeypatch.setattr(ships, "_remote_head", flaky)
    monkeypatch.setattr(ships, "_push", failing_push)
    await ships._run_prefix(task, delivery_id, "r1")

    record = get_delivery(engine, delivery_id)
    assert record.action("push").phase == "needs_reconciliation"
    assert record.disposition == "unresolved"
    assert guard.blocked_reason(task.id) is not None

    # An operator can recheck once the remote is back. It never pushed, so the
    # destination still holds nothing and a retry becomes eligible.
    monkeypatch.setattr(ships, "_remote_head", original_remote_head)
    projection = await ships.reconcile(
        task,
        delivery_id=delivery_id,
        action_id=record.action("push").id,
        expected_version=record.version,
        decision="retry",
        note="the remote was down",
    )
    assert projection["disposition"] == "blocked"
    assert guard.blocked_reason(task.id) is None


async def test_a_lost_pull_request_response_is_found_by_its_correlation_marker(
    tmp_root, engine, ships, config, monkeypatch, runner
):
    _project, task = _make_project_and_task(engine, tmp_root, ending="pr")
    _setup_git_clone(tmp_root / "marker-origin.git", Path(task.clone_path))
    remote = _bare(tmp_root / "marker-remote.git")
    _local_destination(ships, remote, monkeypatch)
    _wrapper, fingerprint = _setup_signing_gpg(tmp_root / "bin", monkeypatch)
    monkeypatch.setattr(ships._gpg, "probe", _ready_gpg_probe(fingerprint))
    await _approve_current(config, engine, task)

    from ompire_daemon.ship import PullRequestError

    marker = correlation_marker(task.id, "r1")
    created: list[str] = []

    async def gh_run(args, cwd, timeout):
        from ompire_daemon.gh import GitHubCommandResult

        if args[:2] == ["pr", "create"]:
            created.append("create")
            # The pull request is created and the reply is lost.
            raise PullRequestError("connection reset after the request was sent")
        if args[:2] == ["pr", "list"]:
            payload = json.dumps(
                [
                    {
                        "number": 9,
                        "url": "https://github.com/owner/repo/pull/9",
                        "state": "OPEN",
                        "body": f"why\n\n<!-- ompire-delivery: {marker} -->",
                        "headRefName": "ompire/task-1",
                        "baseRefName": "main",
                    },
                    {
                        "number": 3,
                        "url": "https://github.com/owner/repo/pull/3",
                        "state": "CLOSED",
                        "body": "an unrelated pull request",
                        "headRefName": "ompire/task-1",
                        "baseRefName": "main",
                    },
                ]
            )
            return GitHubCommandResult(returncode=0, stdout=payload, stderr="")
        return GitHubCommandResult(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(ships._gh, "run", gh_run)
    projection = await _deliver(ships, task, engine=engine, runner=runner, request_id="r1")

    assert created == ["create"]
    assert projection["results"]["pr"]["adopted"] is True
    assert projection["pr_url"] == "https://github.com/owner/repo/pull/9"
    assert get_task(engine, task.id).pr_url == "https://github.com/owner/repo/pull/9"
    # Adopting a lost reply is the delivery completing, not recovering from a
    # failure: the ending it reached is the one that was authorized.
    assert projection["disposition"] == "completed"
    assert projection["completed_actions"] == ["commit", "push", "pr"]


async def test_an_incomplete_pr_search_leaves_the_outcome_unknown(
    tmp_root, engine, ships, config, guard, monkeypatch
):
    _project, task = _make_project_and_task(engine, tmp_root)
    _setup_git_clone(tmp_root / "unknown-pr-origin.git", Path(task.clone_path))
    remote = _bare(tmp_root / "unknown-pr-remote.git")
    _local_destination(ships, remote, monkeypatch)
    _wrapper, fingerprint = _setup_signing_gpg(tmp_root / "bin", monkeypatch)
    monkeypatch.setattr(ships._gpg, "probe", _ready_gpg_probe(fingerprint))
    await _approve_current(config, engine, task)

    async def gh_run(args, cwd, timeout):
        from ompire_daemon.gh import GitHubCommandResult

        if args[:2] == ["pr", "create"]:
            return GitHubCommandResult(returncode=1, stdout="", stderr="HTTP 502")
        if args[:2] == ["pr", "list"]:
            return GitHubCommandResult(returncode=1, stdout="", stderr="HTTP 502")
        return GitHubCommandResult(returncode=1, stdout="", stderr="unexpected")

    monkeypatch.setattr(ships._gh, "run", gh_run)
    preview = await ships.preview(
        task,
        **_gate(engine, task),
        commit_message="m",
        pr_title="t",
        pr_body="b",
        request_id="r1",
    )
    delivery_id, _p = await ships.authorize(task, preview, expected_version=preview.version)
    await ships._run_prefix(task, delivery_id, "r1")

    record = get_delivery(engine, delivery_id)
    assert record.action("pr").phase == "needs_reconciliation"
    assert record.disposition == "unresolved"
    assert guard.blocked_reason(task.id) is not None
    assert get_task(engine, task.id).pr_url is None

    # Abandoning does not erase the unknown effect.
    projection = await ships.reconcile(
        task,
        delivery_id=delivery_id,
        action_id=record.action("pr").id,
        expected_version=record.version,
        decision="abandon",
        note="giving up for now",
    )
    assert projection["disposition"] == "unresolved"
    assert guard.blocked_reason(task.id) is not None


# --- projection -------------------------------------------------------------


async def test_a_legacy_publication_is_a_known_fact_with_no_invented_journal(
    tmp_root, engine, ships
):
    _project, task = _make_project_and_task(engine, tmp_root)
    mark_pr_url(engine, task.id, "https://github.com/owner/repo/pull/1")
    projection = ships.projection(get_task(engine, task.id))
    assert projection is not None
    assert projection["pr_url"] == "https://github.com/owner/repo/pull/1"
    assert projection["legacy_publication"] is True
    assert projection["history"] == []
    assert projection["delivery_id"] is None


async def test_the_projection_version_advances_with_every_committed_change(
    tmp_root, engine, ships, config, monkeypatch, runner
):
    _project, task = _make_project_and_task(engine, tmp_root, ending="commit")
    _setup_git_clone(tmp_root / "version-origin.git", Path(task.clone_path))
    _wrapper, fingerprint = _setup_signing_gpg(tmp_root / "bin", monkeypatch)
    monkeypatch.setattr(ships._gpg, "probe", _ready_gpg_probe(fingerprint))
    await _approve_current(config, engine, task)

    before = ships.projection(task)
    after = await _deliver(ships, task, engine=engine, runner=runner)
    assert before is None or after["version"] > before["version"]
    assert get_latest_delivery(engine, task.id).version == after["version"]


async def test_a_refused_first_action_can_be_corrected_and_confirmed_again(
    tmp_root, engine, ships, config, monkeypatch, runner
):
    """Nothing succeeded, so there is no terminal prefix to protect."""
    _project, task = _make_project_and_task(engine, tmp_root, ending="commit")
    _setup_git_clone(tmp_root / "correct-origin.git", Path(task.clone_path))
    _wrapper, fingerprint = _setup_signing_gpg(tmp_root / "bin", monkeypatch)
    monkeypatch.setattr(ships._gpg, "probe", _ready_gpg_probe(fingerprint))
    await _approve_current(config, engine, task)

    original_sign = ships._sign

    async def refuse(*args, **kwargs):
        raise DeliveryWorkspaceError("gpg said no")

    monkeypatch.setattr(ships, "_sign", refuse)
    preview = await ships.preview(
        task,
        **_gate(engine, task),
        commit_message="first attempt",
        pr_title="",
        pr_body="",
        request_id="r1",
    )
    delivery_id, _p = await ships.confirm(task, preview, runner=runner)
    await ships._run_prefix(task, delivery_id, "r1")
    assert get_delivery(engine, delivery_id).disposition == "blocked"

    # Signing works again. The decision stands, so the correction re-confirms
    # the same authorization rather than asking for a second approval.
    monkeypatch.setattr(ships, "_sign", original_sign)
    corrected = await ships.preview(
        task,
        commit_message="corrected message",
        pr_title="",
        pr_body="",
        request_id="r2",
    )
    assert corrected.source == "workflow-action"
    delivery_id, _p = await ships.confirm(task, corrected, runner=runner)
    await ships._run_prefix(task, delivery_id, "r2")
    projection = ships.projection(get_task(engine, task.id))

    assert projection["disposition"] == "completed"
    assert len(list_deliveries(engine, task.id)) == 1
    clone = Path(task.clone_path)
    assert _git_out(clone, "log", "-1", "--format=%s") == "corrected message"


async def test_a_completed_prefix_survives_a_refusal_and_resumes_without_re_signing(
    tmp_root, engine, ships, config, monkeypatch, runner
):
    _project, task = _make_project_and_task(engine, tmp_root, ending="push")
    _setup_git_clone(tmp_root / "resume-origin.git", Path(task.clone_path))
    remote = _bare(tmp_root / "resume-remote.git")
    _local_destination(ships, remote, monkeypatch)
    _wrapper, fingerprint = _setup_signing_gpg(tmp_root / "bin", monkeypatch)
    monkeypatch.setattr(ships._gpg, "probe", _ready_gpg_probe(fingerprint))
    await _approve_current(config, engine, task)

    original_push = ships._push
    attempts = {"n": 0}

    async def flaky_push(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise PushError("the remote refused it")
        return await original_push(*args, **kwargs)

    monkeypatch.setattr(ships, "_push", flaky_push)
    preview = await ships.preview(
        task,
        **_gate(engine, task),
        commit_message="m",
        pr_title="",
        pr_body="",
        request_id="r1",
    )
    delivery_id, _p = await ships.confirm(task, preview, runner=runner)
    await ships._run_prefix(task, delivery_id, "r1")

    blocked = get_delivery(engine, delivery_id)
    assert blocked.disposition == "blocked"
    signed_tip = blocked.succeeded("commit").result["signed_tip"]

    signings: list[tuple] = []

    async def must_not_sign(*args, **kwargs):
        signings.append(args)
        raise AssertionError("a resumed delivery must not sign again")

    monkeypatch.setattr(ships, "_sign", must_not_sign)
    projection = await _deliver(ships, task, engine=engine, runner=runner, request_id="r2")

    assert signings == []
    assert projection["disposition"] == "completed"
    assert projection["results"]["commit"]["signed_tip"] == signed_tip
    assert projection["results"]["push"]["head"] == signed_tip
    # The completed commit was never re-attempted, and the retry is on record.
    record = get_delivery(engine, delivery_id)
    assert len([a for a in record.actions if a.kind == "commit"]) == 1
    assert "retry" in [d.kind for d in record.decisions]


async def test_adopting_an_unresolved_final_action_completes_the_delivery(
    tmp_root, engine, ships, config, guard, monkeypatch, runner
):
    """Recovering from an unknown effect is not authorization to keep writing,
    but an adopted result that finishes the ending finishes the delivery."""
    _project, task = _make_project_and_task(engine, tmp_root, ending="pr")
    _setup_git_clone(tmp_root / "settle-origin.git", Path(task.clone_path))
    remote = _bare(tmp_root / "settle-remote.git")
    _local_destination(ships, remote, monkeypatch)
    _wrapper, fingerprint = _setup_signing_gpg(tmp_root / "bin", monkeypatch)
    monkeypatch.setattr(ships._gpg, "probe", _ready_gpg_probe(fingerprint))
    await _approve_current(config, engine, task)

    marker = correlation_marker(task.id, "r1")
    listing_available = {"value": False}

    async def gh_run(args, cwd, timeout):
        from ompire_daemon.gh import GitHubCommandResult

        if args[:2] == ["pr", "create"]:
            raise PullRequestError("connection reset after the request was sent")
        if args[:2] == ["pr", "list"]:
            if not listing_available["value"]:
                return GitHubCommandResult(returncode=1, stdout="", stderr="HTTP 502")
            payload = json.dumps(
                [
                    {
                        "number": 11,
                        "url": "https://github.com/owner/repo/pull/11",
                        "state": "OPEN",
                        "body": f"why\n\n<!-- ompire-delivery: {marker} -->",
                        "headRefName": "ompire/task-1",
                        "baseRefName": "main",
                    }
                ]
            )
            return GitHubCommandResult(returncode=0, stdout=payload, stderr="")
        return GitHubCommandResult(returncode=1, stdout="", stderr="unexpected")

    from ompire_daemon.ship import PullRequestError

    monkeypatch.setattr(ships._gh, "run", gh_run)
    projection = await _deliver(ships, task, engine=engine, runner=runner, request_id="r1")

    assert projection["disposition"] == "unresolved"
    assert guard.blocked_reason(task.id) is not None
    delivery_id = projection["delivery_id"]
    action_id = [a for a in projection["actions"] if a["kind"] == "pr"][-1]["id"]

    # The forge answers again and the operator rechecks.
    listing_available["value"] = True
    resolved = await ships.reconcile(
        task,
        delivery_id=delivery_id,
        action_id=action_id,
        expected_version=projection["version"],
        decision="recheck",
        note="the forge is reachable again",
    )
    assert resolved["disposition"] == "completed"
    assert resolved["results"]["pr"]["adopted"] is True
    assert resolved["pr_url"] == "https://github.com/owner/repo/pull/11"
    assert get_task(engine, task.id).pr_url == "https://github.com/owner/repo/pull/11"
    assert guard.blocked_reason(task.id) is None


async def test_a_delivery_records_the_workflow_revision_it_published_from(
    tmp_root, engine, ships, config, monkeypatch, runner
):
    """Attribution, not policy: a delivery says which pinned procedure produced
    the work it published, and that stays true after the library moves on."""
    _project, task = _make_project_and_task(engine, tmp_root, ending="commit")
    _setup_git_clone(tmp_root / "attribution-origin.git", Path(task.clone_path))
    _wrapper, fingerprint = _setup_signing_gpg(tmp_root / "bin", monkeypatch)
    monkeypatch.setattr(ships._gpg, "probe", _ready_gpg_probe(fingerprint))
    await _approve_current(config, engine, task)

    await _deliver(ships, task, engine=engine, runner=runner)

    record = get_latest_delivery(engine, task.id)
    expected = task.execution_inputs.workflow_binding.revision
    assert expected
    assert record.workflow_revision == expected


async def test_terminal_candidate_storage_is_released_and_unresolved_storage_is_kept(
    tmp_root, engine, ships, config, monkeypatch, runner
):
    """Candidate objects are temporary operation evidence: they go once nothing
    still needs them, and they stay while something might."""
    from ompire_daemon.registry.ships import list_task_candidates

    _project, task = _make_project_and_task(engine, tmp_root, ending="commit")
    _setup_git_clone(tmp_root / "lifecycle-origin.git", Path(task.clone_path))
    _wrapper, fingerprint = _setup_signing_gpg(tmp_root / "bin", monkeypatch)
    monkeypatch.setattr(ships._gpg, "probe", _ready_gpg_probe(fingerprint))
    candidate = await _approve_current(config, engine, task)
    store = Path(candidate.storage_path)
    assert store.exists()

    await _deliver(ships, task, engine=engine, runner=runner)

    # The delivery is terminal, so its evidence has served its purpose.
    assert not store.exists()
    retained = [c for c in list_task_candidates(engine, task.id)]
    assert retained, "the manifest is history and stays"
    assert all(c.storage_path is None for c in retained)


# --- workflow-scoped authority (format 3) ------------------------------------


async def test_authority_comes_from_the_question_not_from_an_agent_claim(
    tmp_root, engine, ships, config, monkeypatch
):
    """A result that *looks* like a review verdict establishes nothing.

    The gate's frozen binding names a step and an attempt, and the verdict is
    read from the review journal for that attempt. An agent step that writes
    `{"result": "approved"}` into its own outcome is writing data, and the
    binding it is not named by cannot be satisfied by it.
    """
    from ompire_daemon.registry.workflows import (
        append_step_record,
        build_gate_snapshot,
        finish_step_record,
        park_gate,
    )

    _project, task = _make_project_and_task(engine, tmp_root, ending="commit")
    _setup_git_clone(tmp_root / "forged-origin.git", Path(task.clone_path))
    monkeypatch.setattr(ships._gpg, "probe", _ready_gpg_probe())

    forged = append_step_record(
        engine, task.id, step="work", kind="agent", session="main", evidence=None
    )
    finish_step_record(
        engine,
        task.id,
        forged.seq,
        status="ok",
        outcome={"version": 2, "result": "approved", "summary": "all good"},
    )
    gate = append_step_record(
        engine,
        task.id,
        step="approve",
        kind="gate",
        session=None,
        evidence={
            "version": 1,
            # Pointed at the agent's own attempt, which is the substitution
            # under test.
            "bindings": {"verdict": {"step": "work", "seq": forged.seq}},
        },
    )
    park_gate(
        engine,
        task.id,
        gate.seq,
        step="approve",
        message="Publish?",
        snapshot=build_gate_snapshot(
            message="Publish?",
            choices=[
                {
                    "id": "publish",
                    "label": "Publish",
                    "feedback_required": False,
                    "next": {"step": "commit"},
                    "authorize": {"steps": ["commit"]},
                }
            ],
            evidence={"verdict": {"step": "work", "seq": forged.seq}},
        ),
    )

    preview = await ships.preview(
        task,
        gate_seq=gate.seq,
        choice_id="publish",
        commit_message="m",
        pr_title="",
        pr_body="",
        request_id="r1",
    )
    codes = [b.code for b in preview.blockers]
    assert "review-unrecorded" in codes
    assert not preview.deliverable


async def test_a_preview_must_name_the_question_it_is_about(
    tmp_root, engine, ships, config
):
    _project, task = _make_project_and_task(engine, tmp_root, ending="commit")
    _setup_git_clone(tmp_root / "named-origin.git", Path(task.clone_path))
    await _approve_current(config, engine, task)
    gate = _gate(engine, task)

    with pytest.raises(PreviewMismatchError, match="must name the question"):
        await ships.preview(
            task, commit_message="m", pr_title="", pr_body="", request_id="r1"
        )
    with pytest.raises(PreviewMismatchError, match="reload the question"):
        await ships.preview(
            task,
            gate_seq=gate["gate_seq"] + 5,
            choice_id="publish",
            commit_message="m",
            pr_title="",
            pr_body="",
            request_id="r1",
        )
    with pytest.raises(PreviewMismatchError, match="not one of this question"):
        await ships.preview(
            task,
            gate_seq=gate["gate_seq"],
            choice_id="nope",
            commit_message="m",
            pr_title="",
            pr_body="",
            request_id="r1",
        )
    with pytest.raises(PreviewMismatchError, match="authorizes no publication"):
        await ships.preview(
            task,
            gate_seq=gate["gate_seq"],
            choice_id="finish",
            commit_message="m",
            pr_title="",
            pr_body="",
            request_id="r1",
        )


async def test_answering_the_question_twice_authorizes_once(
    tmp_root, engine, ships, config, monkeypatch, runner
):
    """The decision, the grant, and the successor are one transaction.

    A replayed confirmation is answering a question that no longer has an
    unanswered form, so it is refused before anything else happens — there is
    no window in which it could open a second chain.
    """
    _project, task = _make_project_and_task(engine, tmp_root, ending="commit")
    _setup_git_clone(tmp_root / "twice-origin.git", Path(task.clone_path))
    _wrapper, fingerprint = _setup_signing_gpg(tmp_root / "bin", monkeypatch)
    monkeypatch.setattr(ships._gpg, "probe", _ready_gpg_probe(fingerprint))
    await _approve_current(config, engine, task)

    gate = _gate(engine, task)
    preview = await ships.preview(
        task,
        **gate,
        commit_message="m",
        pr_title="",
        pr_body="",
        request_id="r1",
    )
    delivery_id, _p = await ships.confirm(task, preview, runner=runner)

    with pytest.raises((WorkflowWaitConflictError, WorkflowNotWaitingError)):
        await ships.confirm(task, preview, runner=runner)
    assert len(list_deliveries(engine, task.id)) == 1
    record = get_delivery(engine, delivery_id)
    assert [d.kind for d in record.decisions].count("authorize") == 1
    assert record.workflow_authorized is True
    assert record.workflow_choice_id == "publish"
    assert record.workflow_gate_seq == gate["gate_seq"]
    # The run moved to its first action in the same write.
    from ompire_daemon.registry.workflows import list_step_records

    assert list_step_records(engine, task.id)[-1].step == "commit"


async def test_a_confirmation_cannot_smuggle_a_chain_the_choice_never_granted(
    tmp_root, engine, ships, config, monkeypatch, runner
):
    """The runner checks the confirmed chain against the pinned document."""
    from dataclasses import replace as dc_replace

    _project, task = _make_project_and_task(engine, tmp_root, ending="commit")
    _setup_git_clone(tmp_root / "smuggle-origin.git", Path(task.clone_path))
    monkeypatch.setattr(ships._gpg, "probe", _ready_gpg_probe())
    await _approve_current(config, engine, task)

    preview = await ships.preview(
        task,
        **_gate(engine, task),
        commit_message="m",
        pr_title="",
        pr_body="",
        request_id="r1",
    )
    widened = dc_replace(preview, ending="pr")
    with pytest.raises(WorkflowGateChoiceError, match="is not the chain"):
        await ships.confirm(task, widened, runner=runner)
    assert list_deliveries(engine, task.id)[-1].authorized_at is None


async def test_the_run_performs_its_authorized_chain_and_ends_where_it_declared(
    tmp_root, engine, ships, config, hub, sessions, monkeypatch
):
    """The whole slice, end to end, driven by the run itself.

    The operator answers one question; the run performs commit, push, and the
    pull request as three declared steps, consuming each verified predecessor,
    and ends at the named result its last action routes to. Nothing schedules
    a prefix behind the run's back, and each effect is linked to the attempt
    that asked for it.
    """
    from ompire_daemon.registry.workflows import list_step_records
    from ompire_daemon.taskdefinition import resolve_task_definition

    _project, task = _make_project_and_task(engine, tmp_root, ending="pr")
    _setup_git_clone(tmp_root / "chain-origin.git", Path(task.clone_path))
    remote = _bare(tmp_root / "chain-remote.git")
    _local_destination(ships, remote, monkeypatch)
    _wrapper, fingerprint = _setup_signing_gpg(tmp_root / "bin", monkeypatch)
    monkeypatch.setattr(ships._gpg, "probe", _ready_gpg_probe(fingerprint))
    await _approve_current(config, engine, task)

    live = WorkflowRunner(
        engine, config, hub, AgentSupervisor(config, hub, sessions), sessions
    )
    live.set_guard(ships._guard)
    live.set_operations(None, ships)
    try:
        preview = await ships.preview(
            task,
            **_gate(engine, task),
            commit_message="ship: it",
            pr_title="The work",
            pr_body="why",
            request_id="r1",
        )
        assert preview.deliverable, [b.code for b in preview.blockers]
        await ships.confirm(task, preview, runner=live)

        deadline = asyncio.get_running_loop().time() + 30
        while asyncio.get_running_loop().time() < deadline:
            current = get_task(engine, task.id)
            if current.workflow_status in ("complete", "failed"):
                break
            await asyncio.sleep(0.02)
    finally:
        await live.shutdown()

    finished = get_task(engine, task.id)
    assert finished.workflow_status == "complete"
    assert finished.workflow_result == "published"

    record = get_latest_delivery(engine, task.id)
    assert record.disposition == "completed"
    signed_tip = record.succeeded("commit").result["signed_tip"]
    assert record.succeeded("push").result["head"] == signed_tip
    assert record.succeeded("pr").result["url"].startswith("https://github.com/")
    assert _git_out(remote, "rev-parse", "refs/heads/ompire/task-1") == signed_tip

    # Each effect is attributable to the step attempt that asked for it, and
    # each step records what actually happened rather than its own label.
    steps = {r.step: r for r in list_step_records(engine, task.id)}
    for name in ("commit", "push", "pr"):
        attempt = steps[name]
        assert attempt.status == "ok"
        assert attempt.outcome["action"] == name
        action = record.succeeded(name)
        assert action.workflow_seq == attempt.seq
        assert attempt.outcome["action_id"] == action.id
    assert resolve_task_definition(engine, task).format == 3


async def test_an_action_refuses_a_step_its_grant_does_not_cover(
    tmp_root, engine, ships, config, monkeypatch, runner
):
    """A step is not a grant. Being at one proves only where the run is."""
    _project, task = _make_project_and_task(engine, tmp_root, ending="commit")
    _setup_git_clone(tmp_root / "cover-origin.git", Path(task.clone_path))
    _wrapper, fingerprint = _setup_signing_gpg(tmp_root / "bin", monkeypatch)
    monkeypatch.setattr(ships._gpg, "probe", _ready_gpg_probe(fingerprint))
    await _approve_current(config, engine, task)

    preview = await ships.preview(
        task,
        **_gate(engine, task),
        commit_message="m",
        pr_title="",
        pr_body="",
        request_id="r1",
    )
    await ships.confirm(task, preview, runner=runner)
    from ompire_daemon.registry.workflows import list_step_records

    seq = list_step_records(engine, task.id)[-1].seq

    # The run is at the commit action. Asking for a push there is refused
    # before anything is prepared, let alone written.
    with pytest.raises(DeliveryBlockedError) as excinfo:
        await ships.perform_action(
            task,
            action="push",
            workflow_seq=seq,
            request_id="r2",
            settle=lambda *_args: None,
        )
    assert [b.code for b in excinfo.value.blockers] == ["action-mismatch"]

    # And an attempt number nobody is at is refused just as plainly.
    with pytest.raises(DeliveryBlockedError) as excinfo:
        await ships.perform_action(
            task,
            action="commit",
            workflow_seq=seq + 7,
            request_id="r3",
            settle=lambda *_args: None,
        )
    assert [b.code for b in excinfo.value.blockers] == ["not-at-action"]
    assert get_latest_delivery(engine, task.id).actions == []


# --- no alternate authority path (format 3) ----------------------------------


async def test_no_manual_writer_is_admitted_while_a_decision_is_pending(
    tmp_root, engine, ships, config
):
    """An approval wait holds no lock, and must still exclude writers.

    A turn started here would change the content the decision is about, and
    the decision would then be about something that no longer exists. The
    refusal points at the way forward the author declared.
    """
    from ompire_daemon.runauthority import writer_refusal

    _project, task = _make_project_and_task(engine, tmp_root, ending="commit")
    _setup_git_clone(tmp_root / "writer-origin.git", Path(task.clone_path))
    assert writer_refusal(engine, get_task(engine, task.id)) is None

    await _approve_current(config, engine, task)
    refusal = writer_refusal(engine, get_task(engine, task.id))
    assert refusal is not None
    assert refusal[0] == "awaiting-approval"
    assert "request changes" in refusal[1]


async def test_a_declaring_workflow_does_not_draft_through_an_agent(
    tmp_root, engine, ships, config
):
    """No hidden turn at the approval, in either direction.

    The old Ship page asked the primary session for publication text. A
    format-3 run gets that text from its gate's own declared metadata, or from
    the operator typing it — both inert. Editing by hand stays available.
    """
    from ompire_daemon.ship import DraftNotDeclaredError

    _project, task = _make_project_and_task(engine, tmp_root, ending="commit")
    _setup_git_clone(tmp_root / "draft-origin.git", Path(task.clone_path))
    await _approve_current(config, engine, task)

    with pytest.raises(DraftNotDeclaredError):
        await ships.draft(task)

    projection = ships.save_manual_draft(
        task,
        {"commit_message": "typed by hand", "pr_title": "t", "pr_body": "b"},
    )
    assert projection["draft"]["commit_message"] == "typed by hand"
    assert projection["draft"]["source"] == "operator"


async def test_a_declaring_workflow_reviews_only_at_its_review_step(
    tmp_root, engine, ships, config, hub, sessions, agents, guard
):
    """Review is where the workflow says, for a direct caller too."""
    from ompire_daemon.review import ReviewManager, ReviewNotEligibleError

    _project, task = _make_project_and_task(engine, tmp_root, ending="commit")
    _setup_git_clone(tmp_root / "reviewstep-origin.git", Path(task.clone_path))
    await _approve_current(config, engine, task)
    reviews = ReviewManager(config, engine, hub, sessions, agents, guard)

    with pytest.raises(ReviewNotEligibleError) as excinfo:
        await reviews.start_review(task)
    assert excinfo.value.code == "not-at-review"


# --- interrupted delivery, adopted or continued ------------------------------


def _restored_sign(ships):
    """The manager's real signing implementation, unpatched."""
    return ShipManager._sign.__get__(ships, ShipManager)


def _live_runner(engine, config, hub, sessions, ships):
    """A real runner wired to the trusted services, as app startup wires it."""
    live = WorkflowRunner(
        engine, config, hub, AgentSupervisor(config, hub, sessions), sessions
    )
    live.set_guard(ships._guard)
    live.set_operations(None, ships)
    return live


async def _settle(engine, task_id, statuses, timeout=30.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        task = get_task(engine, task_id)
        if task.workflow_status in statuses:
            return task
        await asyncio.sleep(0.02)
    return get_task(engine, task_id)


async def test_a_restart_adopts_a_completed_effect_instead_of_repeating_it(
    tmp_root, engine, ships, config, hub, sessions, monkeypatch
):
    """The crash window between the journal write and the step transition.

    The commit really happened. A restart that re-ran the step would sign a
    second time, so recovery attaches the recorded result to the attempt that
    asked for it and lets the run continue from there.
    """
    _project, task = _make_project_and_task(engine, tmp_root, ending="commit")
    _setup_git_clone(tmp_root / "adoptstep-origin.git", Path(task.clone_path))
    _wrapper, fingerprint = _setup_signing_gpg(tmp_root / "bin", monkeypatch)
    monkeypatch.setattr(ships._gpg, "probe", _ready_gpg_probe(fingerprint))
    await _approve_current(config, engine, task)

    live = _live_runner(engine, config, hub, sessions, ships)
    # Land the journal result, then die before the step can be told.
    original_land = ships._land

    def land_then_die(action_id, *, result, identity=None, disposition=None, settle=None):
        original_land(
            action_id, result=result, identity=identity, disposition=disposition
        )
        raise asyncio.CancelledError

    monkeypatch.setattr(ships, "_land", land_then_die)
    try:
        preview = await ships.preview(
            task,
            **_gate(engine, task),
            commit_message="ship: it",
            pr_title="",
            pr_body="",
            request_id="r1",
        )
        await ships.confirm(task, preview, runner=live)
        await _settle(engine, task.id, {"complete", "failed", "waiting"})
    finally:
        await live.shutdown()

    record = get_latest_delivery(engine, task.id)
    assert record.succeeded("commit") is not None
    signed_tip = record.succeeded("commit").result["signed_tip"]

    # Next startup: nothing may sign again.
    monkeypatch.setattr(ships, "_land", original_land)

    async def must_not_sign(*args, **kwargs):
        raise AssertionError("recovery must not sign")

    monkeypatch.setattr(ships, "_sign", must_not_sign)
    restarted = _live_runner(engine, config, hub, sessions, ships)
    try:
        restarted.recover_run(
            get_task(engine, task.id),
            resolve_task_definition(engine, get_task(engine, task.id)),
        )
        finished = await _settle(engine, task.id, {"complete", "failed"})
    finally:
        await restarted.shutdown()

    assert finished.workflow_status == "complete"
    assert finished.workflow_result == "published"
    actions = [a for a in get_latest_delivery(engine, task.id).actions]
    assert len(actions) == 1
    step = next(
        r for r in list_step_records(engine, task.id) if r.step == "commit"
    )
    assert step.status == "ok"
    assert step.outcome["result"]["signed_tip"] == signed_tip


async def test_an_interrupted_action_waits_for_an_explicit_continuation(
    tmp_root, engine, ships, config, hub, sessions, monkeypatch
):
    """A restart before the effect neither performs it nor forgets the grant."""
    _project, task = _make_project_and_task(engine, tmp_root, ending="commit")
    _setup_git_clone(tmp_root / "continue-origin.git", Path(task.clone_path))
    _wrapper, fingerprint = _setup_signing_gpg(tmp_root / "bin", monkeypatch)
    monkeypatch.setattr(ships._gpg, "probe", _ready_gpg_probe(fingerprint))
    await _approve_current(config, engine, task)

    live = _live_runner(engine, config, hub, sessions, ships)
    async def die_before_signing(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(ships, "_sign", die_before_signing)
    try:
        preview = await ships.preview(
            task,
            **_gate(engine, task),
            commit_message="ship: it",
            pr_title="",
            pr_body="",
            request_id="r1",
        )
        await ships.confirm(task, preview, runner=live)
        await _settle(engine, task.id, {"complete", "failed", "waiting"})
    finally:
        await live.shutdown()

    # Startup proves the attempt produced nothing, then the run waits.
    signings: list[tuple] = []

    async def must_not_sign(*args, **kwargs):
        signings.append(args)
        raise AssertionError("recovery must not sign")

    monkeypatch.setattr(ships, "_sign", must_not_sign)
    assert await ships.restore() == []
    assert signings == []

    restarted = _live_runner(engine, config, hub, sessions, ships)
    try:
        restarted.recover_run(
            get_task(engine, task.id),
            resolve_task_definition(engine, get_task(engine, task.id)),
        )
        waiting = await _settle(engine, task.id, {"waiting", "complete", "failed"})
        assert waiting.workflow_status == "waiting"
        record = list_step_records(engine, task.id)[-1]
        assert record.step == "commit"
        assert record.pause["reason"] == "delivery_continuation"
        assert signings == []

        # The operator confirms the remaining work; the same attempt resumes
        # against the same delivery, and no second one is opened.
        monkeypatch.setattr(ships, "_sign", _restored_sign(ships))
        current = get_task(engine, task.id)
        again = await ships.preview(
            task,
            commit_message="ship: it",
            pr_title="",
            pr_body="",
            request_id="r2",
        )
        assert again.source == "workflow-action"
        await ships.confirm(current, again, runner=restarted)
        finished = await _settle(engine, task.id, {"complete", "failed"})

    finally:
        await restarted.shutdown()

    assert finished.workflow_status == "complete"
    record = get_latest_delivery(engine, task.id)
    assert record.disposition == "completed"
    # Two attempts at the same step, and exactly one effect: the interrupted
    # one stays on record as proven-not-to-have-happened, and the continuation
    # is what actually signed.
    attempts = [a for a in record.actions if a.kind == "commit"]
    assert [a.phase for a in attempts] == ["failed", "succeeded"]
    assert {a.workflow_seq for a in attempts} == {
        next(r for r in list_step_records(engine, task.id) if r.step == "commit").seq
    }


# --- the non-publishable handoff boundary (ADR-0035) -------------------------


def _attached_task(engine, tmp_root, *paths: str, ending: str = "pr", mode: str = "squash"):
    """A project and a task pinned to handoff destinations at `paths`."""
    from tests.conftest import make_result_attachment

    checkout_dir = tmp_root / "proj" / "myproject"
    checkout_dir.mkdir(parents=True, exist_ok=True)
    project = create_project(
        engine,
        name="myproject",
        title="My Project",
        upstream_url="https://github.com/owner/repo",
        fork_url=None,
        checkout_path=str(checkout_dir),
        default_checkout_root=tmp_root / "proj",
    )
    clone_path = tmp_root / "tasks" / "myproject" / "task-1"
    clone_path.parent.mkdir(parents=True, exist_ok=True)
    revision = install_delivery_workflow(engine, ending=ending, mode=mode)
    task = create_task(
        engine,
        project_name="myproject",
        slug="task-1",
        branch="ompire/task-1",
        clone_path=str(clone_path),
        prompt="do the thing",
        workflow_name=revision.name,
        execution_inputs=make_execution_inputs(
            checkout_path=str(checkout_dir),
            project_name="myproject",
            branch="ompire/task-1",
            upstream_url="https://github.com/owner/repo",
            workflow_name=revision.name,
            revision=revision,
            result_attachments=(make_result_attachment(*paths),),
        ),
    )
    return project, task


async def test_an_ordinary_task_captures_exactly_the_identity_it_always_did(
    tmp_root, engine, config
):
    """The regression that protects every reviewed candidate in the wild: a
    task with no handoff inputs has an empty protected set, so nothing is
    folded into its identity and nothing already approved goes stale."""
    _project, task = _make_project_and_task(engine, tmp_root)
    _setup_git_clone(tmp_root / "plain-origin.git", Path(task.clone_path))

    candidate = await capture_candidate(config, engine, task, base_branch="main")
    assert candidate.candidate_id == compute_candidate_id(
        task_id=task.id,
        base_commit=candidate.base_commit,
        tree_id=candidate.tree_id,
        source_commits=candidate.source_commits,
    )


async def test_a_task_with_handoff_inputs_binds_them_into_its_candidate_identity(
    tmp_root, engine, config
):
    """A candidate is bound to the publication policy it was reviewed under, so
    it cannot be carried into a delivery that believes the policy differs."""
    _project, task = _attached_task(engine, tmp_root, "epics/demo/PLAN.md")
    _setup_git_clone(tmp_root / "attached-origin.git", Path(task.clone_path))

    candidate = await capture_candidate(config, engine, task, base_branch="main")
    unbound = compute_candidate_id(
        task_id=task.id,
        base_commit=candidate.base_commit,
        tree_id=candidate.tree_id,
        source_commits=candidate.source_commits,
    )
    assert candidate.candidate_id != unbound


async def test_capture_refuses_a_proposed_tree_carrying_a_handoff_input(
    tmp_root, engine, config
):
    """Mode-neutral, and before anything is retained or reviewed — which is
    what stops contamination hiding behind a successful content review."""
    _project, task = _attached_task(engine, tmp_root, "epics/demo/PLAN.md")
    clone = Path(task.clone_path)
    _setup_git_clone(tmp_root / "contaminated-origin.git", clone)
    (clone / "epics" / "demo").mkdir(parents=True)
    (clone / "epics" / "demo" / "PLAN.md").write_text("# Plan\n", encoding="utf-8")
    _run_git(clone, "add", "-f", "epics/demo/PLAN.md")
    _run_git(clone, "commit", "-m", "oops, committed the plan")

    with pytest.raises(ProtectedPathError) as excinfo:
        await capture_candidate(config, engine, task, base_branch="main")
    assert "epics/demo/PLAN.md" in str(excinfo.value)
    # Named, not repaired: the file is still exactly where the operator left it.
    assert (clone / "epics" / "demo" / "PLAN.md").exists()


async def test_an_uncommitted_handoff_file_does_not_block_ordinary_code(
    tmp_root, engine, config
):
    """The everyday case. The installed files sit in the clone, excluded from
    staging, while the task's real work ships normally."""
    _project, task = _attached_task(engine, tmp_root, "epics/demo/PLAN.md")
    clone = Path(task.clone_path)
    _setup_git_clone(tmp_root / "clean-origin.git", clone)
    (clone / "epics" / "demo").mkdir(parents=True)
    (clone / "epics" / "demo" / "PLAN.md").write_text("# Plan\n", encoding="utf-8")

    candidate = await capture_candidate(config, engine, task, base_branch="main")
    assert candidate.candidate_id


async def test_a_handoff_file_replaced_by_a_directory_is_still_refused(
    tmp_root, engine, config
):
    """Protection covers the path's namespace. Recursing is what catches a
    protected file that became a directory full of the same content."""
    _project, task = _attached_task(engine, tmp_root, "epics/demo/PLAN.md")
    clone = Path(task.clone_path)
    _setup_git_clone(tmp_root / "dir-origin.git", clone)
    (clone / "epics" / "demo" / "PLAN.md").mkdir(parents=True)
    (clone / "epics" / "demo" / "PLAN.md" / "part.md").write_text("# Plan\n", encoding="utf-8")
    _run_git(clone, "add", "-f", "epics/demo/PLAN.md")
    _run_git(clone, "commit", "-m", "split the plan up")

    with pytest.raises(ProtectedPathError):
        await capture_candidate(config, engine, task, base_branch="main")


async def test_a_handoff_path_with_git_metacharacters_is_matched_literally(
    tmp_root, engine, config
):
    """`[1]` in a filename is punctuation, not a character class. Reading it as
    a pattern would protect the wrong paths and leave the real one open."""
    _project, task = _attached_task(engine, tmp_root, "epics/plan[1].md")
    clone = Path(task.clone_path)
    _setup_git_clone(tmp_root / "meta-origin.git", clone)
    (clone / "epics").mkdir(parents=True)
    (clone / "epics" / "plan[1].md").write_text("# Plan\n", encoding="utf-8")
    _run_git(clone, "add", "-f", "--", "epics/plan[1].md")
    _run_git(clone, "commit", "-m", "committed the plan")

    with pytest.raises(ProtectedPathError) as excinfo:
        await capture_candidate(config, engine, task, base_branch="main")
    assert "epics/plan[1].md" in str(excinfo.value)


async def test_a_handoff_path_tracked_on_the_base_refuses_rather_than_publishing_it(
    tmp_root, engine, config
):
    """An upstream-tracked file at a protected destination would be published
    implicitly, without this task having done anything at all."""
    _project, task = _attached_task(engine, tmp_root, "docs/PLAN.md")
    clone = Path(task.clone_path)
    _setup_git_clone(tmp_root / "base-origin.git", clone)
    # On `main`, and then in the task branch's own ancestry: the merge-base is
    # what a candidate is captured against, so that is where the collision has
    # to be for this refusal to be about the right tree.
    _run_git(clone, "add", "-A")
    _run_git(clone, "commit", "-m", "settle the working tree")
    _run_git(clone, "checkout", "main")
    (clone / "docs").mkdir(parents=True)
    (clone / "docs" / "PLAN.md").write_text("upstream plan\n", encoding="utf-8")
    _run_git(clone, "add", ".")
    _run_git(clone, "commit", "-m", "upstream tracks a plan")
    _run_git(clone, "push", "origin", "HEAD:main")
    _run_git(clone, "checkout", "ompire/task-1")
    _run_git(clone, "rebase", "main")

    with pytest.raises(ProtectedPathError):
        await capture_candidate(config, engine, task, base_branch="main")


async def test_retain_refuses_a_checkpoint_that_carried_a_handoff_input(
    tmp_root, engine, ships, config
):
    """The failure a final-tree check cannot see.

    The file was added in one commit and deleted in the next, so the tree that
    would be published is clean — and, in retain mode, the commit that still
    contains it is published anyway. Squash of the same history is fine,
    because those checkpoints are not published at all.
    """
    _project, task = _attached_task(
        engine, tmp_root, "epics/demo/PLAN.md", ending="commit", mode="retain"
    )
    clone = Path(task.clone_path)
    _setup_git_clone(tmp_root / "retain-origin.git", clone)
    _run_git(clone, "add", "-A")
    _run_git(clone, "commit", "-m", "settle the working tree")
    (clone / "epics" / "demo").mkdir(parents=True)
    (clone / "epics" / "demo" / "PLAN.md").write_text("# Plan\n", encoding="utf-8")
    _run_git(clone, "add", "-f", "epics/demo/PLAN.md")
    _run_git(clone, "commit", "-m", "checkpoint with the plan")
    _run_git(clone, "rm", "-q", "epics/demo/PLAN.md")
    _run_git(clone, "commit", "-m", "remove the plan again")

    # The final tree is clean, so capture and review both pass.
    candidate = await capture_candidate(config, engine, task, base_branch="main")
    gate_seq = park_at_delivery_gate(
        engine, task, candidate_id=candidate.candidate_id
    )
    clear_process_marker(engine, task.id)

    preview = await ships.preview(
        task,
        gate_seq=gate_seq,
        choice_id="publish",
        commit_message="m",
        pr_title="t",
        pr_body="b",
        request_id="r1",
    )
    codes = [blocker.code for blocker in preview.blockers]
    assert "retain-protected-paths" in codes
    message = next(
        b.message for b in preview.blockers if b.code == "retain-protected-paths"
    )
    assert "epics/demo/PLAN.md" in message
    # The offending commit is named, and nothing was rewritten to hide it.
    assert "in the range this delivery would publish" in message
    assert not preview.deliverable
