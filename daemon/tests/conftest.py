import os
import sys
from dataclasses import asdict
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Captured before any test can monkeypatch the module attribute, so a fake
# argv builder always composes the *real* native flags.
from ompire_daemon.agent import build_agent_argv as REAL_BUILD_AGENT_ARGV
from ompire_daemon.app import create_app
from ompire_daemon.config import Config
from ompire_daemon.registry.model_profiles import create_model_profile

FAKE_OMP = Path(__file__).parent / "fake_omp.py"

# Answers the daemon's two in-container invocations so REST-spawned pipelines
# run end-to-end against the fake omp: the ask-timeout preflight gets `0`,
# the rpc-ui spawn execs fake_omp, anything else (info/remove) succeeds.
# The whole argv is forwarded so fake omp parses the real native model flags
# and reports them back through `get_state` — the daemon verifies the child's
# active model before prompting, so a fake that ignored the flags would make
# every spawn fail.
FAKE_WORKSHOP_SCRIPT = f"""#!/bin/sh
case "$*" in
  *"config get ask.timeout"*) echo 0 ;;
  *"--mode rpc-ui"*) exec {sys.executable} -u {FAKE_OMP} happy "$@" ;;
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


# The four-role map every test profile uses. Concrete, provider-qualified,
# and structurally valid; no test ever reaches a provider with it.
TEST_ROLES = {
    "default": {"model": "testing/main-model", "thinking": "medium"},
    "smol": {"model": "testing/smol-model", "thinking": "low"},
    "slow": {"model": "testing/slow-model", "thinking": "high"},
    "plan": {"model": "testing/plan-model", "thinking": "xhigh"},
}


@pytest.fixture
def demo_profile(client: TestClient) -> dict:
    """A complete global model profile named `demo` (ADR-0025)."""
    return asdict(
        create_model_profile(client.app.state.engine, name="demo", roles=TEST_ROLES)
    )


@pytest.fixture
def demo_project(
    client: TestClient,
    auth_headers: dict[str, str],
    git_checkout: Path,
    demo_profile: dict,
) -> dict:
    """Project `demo` on the git checkout, defaulting to the `demo` profile —
    the minimum a launch needs now that templates are gone (ADR-0026)."""
    response = client.post(
        "/api/projects",
        headers=auth_headers,
        json={
            "name": "demo",
            "title": "Demo",
            "upstream_url": "https://example.com/demo.git",
            "checkout_path": str(git_checkout),
            "default_model_profile": "demo",
        },
    )
    assert response.status_code == 201
    return response.json()


def launch_body(
    slug: str = "fix-bug",
    prompt: str = "do the thing",
    *,
    project_name: str = "demo",
    workflow_name: str = "single-step",
    **extra,
) -> dict:
    body = {
        "project_name": project_name,
        "workflow_name": workflow_name,
        "slug": slug,
        "prompt": prompt,
    }
    body.update(extra)
    return body


def spawn_task(
    client: TestClient, auth_headers: dict[str, str], **kwargs
) -> "object":
    """Preview then accept, the way the UI does. Returns the POST response so
    a test can assert on status and body."""
    body = launch_body(**kwargs)
    preview = client.post("/api/tasks/preview", headers=auth_headers, json=body)
    assert preview.status_code == 200, preview.text
    return client.post(
        "/api/tasks",
        headers=auth_headers,
        json={**body, "preview_token": preview.json()["preview_token"]},
    )


def make_execution_inputs(
    *,
    checkout_path: str,
    project_name: str = "demo",
    workflow_name: str = "single-step",
    model_profile: str | None = "demo",
    roles: dict | None = None,
    base_branch: str = "main",
    branch_pattern: str = "ompire/<slug>",
    workshop_additions: str = "project",
    preamble: str = "",
    branch: str = "ompire/fix-bug",
    fetch_remote: str = "origin",
    upstream_url: str = "https://example.com/demo.git",
    fork_url: str | None = None,
):
    """A complete accepted-input document for tests that build a task row
    directly instead of going through preview/accept."""
    from ompire_daemon.execution_inputs import (
        PROFILE_SOURCE_PROJECT,
        PROVENANCE_ACCEPTED,
        TaskExecutionInputs,
        WorkspaceInputs,
    )
    from ompire_daemon.model_config import JUDGE_ROLE
    from ompire_daemon.registry.model_profiles import RoleBinding
    from ompire_daemon.workflows import agent_step_roles, get_workflow

    source = roles or TEST_ROLES
    return TaskExecutionInputs(
        provenance=PROVENANCE_ACCEPTED,
        accepted_at="2026-09-05T00:00:00+00:00",
        project_name=project_name,
        workflow_name=workflow_name,
        model_profile_name=model_profile,
        model_profile_source=PROFILE_SOURCE_PROJECT,
        roles={
            role: RoleBinding(model=binding["model"], thinking=binding["thinking"])
            for role, binding in source.items()
        },
        step_roles=agent_step_roles(get_workflow(workflow_name)),
        judge_role=JUDGE_ROLE,
        workspace=WorkspaceInputs(
            base_branch=base_branch,
            branch_pattern=branch_pattern,
            workshop_additions=workshop_additions,
            preamble=preamble,
        ),
        workspace_overrides=(),
        branch=branch,
        checkout_path=checkout_path,
        fetch_remote=fetch_remote,
        upstream_url=upstream_url,
        fork_url=fork_url,
    )


def make_test_policy(**overrides):
    """The native model policy tests start agents with. Explicit everywhere:
    a supervisor start has no "no policy" case any more (ADR-0026)."""
    from ompire_daemon.execution_inputs import ModelPolicy
    from ompire_daemon.registry.model_profiles import RoleBinding

    roles = {**TEST_ROLES, **overrides}
    return ModelPolicy.from_roles(
        {
            role: RoleBinding(model=binding["model"], thinking=binding["thinking"])
            for role, binding in roles.items()
        }
    )



def fake_argv_builder(scenario: dict | str = "happy"):
    """A `build_agent_argv` replacement that runs fake omp *with the real
    native model flags*.

    The supervisor reads the child's active model back and refuses to prompt
    unless it matches the accepted policy (ADR-0026), so a fake argv that
    dropped the flags would fail every session start for reasons that have
    nothing to do with what the test is about.
    """
    from tests.test_rpc import fake_omp_argv

    def build(clone, *, policy, resume=None):
        name = scenario["name"] if isinstance(scenario, dict) else scenario
        real = REAL_BUILD_AGENT_ARGV(clone, policy=policy, resume=resume)
        return fake_omp_argv(name, *real[real.index("--no-title") + 1 :])

    return build
