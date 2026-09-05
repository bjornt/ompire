"""REST and registry behavior for global model profiles (ADR-0025).

Covers the public boundaries this change introduces: the four-role contract
and its value grammar, whole-profile replacement atomicity, reference-guarded
deletion, and the write reservation that keeps a deletion from racing a
project assignment. Project-side omitted-versus-null update semantics live in
`test_projects_rest.py`, which owns project update behavior.
"""

from __future__ import annotations

import threading

from fastapi.testclient import TestClient

from ompire_daemon.registry.model_profiles import (
    ModelProfileReferencedError,
    UnknownModelProfileReferenceError,
    create_model_profile,
    delete_model_profile,
    get_model_profile,
)
from ompire_daemon.registry.projects import create_project, update_project

from .conftest import make_adoptable_checkout

ROLES = {
    "default": {"model": "anthropic/claude-sonnet-4.5", "thinking": "medium"},
    "smol": {"model": "openai/gpt-4.1-mini", "thinking": "off"},
    "slow": {"model": "openai/o3", "thinking": "high"},
    "plan": {"model": "google/gemini-2.5-pro", "thinking": "max"},
}


def _create_profile(
    client: TestClient, auth_headers: dict[str, str], name: str = "balanced", **overrides
) -> dict:
    payload = {"name": name, "roles": overrides.pop("roles", ROLES)}
    payload.update(overrides)
    response = client.post("/api/model-profiles", headers=auth_headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _create_project_via_rest(
    client: TestClient, auth_headers: dict[str, str], name: str = "demo", **overrides
) -> dict:
    make_adoptable_checkout(client.app.state.config.checkout_root, name)
    payload = {
        "name": name,
        "title": name.title(),
        "upstream_url": f"https://example.com/{name}.git",
    }
    payload.update(overrides)
    response = client.post("/api/projects", headers=auth_headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


# --- Authentication ---------------------------------------------------------


def test_profile_endpoints_require_the_bearer_token(client: TestClient) -> None:
    """The profile routes carry the router's existing authentication boundary
    (ADR-0002); none of them is reachable unauthenticated."""
    for method, path in (
        ("GET", "/api/model-profiles"),
        ("POST", "/api/model-profiles"),
        ("GET", "/api/model-profiles/balanced"),
        ("PUT", "/api/model-profiles/balanced"),
        ("DELETE", "/api/model-profiles/balanced"),
    ):
        assert client.request(method, path, json={}).status_code == 401, (method, path)


# --- The four-role contract -------------------------------------------------


def test_create_returns_all_four_pairs_and_publishes_one_event(
    client: TestClient, auth_headers: dict[str, str], app
) -> None:
    queue = app.state.events.subscribe()

    profile = _create_profile(client, auth_headers)

    assert profile["name"] == "balanced"
    assert profile["roles"] == ROLES
    assert profile["created_at"] == profile["updated_at"]
    event = queue.get_nowait()
    assert event.type == "model_profile_created"
    assert event.payload["roles"] == ROLES
    assert queue.empty()

    listed = client.get("/api/model-profiles", headers=auth_headers)
    assert [p["name"] for p in listed.json()] == ["balanced"]


def test_list_is_sorted_by_name(client: TestClient, auth_headers: dict[str, str]) -> None:
    for name in ("thorough", "balanced", "cheap"):
        _create_profile(client, auth_headers, name)

    listed = client.get("/api/model-profiles", headers=auth_headers).json()

    assert [p["name"] for p in listed] == ["balanced", "cheap", "thorough"]


def test_duplicate_name_is_409(client: TestClient, auth_headers: dict[str, str]) -> None:
    _create_profile(client, auth_headers)

    response = client.post(
        "/api/model-profiles", headers=auth_headers, json={"name": "balanced", "roles": ROLES}
    )

    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]


def test_unknown_profile_lookup_update_and_delete_are_404(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    assert client.get("/api/model-profiles/ghost", headers=auth_headers).status_code == 404
    assert (
        client.put(
            "/api/model-profiles/ghost", headers=auth_headers, json={"roles": ROLES}
        ).status_code
        == 404
    )
    assert client.delete("/api/model-profiles/ghost", headers=auth_headers).status_code == 404


def test_missing_extra_and_null_bindings_are_refused(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    def post(roles: dict) -> int:
        return client.post(
            "/api/model-profiles", headers=auth_headers, json={"name": "p", "roles": roles}
        ).status_code

    # A missing role would leave the profile unable to answer for it.
    assert post({role: ROLES[role] for role in ("default", "smol", "slow")}) == 422
    # An extra role is a typo or a stale client, not silently ignorable config.
    assert post({**ROLES, "judge": ROLES["slow"]}) == 422
    # Null is not "the host default" — a binding must name both values.
    assert post({**ROLES, "plan": {"model": None, "thinking": "high"}}) == 422
    assert post({**ROLES, "plan": {"model": "openai/o3", "thinking": None}}) == 422
    # An unknown field inside a binding is an error, not extra configuration.
    assert post({**ROLES, "plan": {**ROLES["plan"], "provider": "openai"}}) == 422
    assert client.get("/api/model-profiles", headers=auth_headers).json() == []


def test_invalid_model_identifiers_are_refused_with_role_and_field(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    def detail_for(model: str) -> str:
        response = client.post(
            "/api/model-profiles",
            headers=auth_headers,
            json={"name": "p", "roles": {**ROLES, "slow": {"model": model, "thinking": "high"}}},
        )
        assert response.status_code == 422, response.text
        return response.json()["detail"]

    # A bare fuzzy name is what templates accept; a profile binding is concrete.
    assert "provider-qualified" in detail_for("sonnet")
    for bad in ("openai/", "/o3", "open ai/o3", "openai/o3*", "openai/o3?", "openai/o3#x",
                "openai\\o3", "https://example.com/o3", "!openai/o3", "openai/o\n3", ""):
        detail = detail_for(bad)
        assert detail.startswith("role 'slow' field 'model':"), (bad, detail)

    # The native `model:level` argv encoding would hide a second thinking value.
    assert "thinking field" in detail_for("openai/o3:high")


def test_model_punctuation_and_non_thinking_suffixes_survive(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    roles = {
        # Whitespace around the value is trimmed; case and internal punctuation
        # are the model's own and are stored unchanged.
        "default": {"model": "  openrouter/Qwen/qwen3-coder:free  ", "thinking": "low"},
        "smol": {"model": "openai/gpt-4.1-mini", "thinking": "off"},
        "slow": {"model": "anthropic/claude-opus-4-1-20250805", "thinking": "xhigh"},
        "plan": {"model": "x.ai_1/grok-4", "thinking": "auto"},
    }

    profile = _create_profile(client, auth_headers, "mixed", roles=roles)

    assert profile["roles"]["default"]["model"] == "openrouter/Qwen/qwen3-coder:free"
    assert profile["roles"]["slow"]["model"] == "anthropic/claude-opus-4-1-20250805"
    assert profile["roles"]["plan"]["model"] == "x.ai_1/grok-4"


def test_invalid_thinking_level_is_refused(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.post(
        "/api/model-profiles",
        headers=auth_headers,
        json={"name": "p", "roles": {**ROLES, "smol": {"model": "openai/o3", "thinking": "fast"}}},
    )

    assert response.status_code == 422
    assert response.json()["detail"].startswith("role 'smol' field 'thinking':")


def test_invalid_profile_name_is_refused(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    response = client.post(
        "/api/model-profiles", headers=auth_headers, json={"name": "Not A Slug", "roles": ROLES}
    )

    assert response.status_code == 422


# --- Whole-profile replacement ----------------------------------------------


def test_update_replaces_all_bindings_and_preserves_creation_identity(
    client: TestClient, auth_headers: dict[str, str], app
) -> None:
    created = _create_profile(client, auth_headers)
    queue = app.state.events.subscribe()
    replacement = {role: {"model": "openai/o3", "thinking": "minimal"} for role in ROLES}

    response = client.put(
        "/api/model-profiles/balanced", headers=auth_headers, json={"roles": replacement}
    )

    assert response.status_code == 200
    updated = response.json()
    assert updated["roles"] == replacement
    assert updated["created_at"] == created["created_at"]
    assert updated["updated_at"] != created["updated_at"]
    event = queue.get_nowait()
    assert event.type == "model_profile_updated"
    assert event.payload["roles"] == replacement


def test_refused_update_leaves_every_prior_binding_intact(
    client: TestClient, auth_headers: dict[str, str], app
) -> None:
    """An invalid replacement must not land partially: the three good rows in
    the same body are not written while the fourth is refused."""
    created = _create_profile(client, auth_headers)
    queue = app.state.events.subscribe()

    response = client.put(
        "/api/model-profiles/balanced",
        headers=auth_headers,
        json={
            "roles": {
                "default": {"model": "openai/gpt-5", "thinking": "low"},
                "smol": {"model": "openai/gpt-5-mini", "thinking": "off"},
                "slow": {"model": "openai/o3", "thinking": "high"},
                "plan": {"model": "sonnet", "thinking": "max"},
            }
        },
    )

    assert response.status_code == 422
    saved = client.get("/api/model-profiles/balanced", headers=auth_headers).json()
    assert saved["roles"] == ROLES
    assert saved["updated_at"] == created["updated_at"]
    # A refusal is not a mutation, so nothing is broadcast.
    assert queue.empty()


# --- Reference safety -------------------------------------------------------


def test_delete_is_refused_while_projects_reference_it(
    client: TestClient, auth_headers: dict[str, str], app
) -> None:
    _create_profile(client, auth_headers)
    _create_project_via_rest(client, auth_headers, "alpha", default_model_profile="balanced")
    _create_project_via_rest(client, auth_headers, "beta", default_model_profile="balanced")
    queue = app.state.events.subscribe()

    response = client.delete("/api/model-profiles/balanced", headers=auth_headers)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "alpha" in detail and "beta" in detail
    assert queue.empty()
    # No cascade: both projects keep their default.
    for name in ("alpha", "beta"):
        project = client.get(f"/api/projects/{name}", headers=auth_headers).json()
        assert project["default_model_profile"] == "balanced"


def test_delete_succeeds_once_no_project_references_it(
    client: TestClient, auth_headers: dict[str, str], app
) -> None:
    _create_profile(client, auth_headers)
    _create_project_via_rest(client, auth_headers, "alpha", default_model_profile="balanced")
    project = client.get("/api/projects/alpha", headers=auth_headers).json()
    queue = app.state.events.subscribe()
    cleared = client.put(
        "/api/projects/alpha",
        headers=auth_headers,
        json={
            "title": project["title"],
            "upstream_url": project["upstream_url"],
            "checkout_path": project["checkout_path"],
            "default_model_profile": None,
        },
    )
    assert cleared.json()["default_model_profile"] is None
    assert queue.get_nowait().type == "project_updated"

    response = client.delete("/api/model-profiles/balanced", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {"deleted": "balanced"}
    assert queue.get_nowait().type == "model_profile_deleted"
    assert client.get("/api/model-profiles", headers=auth_headers).json() == []


def test_removing_a_project_releases_its_reference_but_keeps_the_profile(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    _create_profile(client, auth_headers)
    _create_project_via_rest(client, auth_headers, "alpha", default_model_profile="balanced")

    assert client.delete("/api/projects/alpha", headers=auth_headers).status_code == 200

    assert client.get("/api/model-profiles/balanced", headers=auth_headers).status_code == 200
    assert client.delete("/api/model-profiles/balanced", headers=auth_headers).status_code == 200


def test_deletion_racing_an_assignment_cannot_orphan_a_default(app) -> None:
    """The reference scan and the delete share one SQLite write reservation.

    Both mutations run on their own file-backed engine, as two REST workers
    would. Whichever commits first wins outright: either the delete refuses
    because it sees the committed assignment, or the assignment fails because
    the profile is already gone. The state that must never exist is a project
    whose default names a nonexistent profile.
    """
    from ompire_daemon.db import db_path_for, make_engine

    engine = app.state.engine
    config = app.state.config
    create_model_profile(engine, name="balanced", roles=ROLES)
    create_project(
        engine,
        name="alpha",
        title="Alpha",
        upstream_url="https://example.com/alpha.git",
        checkout_path=str(config.checkout_root / "alpha"),
        default_checkout_root=config.checkout_root,
    )

    assigner_engine = make_engine(db_path_for(config.data_dir))
    outcomes: dict[str, BaseException | None] = {}
    barrier = threading.Barrier(2)

    def assign() -> None:
        barrier.wait()
        try:
            update_project(
                assigner_engine,
                "alpha",
                title="Alpha",
                upstream_url="https://example.com/alpha.git",
                fork_url=None,
                checkout_path=str(config.checkout_root / "alpha"),
                default_model_profile="balanced",
            )
            outcomes["assign"] = None
        except BaseException as exc:  # noqa: BLE001 — recorded, then asserted
            outcomes["assign"] = exc

    thread = threading.Thread(target=assign)
    thread.start()
    barrier.wait()
    try:
        delete_model_profile(engine, "balanced")
        outcomes["delete"] = None
    except BaseException as exc:  # noqa: BLE001 — recorded, then asserted
        outcomes["delete"] = exc
    thread.join(timeout=30)
    assert not thread.is_alive()

    # Exactly one of the two mutations may succeed.
    succeeded = [op for op, error in outcomes.items() if error is None]
    assert len(succeeded) == 1, outcomes
    if succeeded == ["delete"]:
        assert isinstance(outcomes["assign"], UnknownModelProfileReferenceError)
    else:
        assert isinstance(outcomes["delete"], ModelProfileReferencedError)

    # Whatever the interleaving, the committed state is consistent.
    from ompire_daemon.registry.projects import get_project

    reference = get_project(engine, "alpha").default_model_profile
    if reference is not None:
        assert get_model_profile(engine, reference).name == reference
