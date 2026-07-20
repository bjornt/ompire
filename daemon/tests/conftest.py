import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ompire_daemon.app import create_app
from ompire_daemon.config import Config

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
