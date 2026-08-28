"""Focused tests for the trusted GitHub CLI boundary."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ompire_daemon import gh as gh_module
from ompire_daemon.config import Config
from ompire_daemon.events import EventHub
from ompire_daemon.gh import (
    GitHubCli,
    GitHubProbe,
    parse_github_target,
    redact_github_text,
)
from ompire_daemon.registry.projects import create_project
from ompire_daemon.registry.tasks import Task, create_task


@pytest.fixture
def fake_gh(tmp_path: Path) -> Path:
    script = tmp_path / "gh"
    script.write_text(
        """#!/bin/sh
set -eu
[ -z "${GH_FAKE_ARGS_LOG:-}" ] || printf '%s\\n' "$*" >>"$GH_FAKE_ARGS_LOG"
case "$*" in
  "--version")
    if [ "${GH_FAKE_VERSION_EXIT:-0}" != 0 ]; then
      printf '%s\\n' "${GH_FAKE_VERSION_DETAIL:-version failure}" >&2
      exit "$GH_FAKE_VERSION_EXIT"
    fi
    printf '%s\\n' "${GH_FAKE_VERSION:-gh version 2.97.0 (fake)}"
    ;;
  "api --hostname github.com user")
    if [ "${GH_FAKE_AUTH:-ready}" != ready ]; then
      printf '%s\\n' "${GH_FAKE_AUTH_DETAIL:-HTTP 401: Bad credentials}" >&2
      exit "${GH_FAKE_AUTH_CODE:-1}"
    fi
    [ "${GH_FAKE_SLEEP_USER:-0}" = 0 ] || sleep "$GH_FAKE_SLEEP_USER"
    if [ "${GH_FAKE_USER_RAW:-0}" = 1 ]; then
      printf '%s\\n' "${GH_FAKE_USER_OUTPUT:-not-json}"
    elif [ -n "${GH_TOKEN:-}" ]; then
      printf '{"login":"from-gh-token"}\\n'
    elif [ -n "${GITHUB_TOKEN:-}" ]; then
      printf '{"login":"from-github-token"}\\n'
    else
      printf '{"login":"%s"}\\n' "${GH_FAKE_LOGIN:-from-cli-config}"
    fi
    ;;
  "api --hostname github.com repos/"*"/pulls?per_page=1")
    if [ "${GH_FAKE_PULLS_EXIT:-0}" != 0 ]; then
      printf '%s\n' "${GH_FAKE_PULLS_DETAIL:-HTTP 403: forbidden}" >&2
      exit "$GH_FAKE_PULLS_EXIT"
    fi
    printf '%s\n' "${GH_FAKE_PULLS:-[]}"
    ;;
  "api --hostname github.com repos/"*)
    if [ "${GH_FAKE_REPO_EXIT:-0}" != 0 ]; then
      printf '%s\n' "${GH_FAKE_REPO_DETAIL:-HTTP 404: Not Found}" >&2
      exit "$GH_FAKE_REPO_EXIT"
    fi
    repo=${GH_FAKE_REPO:-}
    if [ -z "$repo" ]; then
      repo='{"archived":false,"disabled":false,"has_issues":true,"pull_request_creation_policy":"collaborators_only","role_name":"write","permissions":{"pull":true}}'
    fi
    printf '%s\n' "$repo"
    ;;
  "echo-secret")
    printf 'exact=%s github=%s github_pat_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 gho_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ghu_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ghs_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ghr_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789\n' "${GH_TOKEN:-none}" "${GITHUB_TOKEN:-none}"
    printf 'aUtHoRiZaTiOn: Digest %s\\nhttps://alice:%s@github.com/owner/repo\\n' "${GH_TOKEN:-none}" "${GITHUB_TOKEN:-none}" >&2
    ;;
  "environment")
    printf 'prompt=%s\\n' "${GH_PROMPT_DISABLED:-}"
    ;;
  *)
    printf 'unsupported invocation: %s\\n' "$*" >&2
    exit 1
    ;;
esac
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def config_for(fake_gh: Path, tmp_path: Path) -> Config:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return Config(data_dir=data_dir, gh_command=(str(fake_gh),))


@pytest.mark.parametrize(
    ("url", "canonical"),
    [
        ("git@github.com:Owner/Repository.git", "github.com/owner/repository"),
        ("https://github.com/owner/repository", "github.com/owner/repository"),
        ("https://GITHUB.COM/owner/repository/", "github.com/owner/repository"),
    ],
)
def test_parse_github_target_normalizes_supported_current_shipping_urls(
    url: str, canonical: str
) -> None:
    assert parse_github_target(url).canonical == canonical


@pytest.mark.parametrize(
    "url",
    [
        "https://gitlab.com/owner/repository",
        "ssh://git@github.com/owner/repository",
        "https://github.com/owner",
        "https://token@example.com/owner/repository",
        "not a URL",
    ],
)
def test_parse_github_target_rejects_unsupported_urls_without_echoing_them(
    url: str,
) -> None:
    with pytest.raises(ValueError, match="^unsupported GitHub upstream target$"):
        parse_github_target(url)


async def test_probe_reports_resolved_executable_version_identity_and_environment_precedence(
    fake_gh: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = config_for(fake_gh, tmp_path)
    hub = EventHub()
    probe = GitHubProbe(config, hub)
    monkeypatch.setenv("GH_TOKEN", "first-token")
    monkeypatch.setenv("GITHUB_TOKEN", "second-token")

    status = await probe.probe()

    assert status.identity.state == "ready"
    assert status.identity.login == "from-gh-token"
    assert status.identity.credential_source == "GH_TOKEN"
    assert status.identity.executable_path == str(fake_gh.resolve())
    assert status.identity.version == "gh version 2.97.0 (fake)"
    assert status.identity.host == "github.com"
    assert status.identity.checked_at is not None


async def test_probe_uses_github_token_then_cli_configuration_when_higher_precedence_absent(
    fake_gh: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = config_for(fake_gh, tmp_path)
    probe = GitHubProbe(config, EventHub())
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "second-token")

    github_token = await probe.probe()
    monkeypatch.delenv("GITHUB_TOKEN")
    cli_configuration = await probe.probe()

    assert (github_token.identity.login, github_token.identity.credential_source) == (
        "from-github-token",
        "GITHUB_TOKEN",
    )
    assert (
        cli_configuration.identity.login,
        cli_configuration.identity.credential_source,
    ) == (
        "from-cli-config",
        "GitHub CLI configuration",
    )


async def test_probe_distinguishes_missing_unauthenticated_and_malformed_identity(
    fake_gh: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = GitHubProbe(
        Config(data_dir=tmp_path, gh_command=(str(tmp_path / "ghp_never-run"),)),
        EventHub(),
    )
    assert (await missing.probe()).identity.state == "missing"

    unrunnable = tmp_path / "unrunnable-gh"
    unrunnable.write_text("#!/no/such/interpreter\n", encoding="utf-8")
    unrunnable.chmod(0o755)
    unrunnable_probe = GitHubProbe(
        Config(data_dir=tmp_path, gh_command=(str(unrunnable),)), EventHub()
    )
    assert (await unrunnable_probe.probe()).identity.state == "missing"

    probe = GitHubProbe(config_for(fake_gh, tmp_path), EventHub())
    monkeypatch.setenv("GH_FAKE_AUTH", "rejected")
    unauthenticated = await probe.probe()
    monkeypatch.setenv("GH_FAKE_AUTH", "ready")
    monkeypatch.setenv("GH_FAKE_USER_RAW", "1")
    malformed = await probe.probe()

    assert unauthenticated.identity.state == "unauthenticated"
    assert malformed.identity.state == "error"
    assert malformed.identity.detail == "GitHub API user response was malformed"


async def test_probe_replaces_ready_target_with_error_and_invalidates_after_identity_change(
    fake_gh: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = config_for(fake_gh, tmp_path)
    hub = EventHub()
    events = hub.subscribe()
    probe = GitHubProbe(config, hub)

    ready, target = await probe.probe_target("https://github.com/owner/repo.git")
    assert ready.identity.login == "from-cli-config"
    assert target.state == "allowed"
    assert target.identity is not None
    assert target.identity.login == "from-cli-config"

    monkeypatch.setenv("GH_FAKE_LOGIN", "another-account")
    changed = await probe.probe()

    assert changed.identity.login == "another-account"
    assert changed.targets == {}
    published = [events.get_nowait() for _ in range(2)]
    assert all(event.type == "gh_status" for event in published)
    assert published[-1].payload["gh"]["targets"] == {}
    hub.unsubscribe(events)


async def test_target_checks_fail_closed_for_repository_policy_permission_and_malformed_data(
    fake_gh: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = GitHubProbe(config_for(fake_gh, tmp_path), EventHub())

    monkeypatch.setenv(
        "GH_FAKE_REPO",
        '{"archived":false,"disabled":false,"has_issues":true,"pull_request_creation_policy":"collaborators_only","role_name":"none","permissions":{"pull":false}}',
    )
    _status, denied = await probe.probe_target("git@github.com:owner/repo.git")
    assert denied.state == "denied"
    assert "no effective repository role" in (denied.detail or "")

    monkeypatch.setenv("GH_FAKE_REPO", '{"archived":false}')
    _status, malformed = await probe.probe_target("git@github.com:owner/repo.git")
    assert malformed.state == "error"
    assert "omitted boolean disabled" in (malformed.detail or "")

    monkeypatch.setenv(
        "GH_FAKE_REPO",
        '{"archived":false,"disabled":false,"has_issues":true,"pull_request_creation_policy":"all"}',
    )
    monkeypatch.setenv("GH_FAKE_PULLS", "not-json")
    _status, bad_pulls = await probe.probe_target("git@github.com:owner/repo.git")
    assert bad_pulls.state == "error"
    assert bad_pulls.detail == "GitHub pull-request response was malformed"


async def test_target_check_classifies_repository_visibility_failure_as_denied(
    fake_gh: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = GitHubProbe(config_for(fake_gh, tmp_path), EventHub())
    monkeypatch.setenv("GH_FAKE_REPO_EXIT", "1")
    monkeypatch.setenv("GH_FAKE_REPO_DETAIL", "HTTP 404: Not Found")

    _status, target = await probe.probe_target("https://github.com/owner/repo")

    assert target.state == "denied"
    assert target.target is not None
    assert target.target.canonical == "github.com/owner/repo"


async def test_failed_identity_recheck_clears_prior_allowed_target(
    fake_gh: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = GitHubProbe(config_for(fake_gh, tmp_path), EventHub())
    ready, target = await probe.probe_target("https://github.com/owner/repo")
    assert ready.identity.state == "ready"
    assert target.state == "allowed"

    monkeypatch.setenv("GH_FAKE_AUTH", "rejected")
    failed = await probe.probe()

    assert failed.identity.state == "unauthenticated"
    assert failed.targets == {}


async def test_runner_is_noninteractive_and_redacts_all_credential_families(
    fake_gh: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = config_for(fake_gh, tmp_path)
    cli = GitHubCli(config)
    exact_gh = "exact-secret-value"
    exact_github = "other-secret-value"
    monkeypatch.setenv("GH_TOKEN", exact_gh)
    monkeypatch.setenv("GITHUB_TOKEN", exact_github)

    environment = await cli.run(["environment"], str(config.data_dir), 1)
    result = await cli.run(["echo-secret"], str(config.data_dir), 1)
    text = f"{result.stdout}\n{result.stderr}"

    assert environment.stdout.strip() == "prompt=1"
    for secret in (
        exact_gh,
        exact_github,
        "github_pat_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        "gho_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        "ghu_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        "ghs_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        "ghr_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        "Digest",
        "alice",
    ):
        assert secret not in text
    assert "aUtHoRiZaTiOn: [redacted]" in text
    assert "https://[redacted]@github.com/owner/repo" in text


@pytest.mark.parametrize(
    "text",
    [
        "Authorization: Basic any-value",
        "authorization: Token any-value",
        "https://user:password@example.test/path",
        "ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        "github_pat_abcdefghijklmnopqrstuvwxyz0123456789",
    ],
)
def test_redaction_helper_covers_non_environment_credential_forms(text: str) -> None:
    assert "password" not in redact_github_text(text)
    assert "any-value" not in redact_github_text(text)
    assert "ghp_" not in redact_github_text(text)
    assert "github_pat_" not in redact_github_text(text)


async def test_timeout_and_command_log_never_expose_or_export_credentials(
    fake_gh: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = config_for(fake_gh, tmp_path)
    args_log = tmp_path / "gh-args"
    probe = GitHubProbe(config, EventHub())
    monkeypatch.setenv("GH_TOKEN", "timeout-secret")
    monkeypatch.setenv("GH_FAKE_ARGS_LOG", str(args_log))
    monkeypatch.setenv("GH_FAKE_SLEEP_USER", "1")
    monkeypatch.setattr(gh_module, "_DEFAULT_TIMEOUT", 0.01)

    status = await probe.probe()

    assert status.identity.state == "error"
    assert "timeout-secret" not in (status.identity.detail or "")
    calls = args_log.read_text(encoding="utf-8")
    assert "auth token" not in calls
    assert "auth status --show-token" not in calls
    assert "api --hostname github.com user" in calls


async def test_probe_redacts_credential_shaped_executable_path(
    fake_gh: Path, tmp_path: Path
) -> None:
    credential_like_name = "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    bin_dir = tmp_path / credential_like_name
    bin_dir.mkdir()
    executable = bin_dir / "gh"
    executable.write_text(fake_gh.read_text(encoding="utf-8"), encoding="utf-8")
    executable.chmod(0o755)
    data_dir = tmp_path / "path-redaction-data"
    data_dir.mkdir()

    status = await GitHubProbe(
        Config(data_dir=data_dir, gh_command=(str(executable),)), EventHub()
    ).probe()

    assert status.identity.state == "ready"
    assert credential_like_name not in (status.identity.executable_path or "")
    assert "[redacted]" in (status.identity.executable_path or "")


def _registered_task(
    engine, tmp_path: Path, *, upstream_url: str = "https://github.com/owner/repo"
) -> Task:
    checkout = tmp_path / "checkout"
    checkout.mkdir(exist_ok=True)
    create_project(
        engine,
        name="github-target",
        title="GitHub target",
        upstream_url=upstream_url,
        checkout_path=str(checkout),
        default_checkout_root=tmp_path,
    )
    return create_task(
        engine,
        project_name="github-target",
        slug="preflight",
        branch="ompire/preflight",
        clone_path=str(tmp_path / "tasks" / "github-target" / "preflight"),
        prompt="test",
    )


def test_gh_current_and_global_recheck_are_bearer_protected_and_safe(
    client: TestClient, auth_headers: dict[str, str]
) -> None:
    assert client.get("/api/gh").status_code == 401

    current = client.get("/api/gh", headers=auth_headers)
    refreshed = client.post("/api/gh/recheck", headers=auth_headers)

    assert current.status_code == 200
    assert refreshed.status_code == 200
    for response in (current, refreshed):
        data = response.json()
        assert data["identity"]["state"] == "ready"
        assert data["identity"]["login"] == "test-user"
        assert data["identity"]["credential_source"] == "GitHub CLI configuration"
        assert data["targets"] == {}


def test_gh_scoped_recheck_uses_registered_upstream_and_unknown_task_is_404(
    client: TestClient,
    auth_headers: dict[str, str],
    app,
    tmp_path: Path,
) -> None:
    task = _registered_task(app.state.engine, tmp_path)

    scoped = client.post(
        "/api/gh/recheck", headers=auth_headers, json={"task_id": task.id}
    )
    unknown = client.post(
        "/api/gh/recheck", headers=auth_headers, json={"task_id": 999_999}
    )

    assert scoped.status_code == 200
    target = scoped.json()["targets"]["github.com/owner/repo"]
    assert target["state"] == "allowed"
    assert target["target"] == {
        "host": "github.com",
        "owner": "owner",
        "repository": "repo",
    }
    assert unknown.status_code == 404


def test_gh_recheck_broadcasts_complete_status_and_reconnect_reads_snapshot(
    client: TestClient,
    auth_token: str,
    auth_headers: dict[str, str],
    app,
    tmp_path: Path,
) -> None:
    task = _registered_task(app.state.engine, tmp_path)
    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        snapshot = ws.receive_json()
        assert snapshot["payload"]["gh"]["identity"]["state"] == "ready"
        response = client.post(
            "/api/gh/recheck", headers=auth_headers, json={"task_id": task.id}
        )
        assert response.status_code == 200
        event = ws.receive_json()
        assert event["type"] == "gh_status"
        assert (
            event["payload"]["gh"]["targets"]["github.com/owner/repo"]["state"]
            == "allowed"
        )

    with client.websocket_connect(f"/api/ws?token={auth_token}") as reconnected:
        snapshot = reconnected.receive_json()
        assert snapshot["type"] == "snapshot"
        assert (
            snapshot["payload"]["gh"]["targets"]["github.com/owner/repo"]["state"]
            == "allowed"
        )


def test_recheck_redacts_github_failures_from_rest_and_websocket(
    client: TestClient,
    auth_token: str,
    auth_headers: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "rest-websocket-exact-secret"
    fake = tmp_path / "fake-bin" / "gh"
    fake.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        "'--version') echo 'gh version 2.97.0 (test)' ;;\n"
        "'api --hostname github.com user')\n"
        "  printf 'HTTP 401: Bad credentials\\nAuthorization: Weird %s\\ngithub_pat_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789\\n' \"$GH_TOKEN\" >&2\n"
        "  exit 1 ;;\n"
        "*) echo unsupported >&2; exit 1 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("GH_TOKEN", secret)

    with client.websocket_connect(f"/api/ws?token={auth_token}") as ws:
        ws.receive_json()  # startup snapshot was produced before the injected failure.
        response = client.post("/api/gh/recheck", headers=auth_headers)
        event = ws.receive_json()

    published = f"{response.text}\n{event}"
    assert response.status_code == 200
    assert event["type"] == "gh_status"
    assert event["payload"]["gh"]["identity"]["state"] == "unauthenticated"
    assert secret not in published
    assert "github_pat_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789" not in published
    assert "Authorization: Weird" not in published
    assert "[redacted]" in published
