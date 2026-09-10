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
from ompire_daemon.work.profiles import create_model_profile

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
def isolated_revision_cache():
    """Every test starts with an empty decoded-revision cache.

    The cache is process-local and keyed by content identity (ADR-0028), so a
    revision one test decoded would otherwise resolve inside another test
    whose database has never heard of it — and a coexistence test would pass
    for the wrong reason. What a *name* means is no longer process-local: it
    lives in each test's own database (ADR-0031).
    """
    from ompire_daemon.registry.workflow_definitions import clear_cache

    clear_cache()
    yield
    clear_cache()


# The engine's baseline workflow for tests that are about something else.
#
# The packaged `single-step` declares review, an approval, and delivery — it
# is a complete procedure, which is the point of it. That makes it the wrong
# vehicle for a test about prompt bytes, session events, or a REST route: such
# a test would drive a reviewer subprocess and a publication decision it never
# meant to ask about. `plain` is one agent step and nothing else.
PLAIN_WORKFLOW_YAML = r"""
format: 1
name: plain
sessions: [main]
primary: main
steps:
  - name: work
    kind: agent
    session: main
    role: default
    expects_outcome: false
    prompt:
      separator: ""
      parts:
        - if:
            op: ne
            left: {op: input, name: task.prompt}
            right: {op: literal, value: ""}
          then:
            separator: ""
            parts:
              - if:
                  op: ne
                  left: {op: input, name: workspace.preamble}
                  right: {op: literal, value: ""}
                then:
                  separator: ""
                  parts:
                    - value: {op: input, name: workspace.preamble}
                      format: text
                    - text: "\n\n"
                else:
                  parts: []
              - value: {op: input, name: task.prompt}
                format: text
          else:
            parts: []
"""


def install_plain_workflow(engine):
    """Make `plain` launchable in this test's catalog."""
    return install_test_workflow(engine, PLAIN_WORKFLOW_YAML)


def register_builtin_workflows(engine):
    """Install the packaged definitions, exactly as daemon startup does.

    Tests that build an engine directly instead of going through `create_app`
    have to do this themselves: a launch cannot resolve a name the library
    does not hold, which is the behavior under test everywhere else.
    """
    from ompire_daemon.workflows import install_packaged_workflows

    return install_packaged_workflows(engine)


def install_test_workflow(engine, document: str, *, name=None):
    """Save one definition written for a single test as a custom entry.

    Goes through the same library operations the REST layer uses, so a test
    fixture cannot install something an operator could not have saved.
    """
    from ompire_daemon.registry.workflow_library import (
        WorkflowEntryNotFoundError,
        create_entry,
        get_detail,
        save_revision,
    )
    from ompire_daemon.workflow_definitions import load_definition

    revision = load_definition(document)
    entry_name = name or revision.name
    try:
        version = get_detail(engine, entry_name).entry.version
    except WorkflowEntryNotFoundError:
        version = create_entry(engine, name=entry_name, yaml_text=document).entry.version
    save_revision(
        engine,
        entry_name,
        revision=revision,
        yaml_text=document,
        expected_version=version,
    )
    return revision


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
    the minimum a launch needs now that templates are gone (ADR-0026).

    Also retains the `plain` engine baseline, which is what a launch selects
    unless a test names a packaged workflow: the packaged ones are complete
    procedures with review and delivery in them, and most REST tests are about
    something else entirely.
    """
    install_plain_workflow(client.app.state.engine)
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
    workflow_name: str = "plain",
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
    if body["workflow_name"] == "plain":
        install_plain_workflow(client.app.state.engine)
    preview = client.post("/api/tasks/preview", headers=auth_headers, json=body)
    assert preview.status_code == 200, preview.text
    return client.post(
        "/api/tasks",
        headers=auth_headers,
        json={**body, "preview_token": preview.json()["preview_token"]},
    )


def make_result_attachment(*paths: str, result_id: str = "res_test", body: bytes = b"# Plan\n"):
    """One pinned handoff attachment, for a task built directly rather than
    through preview/accept. The classification is the fixed handoff value —
    there is no other legal one (ADR-0035)."""
    import hashlib

    from ompire_daemon.work.inputs import (
        HANDOFF_CLASSIFICATION,
        AttachedFile,
        ResultAttachment,
    )

    return ResultAttachment(
        result_id=result_id,
        producer_task_id=99,
        manifest_id=f"manifest-{result_id}",
        content_id=None,
        accepted_at="2026-09-09T00:00:00+00:00",
        manifest_project_name="myproject",
        files=tuple(
            AttachedFile(
                path=path,
                length=len(body),
                sha256=hashlib.sha256(body).hexdigest(),
                media_type="text/markdown",
            )
            for path in paths
        ),
        provenance={},
        classification=HANDOFF_CLASSIFICATION,
    )


def make_execution_inputs(
    *,
    checkout_path: str,
    project_name: str = "demo",
    workflow_name: str = "plain",
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
    step_roles: dict | None = None,
    step_profile_names: dict | None = None,
    step_profiles: dict | None = None,
    result_attachments: tuple = (),
    source_commit: str | None = None,
    revision=None,
    engine=None,
):
    """A complete accepted-input document for tests that build a task row
    directly instead of going through preview/accept.

    `step_roles` and `step_profile_names` express per-consumer overrides the
    way a launch would: a step named in either gets `step` attribution on
    that dimension. `step_profiles` supplies the role maps those named
    profiles bind, so a test can give two steps genuinely different models.

    `revision` pins the definition. Left out, it is the library's current
    choice for `workflow_name` when an `engine` is supplied, and the packaged
    definition otherwise — either way, the same thing acceptance would have
    pinned. Pass a revision explicitly to build a task on a definition that is
    *not* the library's current one, which is how coexistence across an edit
    is exercised.
    """
    from ompire_daemon.model_config import RoleBinding
    from ompire_daemon.registry.workflow_library import resolve_current
    from ompire_daemon.work.inputs import (
        PROFILE_SOURCE_PROJECT,
        PROVENANCE_ACCEPTED,
        ROLE_SOURCE_WORKFLOW,
        WORKFLOW_SOURCE_ACCEPTED,
        ConsumerBinding,
        TaskExecutionInputs,
        WorkflowBinding,
        WorkspaceInputs,
    )
    from ompire_daemon.workflows import load_packaged_workflows

    if engine is not None and workflow_name == "plain" and revision is None:
        # Retained on demand: a task pinned to the engine baseline has to be
        # able to resolve it, and only the caller knows which database that is.
        install_plain_workflow(engine)
    if revision is not None:
        pinned = revision
    elif engine is not None:
        with engine.connect() as conn:
            pinned = resolve_current(conn, workflow_name)
    elif workflow_name == "plain":
        # The engine baseline is a test document, not a packaged one, so it
        # is loaded from its own source rather than from the catalog.
        from ompire_daemon.workflow_definitions import load_definition

        pinned = load_definition(PLAIN_WORKFLOW_YAML)
    else:
        pinned = load_packaged_workflows()[workflow_name]

    source = roles or TEST_ROLES
    decoded = {
        role: RoleBinding(model=binding["model"], thinking=binding["thinking"])
        for role, binding in source.items()
    }

    def binding(role, *, profile=None, profile_source=None, role_source=None):
        return ConsumerBinding(
            profile_name=profile if profile is not None else model_profile,
            profile_source=profile_source or PROFILE_SOURCE_PROJECT,
            role=role,
            role_source=role_source or ROLE_SOURCE_WORKFLOW,
            roles=dict(bindings_for(profile)),
        )

    def bindings_for(profile):
        if profile is None or profile == model_profile:
            return decoded
        return (step_profiles or {})[profile]

    return TaskExecutionInputs(
        result_attachments=tuple(result_attachments),
        source_commit=source_commit,
        provenance=PROVENANCE_ACCEPTED,
        accepted_at="2026-09-05T00:00:00+00:00",
        project_name=project_name,
        workflow_name=pinned.name,
        workflow_binding=WorkflowBinding(
            revision=pinned.revision,
            source=WORKFLOW_SOURCE_ACCEPTED,
            bound_at="2026-09-05T00:00:00+00:00",
        ),
        model_profile_name=model_profile,
        model_profile_source=PROFILE_SOURCE_PROJECT,
        step_bindings={
            step.name: binding(
                (step_roles or {}).get(step.name, step.role),
                profile=(step_profile_names or {}).get(step.name),
                profile_source=(
                    "step" if step.name in (step_profile_names or {}) else None
                ),
                role_source="step" if step.name in (step_roles or {}) else None,
            )
            for step in pinned.definition.agent_steps()
        },
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
    from ompire_daemon.model_config import RoleBinding
    from ompire_daemon.work.inputs import ModelPolicy

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


# --- format-3 delivery fixtures -----------------------------------------------
# What a task that has reached its approval gate actually looks like: a pinned
# format-3 revision, an approved review bound to the candidate it graded, and
# a persisted question with the frozen evidence binding that ties the two
# together. Tests build this rather than a bare task, because a delivery has
# no meaning without the decision that would authorize it.


_DELIVERY_STEP_TEMPLATE = {
    "commit": (
        "  - name: commit\n    kind: delivery\n    action: commit\n"
        "    mode: {mode}\n    approval: approve\n    next: {next}\n"
    ),
    "push": (
        "  - name: push\n    kind: delivery\n    action: push\n"
        "    previous: commit\n    approval: approve\n    next: {next}\n"
    ),
    "pr": (
        "  - name: pr\n    kind: delivery\n    action: pr\n"
        "    previous: push\n    approval: approve\n    next: {next}\n"
    ),
}

_ENDING_CHAIN = {
    "commit": ("commit",),
    "push": ("commit", "push"),
    "pr": ("commit", "push", "pr"),
}


def delivery_workflow_yaml(
    *, name: str = "delivering", ending: str = "pr", mode: str = "squash"
) -> str:
    """A minimal format-3 workflow whose approval authorizes one chain."""
    chain = _ENDING_CHAIN[ending]
    steps = ""
    for index, action in enumerate(chain):
        following = (
            f"{{step: {chain[index + 1]}}}"
            if index + 1 < len(chain)
            else "{complete: true, result: published}"
        )
        steps += _DELIVERY_STEP_TEMPLATE[action].format(mode=mode, next=following)
    return f"""
format: 3
name: {name}
sessions: [main]
primary: main
steps:
  - name: work
    kind: agent
    session: main
    outcome: null
    prompt: {{parts: [{{text: "do it"}}]}}

  - name: review
    kind: review
    evidence:
      work: {{steps: [work], with_outcome: false}}

  - name: approve
    kind: gate
    evidence:
      verdict: {{steps: [review]}}
    delivery:
      review: verdict
    message: {{parts: [{{text: "Publish?"}}]}}
    choices:
      - id: publish
        label: Publish
        next: {{step: {chain[0]}}}
        authorize: {{steps: [{", ".join(chain)}]}}
      - id: finish
        label: Finish without publishing
        next: {{complete: true, result: done-unpublished}}

{steps}"""


def install_delivery_workflow(
    engine, *, name: str = "delivering", ending: str = "pr", mode: str = "squash"
):
    """Retain the workflow and return its revision, as a save would."""
    from ompire_daemon.workflow_definitions import load_definition

    document = delivery_workflow_yaml(name=name, ending=ending, mode=mode)
    install_test_workflow(engine, document, name=name)
    return load_definition(document)


def park_at_delivery_gate(
    engine,
    task,
    *,
    candidate_id: str | None,
    outcome: str = "approved",
    findings: str | None = "",
) -> int:
    """Drive the run's records to its approval, and return the question's seq.

    Writes exactly what the runner would have: a finished work attempt, a
    review attempt carrying its trusted verdict, and a parked gate whose
    frozen evidence names that review attempt. The binding is the point — an
    approval that named "the task's review" could be answered against a
    different one.
    """
    from ompire_daemon.registry.reviews import append_iteration, open_review
    from ompire_daemon.registry.workflows import (
        append_step_record,
        build_gate_snapshot,
        finish_step_record,
        park_gate,
    )
    from ompire_daemon.workflows import review_outcome

    work = append_step_record(
        engine, task.id, step="work", kind="agent", session="main", evidence=None
    )
    finish_step_record(engine, task.id, work.seq, status="ok", outcome=None)
    review = append_step_record(
        engine,
        task.id,
        step="review",
        kind="review",
        session=None,
        evidence={
            "version": 1,
            "bindings": {"work": {"step": "work", "seq": work.seq}},
        },
    )
    open_review(
        engine, task.id, candidate_id=candidate_id, workflow_seq=review.seq
    )
    iteration = append_iteration(
        engine,
        task.id,
        outcome=outcome,
        status=outcome if outcome != "comments" else None,
        candidate_id=candidate_id,
        workflow_seq=review.seq,
        findings=findings,
    )
    finish_step_record(
        engine,
        task.id,
        review.seq,
        status="ok",
        outcome=review_outcome(iteration),
    )
    gate = append_step_record(
        engine,
        task.id,
        step="approve",
        kind="gate",
        session=None,
        evidence={
            "version": 1,
            "bindings": {"verdict": {"step": "review", "seq": review.seq}},
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
                    "authorize": {"steps": list(_ENDING_CHAIN["pr"])},
                },
                {
                    "id": "finish",
                    "label": "Finish without publishing",
                    "feedback_required": False,
                    "next": {"complete": True, "result": "done-unpublished"},
                    "authorize": None,
                },
            ],
            evidence={"verdict": {"step": "review", "seq": review.seq}},
        ),
    )
    return gate.seq
