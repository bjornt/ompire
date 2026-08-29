import os
import sys
from dataclasses import asdict
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ompire_daemon.app import create_app
from ompire_daemon.config import Config
from ompire_daemon.registry.templates import create_template

FAKE_OMP = Path(__file__).parent / "fake_omp.py"

# Answers the daemon's two in-container invocations so REST-spawned pipelines
# run end-to-end against the fake omp: the ask-timeout preflight gets `0`,
# the rpc-ui spawn execs fake_omp, anything else (info/remove) succeeds.
FAKE_WORKSHOP_SCRIPT = f"""#!/bin/sh
case "$*" in
  *"config get ask.timeout"*) echo 0 ;;
  *"--mode rpc-ui"*) exec {sys.executable} -u {FAKE_OMP} happy ;;
  *) exit 0 ;;
esac
"""

FAKE_GH_SCRIPT = """#!/bin/sh
case "$*" in
  "--version") echo "gh version 2.97.0 (test)" ;;
  "api --hostname github.com user") echo '{"login":"test-user"}' ;;
  "api --hostname github.com repos/"*"/pulls?per_page=1") echo '[]' ;;
  "api --hostname github.com repos/"*) echo '{"archived":false,"disabled":false,"has_issues":true,"pull_request_creation_policy":"all"}' ;;
  "pr create"*) echo "https://github.com/owner/repo/pull/1" ;;
  "pr view"*) echo '{"state":"OPEN","mergedAt":null}' ;;
  *) echo "unsupported gh invocation: $*" >&2; exit 1 ;;
esac
"""


@pytest.fixture(autouse=True)
def fake_workshop_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Shadow any real `workshop` binary with a fake on PATH.

    Defaults to success for every subcommand and speaks fake omp for agent
    spawns; tests overwrite the script to exercise absent/error paths.
    Autouse so no test can ever touch real containers.
    """
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    script = bin_dir / "workshop"
    script.write_text(FAKE_WORKSHOP_SCRIPT)
    script.chmod(0o755)
    gh_script = bin_dir / "gh"
    gh_script.write_text(FAKE_GH_SCRIPT)
    gh_script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return script


@pytest.fixture
def daemon_config(tmp_path: Path) -> Config:
    # A fake my-workshop so pipelines can complete without real containers;
    # individual tests override my_workshop_command to exercise failure modes.
    fake_my_workshop = tmp_path / "fake-my-workshop"
    fake_my_workshop.write_text('#!/bin/sh\necho "ws-test" > .workshop.lock\n')
    fake_my_workshop.chmod(0o755)
    return Config(
        data_dir=tmp_path / "data",
        task_dir_root=tmp_path / "tasks",
        checkout_root=tmp_path / "proj",
        my_workshop_command=(str(fake_my_workshop),),
        # Fast turn boundaries so idle transitions land within test budgets.
        session_idle_debounce=0.1,
    )


@pytest.fixture
def app(daemon_config: Config, tmp_path: Path):
    # Point at a nonexistent dist so tests don't depend on a real frontend build.
    return create_app(daemon_config, frontend_dist=tmp_path / "no-dist")


@pytest.fixture
def auth_token(app) -> str:
    return app.state.auth_token


@pytest.fixture
def client(app) -> TestClient:
    # Context-managed so one event loop lives for the whole test: background
    # jobs started by request handlers (the spawn pipeline) keep running.
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth_headers(auth_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth_token}"}


def make_adoptable_checkout(
    root: Path, name: str, *, remote: str = "origin", commit: bool = True
) -> Path:
    """A real work tree at `root/name` that adoption accepts.

    Registration validates the checkout now (ADR-0022), so a test that only
    cares about registry or event semantics still needs a genuine repository
    at the path it registers.
    """
    import subprocess

    def git(*args: str, cwd: Path) -> None:
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
        )

    checkout = root / name
    if checkout.exists():
        return checkout
    checkout.mkdir(parents=True)
    git("init", "--initial-branch=main", ".", cwd=checkout)
    if commit:
        (checkout / "README.md").write_text(f"{name}\n")
        git("add", "README.md", cwd=checkout)
        git("commit", "-m", "initial", cwd=checkout)
    git("remote", "add", remote, f"https://example.com/{name}.git", cwd=checkout)
    return checkout


@pytest.fixture
def make_checkout(daemon_config: Config):
    """Factory for `make_adoptable_checkout` bound to the config's root."""

    def _make(name: str = "demo", **kwargs) -> Path:
        return make_adoptable_checkout(daemon_config.checkout_root, name, **kwargs)

    return _make


@pytest.fixture
def git_checkout(tmp_path: Path) -> Path:
    """A real project checkout with an `origin` remote and a committed `main`."""
    import subprocess

    def git(*args: str, cwd: Path) -> None:
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
        )

    upstream = tmp_path / "upstream.git"
    upstream.mkdir()
    git("init", "--bare", "--initial-branch=main", ".", cwd=upstream)

    checkout = tmp_path / "proj" / "demo"
    checkout.mkdir(parents=True)
    git("init", "--initial-branch=main", ".", cwd=checkout)
    (checkout / "README.md").write_text("demo\n")
    git("add", "README.md", cwd=checkout)
    git("commit", "-m", "initial", cwd=checkout)
    git("remote", "add", "origin", str(upstream), cwd=checkout)
    git("push", "origin", "main", cwd=checkout)
    return checkout


@pytest.fixture
def demo_template(
    client: TestClient, auth_headers: dict[str, str], git_checkout: Path
) -> dict:
    """Project `demo` on the git checkout plus a same-named template — the
    minimum a REST spawn needs (mirrors git_checkout's fixture style)."""
    response = client.post(
        "/api/projects",
        headers=auth_headers,
        json={
            "name": "demo",
            "title": "Demo",
            "upstream_url": "https://example.com/demo.git",
            "checkout_path": str(git_checkout),
        },
    )
    assert response.status_code == 201
    template = create_template(
        client.app.state.engine,
        name="demo",
        project_name="demo",
        branch_pattern="ompire/<slug>",
    )
    return asdict(template)
