"""Upgrade reconciliation: what an operator has to decide after the template
retirement, and what the daemon refuses to decide for them (ADR-0026).

These tests drive the REST surface against a database landed at 0012 with
real template rows, so they exercise the migration, the startup
initialization, and the confirmation endpoints as one path — the way an
actual upgrade runs.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from ompire_daemon.app import create_app
from ompire_daemon.config import Config
from ompire_daemon.db import db_path_for, make_engine
from tests.conftest import TEST_ROLES, launch_body

TEMPLATE_COLUMNS = (
    "name, project_name, base_branch, branch_pattern, workflow, "
    "workshop_additions, model, thinking, preamble, created_at, updated_at"
)


def _land_at_0012(config: Config) -> None:
    """Bring a database up to the last pre-retirement revision, so the test
    can seed genuine template rows before 0013 runs."""
    from alembic.config import Config as AlembicConfig

    from alembic import command

    db_path = db_path_for(config.data_dir)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    alembic_ini = Path(__file__).resolve().parent.parent / "alembic.ini"
    cfg = AlembicConfig(str(alembic_ini))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "0012")


def _seed(config: Config, checkout: Path, templates: list[dict]) -> None:
    engine = make_engine(db_path_for(config.data_dir))
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO projects (name, title, upstream_url, fork_url, "
                "checkout_path) VALUES ('demo', 'Demo', "
                "'https://example.com/demo.git', NULL, :checkout)"
            ),
            {"checkout": str(checkout)},
        )
        for values in templates:
            row = {
                "name": values["name"],
                "project_name": "demo",
                "base_branch": values.get("base_branch", "main"),
                "branch_pattern": values.get("branch_pattern", "ompire/<slug>"),
                "workflow": "single-step",
                "workshop_additions": values.get("workshop_additions", "project"),
                "model": values.get("model"),
                "thinking": values.get("thinking"),
                "preamble": values.get("preamble", ""),
                "created_at": "2026-08-01T00:00:00+00:00",
                "updated_at": "2026-08-01T00:00:00+00:00",
            }
            binds = ", ".join(f":{key}" for key in row)
            conn.execute(
                text(f"INSERT INTO templates ({TEMPLATE_COLUMNS}) VALUES ({binds})"),
                row,
            )
    engine.dispose()


@pytest.fixture
def upgraded(daemon_config: Config, git_checkout: Path):
    """Factory: seed templates at 0012, then start the daemon (which runs the
    0013 upgrade and the startup initialization) and hand back a client."""

    def build(templates: list[dict], *, config: Config | None = None):
        effective = config or daemon_config
        _land_at_0012(effective)
        _seed(effective, git_checkout, templates)
        app = create_app(effective, frontend_dist=effective.data_dir / "no-dist")
        from ompire_daemon.registry.model_profiles import create_model_profile

        create_model_profile(app.state.engine, name="chosen", roles=TEST_ROLES)
        client = TestClient(app)
        headers = {"Authorization": f"Bearer {app.state.auth_token}"}
        return app, client, headers

    return build


def test_conflicting_templates_block_launch_until_the_operator_decides(
    upgraded, git_checkout: Path
) -> None:
    _, client, headers = upgraded(
        [
            {"name": "a", "base_branch": "main", "preamble": ""},
            {"name": "b", "base_branch": "release", "preamble": "be careful"},
        ]
    )

    project = client.get("/api/projects/demo", headers=headers).json()
    assert project["launch_config_state"] == "needs-reconciliation"

    refused = client.post("/api/tasks/preview", headers=headers, json=launch_body())
    assert refused.status_code == 409
    assert "reconciliation" in refused.json()["detail"]

    evidence = client.get(
        "/api/projects/demo/launch-reconciliation", headers=headers
    ).json()
    assert evidence["needs_reconciliation"] is True
    # Every distinct old value, with the template it came from, and nothing
    # pre-selected.
    assert evidence["workspace_conflicts"]["base_branch"] == ["main", "release"]
    assert evidence["workspace_conflicts"]["preamble"] == ["", "be careful"]
    assert {t["source"] for t in evidence["source_templates"]} == {"a", "b"}

    confirmed = client.post(
        "/api/projects/demo/launch-reconciliation",
        headers=headers,
        json={
            "evidence_fingerprint": evidence["evidence_fingerprint"],
            "base_branch": "release",
            "branch_pattern": "ompire/<slug>",
            "workshop_additions": "project",
            "preamble": "be careful",
            "default_model_profile": "chosen",
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["launch_config_state"] == "reconciled"
    assert confirmed.json()["base_branch"] == "release"

    launched = client.post("/api/tasks/preview", headers=headers, json=launch_body())
    assert launched.status_code == 200
    assert launched.json()["workspace"]["base_branch"] == "release"

    # The unselected candidates are still inspectable afterwards.
    after = client.get("/api/projects/demo/launch-reconciliation", headers=headers).json()
    assert after["needs_reconciliation"] is False
    assert after["workspace_conflicts"]["base_branch"] == ["main", "release"]


def test_a_legacy_model_choice_requires_an_explicit_acknowledgement(
    upgraded,
) -> None:
    """One old concrete pair is not four roles. The operator must say, in as
    many words, that a profile replaces it."""
    _, client, headers = upgraded(
        [{"name": "m", "model": "sonnet", "thinking": "high"}]
    )

    evidence = client.get(
        "/api/projects/demo/launch-reconciliation", headers=headers
    ).json()
    assert evidence["model_candidates"] == [
        {"source": "m", "model": "sonnet", "thinking": "high"}
    ]

    body = {
        "evidence_fingerprint": evidence["evidence_fingerprint"],
        "base_branch": "main",
        "branch_pattern": "ompire/<slug>",
        "workshop_additions": "project",
        "preamble": "",
        "default_model_profile": "chosen",
    }
    unacknowledged = client.post(
        "/api/projects/demo/launch-reconciliation", headers=headers, json=body
    )
    assert unacknowledged.status_code == 422
    assert "acknowledge_model_candidates" in unacknowledged.json()["detail"]

    accepted = client.post(
        "/api/projects/demo/launch-reconciliation",
        headers=headers,
        json={**body, "acknowledge_model_candidates": True},
    )
    assert accepted.status_code == 200
    assert accepted.json()["default_model_profile"] == "chosen"


def test_choosing_no_project_default_is_an_explicit_option(upgraded) -> None:
    """A project without a default is valid; the operator selects a profile
    at each launch instead."""
    _, client, headers = upgraded(
        [{"name": "m", "model": "sonnet", "thinking": "high"}]
    )
    evidence = client.get(
        "/api/projects/demo/launch-reconciliation", headers=headers
    ).json()

    accepted = client.post(
        "/api/projects/demo/launch-reconciliation",
        headers=headers,
        json={
            "evidence_fingerprint": evidence["evidence_fingerprint"],
            "base_branch": "main",
            "branch_pattern": "ompire/<slug>",
            "workshop_additions": "project",
            "preamble": "",
            "default_model_profile": None,
            "acknowledge_model_candidates": True,
        },
    )
    assert accepted.status_code == 200
    assert accepted.json()["default_model_profile"] is None

    refused = client.post("/api/tasks/preview", headers=headers, json=launch_body())
    assert refused.status_code == 422
    assert "model_profile" in refused.json()["detail"]

    resolved = client.post(
        "/api/tasks/preview", headers=headers, json=launch_body(model_profile="chosen")
    )
    assert resolved.status_code == 200


def test_a_stale_evidence_fingerprint_writes_nothing(upgraded) -> None:
    _, client, headers = upgraded(
        [
            {"name": "a", "base_branch": "main"},
            {"name": "b", "base_branch": "release"},
        ]
    )
    response = client.post(
        "/api/projects/demo/launch-reconciliation",
        headers=headers,
        json={
            "evidence_fingerprint": "not-the-one-that-was-read",
            "base_branch": "release",
            "branch_pattern": "ompire/<slug>",
            "workshop_additions": "project",
            "preamble": "",
            "default_model_profile": "chosen",
        },
    )
    assert response.status_code == 409
    assert (
        client.get("/api/projects/demo", headers=headers).json()["launch_config_state"]
        == "needs-reconciliation"
    )


def test_a_retired_judge_model_is_captured_and_acknowledged_per_project(
    upgraded, daemon_config: Config
) -> None:
    """The daemon records what it found in `config.toml`, never rewrites it,
    and blocks the affected project until the operator accepts that the
    profile's `slow` binding replaces it."""
    config = replace(daemon_config, retired={"judge_model": "old-judge-model"})
    app, client, headers = upgraded(
        [{"name": "t", "base_branch": "main"}], config=config
    )

    project = client.get("/api/projects/demo", headers=headers).json()
    assert project["launch_config_state"] == "needs-reconciliation"

    evidence = client.get(
        "/api/projects/demo/launch-reconciliation", headers=headers
    ).json()
    assert evidence["retired_judge_model"] == "old-judge-model"
    # No role replaces it: the engine runs no implicit model at all now.
    assert evidence["judge_removed"] is True

    body = {
        "evidence_fingerprint": evidence["evidence_fingerprint"],
        "base_branch": "main",
        "branch_pattern": "ompire/<slug>",
        "workshop_additions": "project",
        "preamble": "",
        "default_model_profile": "chosen",
    }
    unacknowledged = client.post(
        "/api/projects/demo/launch-reconciliation", headers=headers, json=body
    )
    assert unacknowledged.status_code == 422
    assert "acknowledge_judge_model" in unacknowledged.json()["detail"]

    accepted = client.post(
        "/api/projects/demo/launch-reconciliation",
        headers=headers,
        json={**body, "acknowledge_judge_model": True},
    )
    assert accepted.status_code == 200

    # A restart with the same unchanged value does not reopen the decision.
    client.close()
    app.state.engine.dispose()
    restarted = create_app(config, frontend_dist=config.data_dir / "no-dist")
    with TestClient(restarted) as again:
        headers2 = {"Authorization": f"Bearer {restarted.state.auth_token}"}
        assert (
            again.get("/api/projects/demo", headers=headers2).json()[
                "launch_config_state"
            ]
            == "reconciled"
        )

    # A *changed* retired value is new evidence, and reopens it.
    restarted.state.engine.dispose()
    changed = replace(daemon_config, retired={"judge_model": "a-different-model"})
    third = create_app(changed, frontend_dist=changed.data_dir / "no-dist")
    with TestClient(third) as final:
        headers3 = {"Authorization": f"Bearer {third.state.auth_token}"}
        assert (
            final.get("/api/projects/demo", headers=headers3).json()[
                "launch_config_state"
            ]
            == "needs-reconciliation"
        )
        reopened = final.get(
            "/api/projects/demo/launch-reconciliation", headers=headers3
        ).json()
        assert reopened["retired_judge_model"] == "a-different-model"


def test_a_zero_template_project_needs_no_decision_and_gets_new_defaults(
    upgraded, daemon_config: Config
) -> None:
    config = replace(daemon_config, default_branch_pattern="wip/<slug>")
    app, client, headers = upgraded([], config=config)

    project = client.get("/api/projects/demo", headers=headers).json()
    assert project["launch_config_state"] == "reconciled"
    # The daemon's configured pattern is the seed, applied once at startup.
    assert project["branch_pattern"] == "wip/<slug>"

    # Restarting does not re-apply it over whatever the operator has chosen.
    client.put(
        "/api/projects/demo",
        headers=headers,
        json={
            "title": project["title"],
            "upstream_url": project["upstream_url"],
            "checkout_path": project["checkout_path"],
            "branch_pattern": "mine/<slug>",
        },
    )
    client.close()
    app.state.engine.dispose()
    restarted = create_app(config, frontend_dist=config.data_dir / "no-dist")
    with TestClient(restarted) as again:
        headers2 = {"Authorization": f"Bearer {restarted.state.auth_token}"}
        assert (
            again.get("/api/projects/demo", headers=headers2).json()["branch_pattern"]
            == "mine/<slug>"
        )


def test_unrelated_projects_keep_working_while_one_is_unreconciled(
    upgraded, daemon_config: Config, tmp_path: Path
) -> None:
    from tests.conftest import make_adoptable_checkout

    _, client, headers = upgraded(
        [
            {"name": "a", "base_branch": "main"},
            {"name": "b", "base_branch": "release"},
        ]
    )
    other_checkout = make_adoptable_checkout(daemon_config.checkout_root, "other")
    created = client.post(
        "/api/projects",
        headers=headers,
        json={
            "name": "other",
            "title": "Other",
            "upstream_url": "https://example.com/other.git",
            "checkout_path": str(other_checkout),
            "default_model_profile": "chosen",
        },
    )
    assert created.status_code == 201, created.text
    # A newly registered project already uses the new contract.
    assert created.json()["launch_config_state"] == "reconciled"

    ok = client.post(
        "/api/tasks/preview", headers=headers, json=launch_body(project_name="other")
    )
    assert ok.status_code == 200


# --- legacy task continuation -------------------------------------------------


def _seed_legacy_task(config: Config, checkout: Path, *, state: str = "created") -> int:
    """A task row as 0013 leaves one: full history, no pinned inputs."""
    engine = make_engine(db_path_for(config.data_dir))
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tasks (project_name, slug, branch, clone_path, state, "
                "prompt, workflow_name, workflow_status, workflow_step, "
                "spawn_completed_at, created_at, updated_at) VALUES "
                "('demo', 'legacy', 'ompire/legacy', :clone, :state, 'fix it', "
                "'single-step', 'running', 'work', '2026-08-01T00:00:00+00:00', "
                "'2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00')"
            ),
            {"clone": str(checkout), "state": state},
        )
        task_id = conn.execute(
            text("SELECT id FROM tasks WHERE slug = 'legacy'")
        ).scalar_one()
    engine.dispose()
    return task_id


def test_a_legacy_task_states_what_is_unknown_and_blocks_until_confirmed(
    daemon_config: Config, git_checkout: Path
) -> None:
    _land_at_0012(daemon_config)
    _seed(daemon_config, git_checkout, [{"name": "t"}])
    task_id = _seed_legacy_task(daemon_config, git_checkout)
    app = create_app(daemon_config, frontend_dist=daemon_config.data_dir / "no-dist")
    from ompire_daemon.registry.model_profiles import create_model_profile

    create_model_profile(app.state.engine, name="chosen", roles=TEST_ROLES)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {app.state.auth_token}"}

        task = client.get(f"/api/tasks/{task_id}", headers=headers).json()
        assert task["needs_configuration"] is True
        assert task["execution_inputs"] is None

        configuration = client.get(
            f"/api/tasks/{task_id}/configuration", headers=headers
        ).json()
        # Known facts are preserved exactly; the missing ones are named.
        assert configuration["known"]["branch"] == "ompire/legacy"
        assert configuration["known"]["workflow_status"] == "running"
        assert configuration["unknown_inputs"] == [
            "model_profile",
            "thinking",
            "preamble",
            "workspace_overrides",
            # The definition itself was never recorded either (ADR-0028).
            "workflow_definition",
        ]
        assert configuration["needs_workflow_confirmation"] is True
        candidate = configuration["workflow_candidate"]
        assert candidate["workflow_name"] == "single-step"
        assert candidate["revision"].startswith("sha256:")
        assert candidate["compatible"] is True
        # The template it came from is attribution, not its contents.
        assert configuration["source_attribution"][0]["source"] == ""
        # The current project's routing is offered as a candidate to confirm.
        assert configuration["candidates"]["checkout_path"] == str(git_checkout)

        # Continuing is refused until a configuration is confirmed.
        blocked = client.post(f"/api/tasks/{task_id}/continue", headers=headers)
        assert blocked.status_code == 409
        assert "launch configuration" in blocked.json()["detail"]
        # So is review.
        assert (
            client.post(f"/api/tasks/{task_id}/review", headers=headers).status_code
            != 200
        )

        continuation = {
            "model_profile": "chosen",
            "base_branch": "main",
            "workshop_additions": "project",
            "preamble": "",
        }
        preview = client.post(
            f"/api/tasks/{task_id}/configuration/preview",
            headers=headers,
            json=continuation,
        ).json()
        assert preview["inputs"]["provenance"] == "legacy-confirmed"
        assert preview["unknown_inputs"] == configuration["unknown_inputs"]
        assert (
            preview["inputs"]["workflow_binding"]["revision"]
            == preview["workflow"]["revision"]
        )
        # The branch that exists is stated as the branch, not as a pattern
        # nobody recorded.
        assert preview["inputs"]["branch"] == "ompire/legacy"

        # The acknowledgement is not optional.
        unacknowledged = client.post(
            f"/api/tasks/{task_id}/configuration/confirm",
            headers=headers,
            json={
                **continuation,
                "preview_token": preview["preview_token"],
                "acknowledge_workflow": True,
            },
        )
        assert unacknowledged.status_code == 422
        assert "acknowledge_unknown" in unacknowledged.json()["detail"]

        # And so is the separate one about the definition.
        no_workflow_ack = client.post(
            f"/api/tasks/{task_id}/configuration/confirm",
            headers=headers,
            json={
                **continuation,
                "preview_token": preview["preview_token"],
                "acknowledge_unknown": True,
            },
        )
        assert no_workflow_ack.status_code == 422
        assert "acknowledge_workflow" in no_workflow_ack.json()["detail"]

        confirmed = client.post(
            f"/api/tasks/{task_id}/configuration/confirm",
            headers=headers,
            json={
                **continuation,
                "preview_token": preview["preview_token"],
                "acknowledge_unknown": True,
                "acknowledge_workflow": True,
            },
        )
        assert confirmed.status_code == 200, confirmed.text
        body = confirmed.json()
        assert body["needs_configuration"] is False
        assert body["execution_inputs"]["model_profile_source"] == "legacy-confirmed"
        # Confirmation pins the future; it changes no recorded history.
        assert body["branch"] == "ompire/legacy"
        assert body["workflow_status"] == "running"
        assert body["clone_path"] == str(git_checkout)

        # A second confirmation is refused rather than silently reapplied.
        again = client.post(
            f"/api/tasks/{task_id}/configuration/confirm",
            headers=headers,
            json={
                **continuation,
                "preview_token": preview["preview_token"],
                "acknowledge_unknown": True,
            },
        )
        assert again.status_code == 409


def test_a_stale_continuation_preview_changes_nothing(
    daemon_config: Config, git_checkout: Path
) -> None:
    _land_at_0012(daemon_config)
    _seed(daemon_config, git_checkout, [{"name": "t"}])
    task_id = _seed_legacy_task(daemon_config, git_checkout)
    app = create_app(daemon_config, frontend_dist=daemon_config.data_dir / "no-dist")
    from ompire_daemon.registry.model_profiles import create_model_profile

    create_model_profile(app.state.engine, name="chosen", roles=TEST_ROLES)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {app.state.auth_token}"}
        response = client.post(
            f"/api/tasks/{task_id}/configuration/confirm",
            headers=headers,
            json={
                "model_profile": "chosen",
                "base_branch": "main",
                "workshop_additions": "project",
                "preamble": "",
                "preview_token": "reviewed-something-else",
                "acknowledge_unknown": True,
                "acknowledge_workflow": True,
            },
        )
        assert response.status_code == 409
        assert (
            client.get(f"/api/tasks/{task_id}", headers=headers).json()[
                "needs_configuration"
            ]
            is True
        )


def test_an_archived_legacy_task_stays_readable_without_confirmation(
    daemon_config: Config, git_checkout: Path
) -> None:
    """Archived history needs no fabricated inputs and asks for nothing."""
    _land_at_0012(daemon_config)
    _seed(daemon_config, git_checkout, [{"name": "t"}])
    task_id = _seed_legacy_task(daemon_config, git_checkout, state="archived")
    app = create_app(daemon_config, frontend_dist=daemon_config.data_dir / "no-dist")
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {app.state.auth_token}"}
        configuration = client.get(
            f"/api/tasks/{task_id}/configuration", headers=headers
        ).json()
        assert configuration["archived"] is True
        assert configuration["known"]["state"] == "archived"
        # Listing and detail keep working.
        assert client.get(f"/api/tasks/{task_id}", headers=headers).status_code == 200
        # And purging it is still available.
        assert client.delete(f"/api/tasks/{task_id}", headers=headers).status_code == 200


LEGACY_FORMAT_1_BUGFIX = """
format: 1
name: bugfix
sessions: [reproducer, coder]
primary: coder
steps:
  - name: reproduce
    kind: agent
    session: reproducer
    expects_outcome: true
    prompt: {parts: [{text: "reproduce it"}]}
  - name: triage
    kind: decision
    cases:
      - when:
          op: eq
          left:
            op: get
            value: {op: latest, steps: [reproduce]}
            keys: [outcome, status]
          right: {op: literal, value: "success"}
        next: {step: fix}
    otherwise: {step: escalate}
  - name: fix
    kind: agent
    session: coder
    expects_outcome: true
    max_visits: 3
    on_exhausted: {step: escalate}
    prompt: {parts: [{text: "fix it"}]}
  - name: route-validate
    kind: decision
    cases:
      - when: true
        next: {step: validate-agent}
    otherwise: {step: validate-agent}
  - name: validate-agent
    kind: agent
    session: reproducer
    expects_outcome: true
    prompt: {parts: [{text: "validate it"}]}
  - name: check
    kind: decision
    cases:
      - when:
          op: eq
          left:
            op: get
            value: {op: latest, steps: [validate-agent], after: fix}
            keys: [outcome, status]
          right: {op: literal, value: "success"}
        next: {complete: true}
    otherwise: {pause: true}
  - name: escalate
    kind: gate
    message: {parts: [{text: "operator call"}]}
"""


def _install_format_1_bugfix() -> None:
    """Make the process catalog's `bugfix` a format-1 definition."""
    from ompire_daemon.workflow_definitions import load_definition
    from ompire_daemon.workflows import catalog, install_definition

    catalog()  # load the packaged set first, then shadow one entry
    install_definition(load_definition(LEGACY_FORMAT_1_BUGFIX))

def _seed_legacy_bugfix_at_a_synthesized_gate(config: Config, checkout: Path) -> int:
    """A bugfix task parked exactly as the pre-ADR-0028 engine left one.

    That engine escalated an unresolvable decision by finishing the *decision*
    record `ok` with the escalation message in its error field, then appending
    a separate `gate` record under the decision's own name. Resuming it fell
    through to the step after the decision — the silent "continue as if the
    evidence had been accepted" this change removes.
    """
    engine = make_engine(db_path_for(config.data_dir))
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tasks (project_name, slug, branch, clone_path, state, "
                "prompt, workflow_name, workflow_status, workflow_step, "
                "spawn_completed_at, created_at, updated_at) VALUES "
                "('demo', 'legacy-gate', 'ompire/legacy-gate', :clone, 'created', "
                "'fix it', 'bugfix', 'waiting', 'check', "
                "'2026-08-01T00:00:00+00:00', '2026-08-01T00:00:00+00:00', "
                "'2026-08-01T00:00:00+00:00')"
            ),
            {"clone": str(checkout)},
        )
        task_id = conn.execute(
            text("SELECT id FROM tasks WHERE slug = 'legacy-gate'")
        ).scalar_one()
        rows = [
            (1, "reproduce", "agent", "reproducer", "ok",
             '{"version": 1, "status": "success", "summary": "reproduced"}', None),
            (2, "triage", "decision", None, "ok", '{"route": "fix"}', None),
            (3, "fix", "agent", "coder", "ok",
             '{"version": 1, "status": "success", "summary": "fixed"}', None),
            (4, "route-validate", "decision", None, "ok",
             '{"route": "validate-agent"}', None),
            (5, "validate-agent", "agent", "reproducer", "ok", None, None),
            # The synthesized pair: decision finished ok with the message in
            # `error` and no outcome, then a gate record under its own name.
            (6, "check", "decision", None, "ok", None,
             "decision 'check' resolved no route (a required outcome is missing?)"),
            (7, "check", "gate", None, "waiting",
             '{"message": "the route could not be resolved; resume to continue"}', None),
        ]
        for seq, step, kind, session, status, outcome, error in rows:
            conn.execute(
                text(
                    "INSERT INTO workflow_step_records (task_id, seq, step, kind, "
                    "session, status, outcome_json, error, started_at) VALUES "
                    "(:t, :s, :step, :kind, :sess, :status, :outcome, :error, "
                    "'2026-08-01T00:00:00+00:00')"
                ),
                {"t": task_id, "s": seq, "step": step, "kind": kind,
                 "sess": session, "status": status, "outcome": outcome,
                 "error": error},
            )
        for name in ("reproducer", "coder"):
            conn.execute(
                text(
                    "INSERT INTO task_sessions (task_id, name, omp_session_id, "
                    "spawned_at) VALUES (:t, :n, :sid, '2026-08-01T00:00:00+00:00')"
                ),
                {"t": task_id, "n": name, "sid": f"sess-{name}"},
            )
    engine.dispose()
    return task_id


def test_a_format_2_candidate_cannot_claim_format_1_history(
    daemon_config: Config, git_checkout: Path
) -> None:
    """The format boundary, enforced where a continuation is offered.

    The packaged `bugfix` is format 2: it reads results by declared name, and
    it does not declare the steps this task actually ran. Its history recorded
    generic success/failed outcomes that the new contract has no way to
    express. Offering it as a continuation would point a different procedure
    at somebody else's history, so it is refused with the reasons named — and
    no automatic upgrade is offered in its place.
    """
    _land_at_0012(daemon_config)
    _seed(daemon_config, git_checkout, [{"name": "t"}])
    task_id = _seed_legacy_bugfix_at_a_synthesized_gate(daemon_config, git_checkout)
    app = create_app(daemon_config, frontend_dist=daemon_config.data_dir / "no-dist")
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {app.state.auth_token}"}
        configuration = client.get(
            f"/api/tasks/{task_id}/configuration", headers=headers
        ).json()
        candidate = configuration["workflow_candidate"]

    assert candidate["format"] == 2
    assert candidate["compatible"] is False
    problems = " ".join(candidate["problems"])
    # Both halves are named: the results cannot be reinterpreted, and the
    # steps are not even declared.
    assert "older success/failed envelope" in problems
    assert "workflow format 2" in problems
    assert "'triage'" in problems
    # The history itself is untouched and still readable.
    from ompire_daemon.registry.workflows import list_step_records

    records = list_step_records(app.state.engine, task_id)
    assert [r.step for r in records] == [
        "reproduce",
        "triage",
        "fix",
        "route-validate",
        "validate-agent",
        "check",
        "check",
    ]
    assert records[-1].status == "waiting"


def test_an_old_synthesized_escalation_gate_retries_the_decision(
    daemon_config: Config, git_checkout: Path
) -> None:
    """The one legacy record shape a `decision` step is allowed to explain.

    A strict kind check would call this history incompatible, because the
    persisted kind is `gate` where the definition declares a `decision`. It is
    admitted only in exactly that shape, both records are preserved, and after
    confirmation the waiting record becomes an uncertainty pause whose action
    retries the decision — never the old fall-through to the next step.
    """
    _install_format_1_bugfix()
    _land_at_0012(daemon_config)
    _seed(daemon_config, git_checkout, [{"name": "t"}])
    task_id = _seed_legacy_bugfix_at_a_synthesized_gate(daemon_config, git_checkout)
    app = create_app(daemon_config, frontend_dist=daemon_config.data_dir / "no-dist")
    from ompire_daemon.registry.model_profiles import create_model_profile

    create_model_profile(app.state.engine, name="chosen", roles=TEST_ROLES)
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {app.state.auth_token}"}

        configuration = client.get(
            f"/api/tasks/{task_id}/configuration", headers=headers
        ).json()
        candidate = configuration["workflow_candidate"]
        # The known shape is admitted; nothing else about the history is.
        assert candidate["compatible"] is True, candidate["problems"]
        assert candidate["legacy_through_seq"] == 7
        assert candidate["interrupted_legacy_seq"] == 7

        continuation = {
            "model_profile": "chosen",
            "base_branch": "main",
            "workshop_additions": "project",
            "preamble": "",
        }
        preview = client.post(
            f"/api/tasks/{task_id}/configuration/preview",
            headers=headers,
            json=continuation,
        ).json()
        confirmed = client.post(
            f"/api/tasks/{task_id}/configuration/confirm",
            headers=headers,
            json={
                **continuation,
                "preview_token": preview["preview_token"],
                "acknowledge_unknown": True,
                "acknowledge_workflow": True,
            },
        )
        assert confirmed.status_code == 200, confirmed.text

    from ompire_daemon.registry.workflows import list_step_records

    records = list_step_records(app.state.engine, task_id)
    # Both records survive: the decision's own evidence and the gate that
    # stood in for it.
    assert [(r.seq, r.step, r.kind) for r in records[-2:]] == [
        (6, "check", "decision"),
        (7, "check", "gate"),
    ]
    waiting = records[-1]
    assert waiting.status == "waiting"
    assert waiting.pause is not None
    assert waiting.pause["reason"] == "unresolved_decision"
    # The action retries the *decision*, not the step after it.
    assert waiting.pause["retry_step"] == "check"
    assert waiting.pause["retry_kind"] == "decision"
    assert "re-evaluates" in waiting.pause["message"]
    # The original escalation message is kept, not replaced.
    assert "the route could not be resolved" in waiting.pause["message"]

    # And the retry really opens a *decision* attempt at `check`, rather than
    # another gate or the step after it.
    from ompire_daemon.registry.workflows import retry_paused_step

    opened, updated = retry_paused_step(app.state.engine, task_id, waiting.seq)
    assert (opened.step, opened.kind, opened.status) == ("check", "decision", "running")
    assert updated.workflow_status == "running"
    assert updated.workflow_step == "check"
    closed = list_step_records(app.state.engine, task_id)[-2]
    assert closed.seq == waiting.seq and closed.status == "failed"
    assert "unresolved route recorded before" in closed.error
