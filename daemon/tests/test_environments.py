"""Environment declarations and real, bounded read-only check commands."""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from ompire_daemon.config import Config, ConfigError, load_config
from ompire_daemon.environments import (
    MAX_DIAGNOSTIC_LENGTH,
    EnvironmentDefinition,
    EnvironmentRegistry,
    parse_environments,
)
from ompire_daemon.events import EventHub


def declaration(**overrides: Any) -> dict[str, Any]:
    return {
        "host": "host",
        "provision": ["/bin/true"],
        "check": ["/bin/true"],
        **overrides,
    }


def script(tmp_path: Path, name: str, body: str, *, python: bool = False) -> Path:
    path = tmp_path / name
    interpreter = sys.executable if python else "/bin/sh"
    path.write_text(f"#!{interpreter}\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def registry_for(
    check: Path | str,
    *,
    provision: Path | str = "/bin/true",
    hub: EventHub | None = None,
    timeout: float = 2,
) -> EnvironmentRegistry:
    definitions = parse_environments(
        {"qa-env": declaration(check=[str(check)], provision=[str(provision)])}
    )
    return EnvironmentRegistry(Config(environments=definitions), hub or EventHub(), timeout=timeout)


@pytest.mark.parametrize("host", ["host", "container", "vm"])
def test_valid_declaration_preserves_commands_and_optional_capabilities(host: str) -> None:
    definitions = parse_environments(
        {
            "qa-1": declaration(
                host=host,
                capabilities={"model": "auth-gateway", "forge": "forge-broker"},
                check=["qa-check", "--probe"],
            ),
            "2": declaration(host=host),
        }
    )
    assert [definition.name for definition in definitions] == ["qa-1", "2"]
    assert definitions[0].host == host
    assert definitions[0].check == ("qa-check", "--probe")
    assert definitions[0].capabilities == {"model": "auth-gateway", "forge": "forge-broker"}
    assert definitions[1].capabilities == {}
    assert parse_environments({}) == ()


@pytest.mark.parametrize(
    ("value", "key"),
    [
        (None, "environments"),
        ([], "environments"),
        ({"Qa": declaration()}, "environments.Qa"),
        ({"qa_env": declaration()}, "environments.qa_env"),
        ({"-qa": declaration()}, "environments.-qa"),
        ({"qa/other": declaration()}, "environments.qa/other"),
        ({"qa\n": declaration()}, "environments.qa"),
        ({"": declaration()}, "environments."),
        ({"qa": []}, "environments.qa"),
        ({"qa": declaration(extra=True)}, "environments.qa.extra"),
        ({"qa": declaration(host="baremetal")}, "environments.qa.host"),
        ({"qa": declaration(host=1)}, "environments.qa.host"),
        ({"qa": declaration(host=None)}, "environments.qa.host"),
        ({"qa": {}}, "environments.qa.host"),
        ({"qa": {"host": "host"}}, "environments.qa.provision"),
        ({"qa": {"host": "host", "provision": ["true"]}}, "environments.qa.check"),
        ({"qa": declaration(check=[])}, "environments.qa.check"),
        ({"qa": declaration(provision=[])}, "environments.qa.provision"),
        ({"qa": declaration(check="true")}, "environments.qa.check"),
        ({"qa": declaration(provision=1)}, "environments.qa.provision"),
        ({"qa": declaration(check=[""])}, "environments.qa.check.0"),
        ({"qa": declaration(check=["  "])}, "environments.qa.check.0"),
        ({"qa": declaration(check=["true", 1])}, "environments.qa.check.1"),
        ({"qa": declaration(provision=["true", ""])}, "environments.qa.provision.1"),
        ({"qa": declaration(check=["bad\0program"])}, "environments.qa.check.0"),
        ({"qa": declaration(capabilities=[])}, "environments.qa.capabilities"),
        ({"qa": declaration(capabilities={"": "provider"})}, "environments.qa.capabilities."),
        ({"qa": declaration(capabilities={" ": "provider"})}, "environments.qa.capabilities. "),
        ({"qa": declaration(capabilities={"model": ""})}, "environments.qa.capabilities.model"),
        ({"qa": declaration(capabilities={"model": " "})}, "environments.qa.capabilities.model"),
        ({"qa": declaration(capabilities={"model": 1})}, "environments.qa.capabilities.model"),
    ],
)
def test_invalid_declaration_names_the_offending_key(value: Any, key: str) -> None:
    with pytest.raises(ConfigError) as error:
        parse_environments(value)
    assert key in str(error.value)


def test_definition_is_deeply_immutable_and_detached_from_input() -> None:
    capabilities = {"browser": "chrome"}
    check = ["true"]
    definition = parse_environments(
        {"qa": declaration(capabilities=capabilities, check=check)}
    )[0]
    capabilities["browser"] = "changed"
    check[0] = "false"
    assert definition.capabilities == {"browser": "chrome"}
    assert definition.check == ("true",)
    with pytest.raises(FrozenInstanceError):
        definition.host = "vm"  # type: ignore[misc]
    with pytest.raises(TypeError):
        definition.capabilities["browser"] = "changed"  # type: ignore[index]


def test_direct_definition_freezes_nested_values() -> None:
    providers = {"browser": "chrome"}
    definition = EnvironmentDefinition("qa", "host", ("true",), ("true",), providers)
    providers.clear()
    assert definition.capabilities == {"browser": "chrome"}


def test_tilde_expands_only_program_not_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    definition = parse_environments(
        {"qa": declaration(check=["~/check", "~/argument"], provision=["~/provision"])}
    )[0]
    assert definition.check == (str(tmp_path / "check"), "~/argument")
    assert definition.provision == (str(tmp_path / "provision"),)


def test_load_config_accepts_complete_environment_table(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        '[environments.qa-env]\nhost = "container"\n'
        'provision = ["qa-setup"]\ncheck = ["qa-setup", "--probe"]\n'
        '[environments.qa-env.capabilities]\nmodel = "auth-gateway"\n'
        'forge = "forge-broker"\n',
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.environments == parse_environments(
        {
            "qa-env": declaration(
                host="container",
                provision=["qa-setup"],
                check=["qa-setup", "--probe"],
                capabilities={"model": "auth-gateway", "forge": "forge-broker"},
            )
        }
    )
    assert Config().environments == ()


@pytest.mark.parametrize(
    ("toml", "key"),
    [
        ('environments = []\n', "environments"),
        ('[environments.qa]\nhost="host"\nprovision=["true"]\ncheck=["true"]\nunknown=true\n', "environments.qa.unknown"),
        ('[environments.qa]\nhost="host"\nprovision=["true"]\ncheck=[]\n', "environments.qa.check"),
        ('[environments.qa]\nhost="host"\nprovision=["true"]\ncheck=["true"]\n[environments.qa.capabilities]\nmodel=1\n', "environments.qa.capabilities.model"),
    ],
)
def test_load_config_fails_closed_with_nested_key(tmp_path: Path, toml: str, key: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(toml, encoding="utf-8")
    with pytest.raises(ConfigError) as error:
        load_config(path)
    assert key in str(error.value)


async def test_ready_probe_and_cached_unknown_never_run_provision(tmp_path: Path) -> None:
    check_marker = tmp_path / "checked"
    provision_marker = tmp_path / "provisioned"
    check = script(tmp_path, "check", f'touch "{check_marker}"\nexit 0')
    provision = script(tmp_path, "provision", f'touch "{provision_marker}"')
    registry = registry_for(check, provision=provision)
    unknown = registry.current()[0]
    assert unknown["status"] == {"state": "unknown", "blockers": [], "checked_at": None}
    assert not check_marker.exists()
    result = await registry.probe("qa-env")
    assert result["status"]["state"] == "ready"
    assert result["status"]["blockers"] == []
    assert result["status"]["checked_at"] is not None
    assert check_marker.exists()
    assert not provision_marker.exists()
    assert registry.current() == [result]
    result["capabilities"]["new"] = "provider"
    result["check"].clear()
    result["status"]["state"] = "error"
    assert registry.current()[0]["status"]["state"] == "ready"
    assert registry.current()[0]["check"] == [str(check)]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ('printf "first diagnostic\\nlater detail\\n" >&2\nexit 1', "first diagnostic"),
        ('printf "stdout diagnostic\\nlater detail\\n"\nexit 7', "stdout diagnostic"),
        ('printf "stdout\\n"\nprintf "stderr\\n" >&2\nexit 1', "stderr"),
        ('printf "\\n  useful diagnostic  \\nlater\\n" >&2\nexit 1', "useful diagnostic"),
        ('exit 3', "check exited with status 3"),
    ],
)
async def test_nonzero_check_reports_first_diagnostic_line(
    tmp_path: Path, body: str, message: str
) -> None:
    check = script(tmp_path, "check", body)
    result = await registry_for(check).probe("qa-env")
    assert result["status"]["state"] == "degraded"
    assert result["status"]["blockers"] == [{"code": "check-failed", "message": message}]


async def test_large_output_is_drained_and_diagnostic_is_capped(tmp_path: Path) -> None:
    check = script(
        tmp_path,
        "check",
        "import os\n"
        "for _ in range(512):\n"
        "    os.write(1, b'x' * 4096)\n"
        "    os.write(2, b'd' * 4096)\n"
        "os.write(2, b'\\nsecond line\\n')\n"
        "raise SystemExit(1)",
        python=True,
    )
    result = await registry_for(check).probe("qa-env")
    assert result["status"]["state"] == "degraded"
    assert result["status"]["blockers"] == [
        {"code": "check-failed", "message": "d" * MAX_DIAGNOSTIC_LENGTH}
    ]


async def test_long_leading_whitespace_does_not_hide_first_diagnostic(tmp_path: Path) -> None:
    check = script(
        tmp_path,
        "check",
        "import os\nos.write(2, b' ' * 100000 + b'diagnostic\\nlater\\n')\nraise SystemExit(1)",
        python=True,
    )
    result = await registry_for(check).probe("qa-env")
    assert result["status"]["blockers"] == [{"code": "check-failed", "message": "diagnostic"}]


async def test_deadline_stays_bounded_while_both_pipes_are_saturated(tmp_path: Path) -> None:
    check = script(
        tmp_path,
        "check",
        "import os\nwhile True:\n    os.write(1, b'x' * 4096)\n    os.write(2, b'y' * 4096)",
        python=True,
    )
    result = await asyncio.wait_for(
        registry_for(check, timeout=0.1).probe("qa-env"), timeout=2
    )
    assert result["status"]["state"] == "degraded"
    assert result["status"]["blockers"][0]["code"] == "check-timed-out"


async def assert_not_running(pid: int) -> None:
    stat = Path(f"/proc/{pid}/stat")
    async with asyncio.timeout(1):
        while True:
            try:
                state = stat.read_text().split(") ", 1)[1].split()[0]
            except FileNotFoundError:
                return
            # An orphan can briefly remain a zombie until init reaps it.
            if state in {"Z", "X"}:
                return
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("leader_exits", [False, True])
async def test_deadline_kills_parent_and_descendants_holding_pipes(
    tmp_path: Path, leader_exits: bool
) -> None:
    parent_pid = tmp_path / "parent.pid"
    child_pid = tmp_path / "child.pid"
    check = script(
        tmp_path,
        "check",
        "import os, signal, subprocess, sys, time\n"
        "from pathlib import Path\n"
        f"Path({str(parent_pid)!r}).write_text(str(os.getpid()))\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"Path({str(child_pid)!r}).write_text(str(child.pid))\n"
        + ("raise SystemExit(0)" if leader_exits else "time.sleep(60)"),
        python=True,
    )
    registry = registry_for(check, timeout=0.2)
    started = time.monotonic()
    result = await asyncio.wait_for(registry.probe("qa-env"), timeout=2)
    assert time.monotonic() - started < 1.5
    assert result["status"]["state"] == "degraded"
    assert result["status"]["blockers"][0]["code"] == "check-timed-out"
    await assert_not_running(int(parent_pid.read_text()))
    await assert_not_running(int(child_pid.read_text()))


async def test_cancelled_probe_cleans_up_its_process_group(tmp_path: Path) -> None:
    pid_path = tmp_path / "pid"
    check = script(
        tmp_path,
        "check",
        "import os, time\nfrom pathlib import Path\n"
        f"Path({str(pid_path)!r}).write_text(str(os.getpid()))\ntime.sleep(60)",
        python=True,
    )
    registry = registry_for(check)
    job = asyncio.create_task(registry.probe("qa-env"))
    async with asyncio.timeout(2):
        while not pid_path.exists():
            await asyncio.sleep(0.01)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await job
    await assert_not_running(int(pid_path.read_text()))
    assert registry.current()[0]["status"]["state"] == "unknown"


@pytest.mark.parametrize("path_name", [True, False])
async def test_absent_check_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path_name: bool
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    program = str(tmp_path / "absent") if path_name else "absent"
    result = await registry_for(program).probe("qa-env")
    assert result["status"]["state"] == "missing"
    assert result["status"]["blockers"][0]["code"] == "check-program-missing"


@pytest.mark.parametrize("path_name", [True, False])
async def test_nonexecutable_check_is_error_not_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path_name: bool
) -> None:
    check = script(tmp_path, "check", "exit 0")
    check.chmod(0o644)
    monkeypatch.setenv("PATH", str(tmp_path))
    program = str(check) if path_name else check.name
    result = await registry_for(program).probe("qa-env")
    assert result["status"]["state"] == "error"
    assert result["status"]["blockers"][0]["code"] == "check-error"
    assert "Permission denied" in result["status"]["blockers"][0]["message"]


async def test_existing_script_with_missing_interpreter_is_execution_error(tmp_path: Path) -> None:
    check = tmp_path / "check"
    check.write_text("#!/absent/interpreter\n", encoding="utf-8")
    check.chmod(0o755)
    result = await registry_for(check).probe("qa-env")
    assert result["status"]["state"] == "error"
    assert result["status"]["blockers"][0]["code"] == "check-error"


async def test_plain_program_resolves_on_path_and_receives_literal_argv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    check = script(tmp_path, "qa-check", '[ "$1" = "a; b" ]')
    monkeypatch.setenv("PATH", str(tmp_path))
    config = Config(environments=parse_environments({"qa": declaration(check=[check.name, "a; b"])}))
    result = await EnvironmentRegistry(config, EventHub()).probe("qa")
    assert result["status"]["state"] == "ready"


@pytest.mark.parametrize("outcome", ["ready", "failed", "missing", "error", "timeout"])
async def test_absent_provision_wins_over_every_check_outcome(tmp_path: Path, outcome: str) -> None:
    check = script(tmp_path, "check", "sleep 60" if outcome == "timeout" else "exit 1" if outcome == "failed" else "exit 0")
    if outcome == "missing":
        check.unlink()
    elif outcome == "error":
        check.chmod(0o644)
    result = await registry_for(check, provision=tmp_path / "absent", timeout=0.1).probe("qa-env")
    assert result["status"]["state"] == "missing"
    assert result["status"]["blockers"][0]["code"] == "provision-script-missing"
    assert result["status"]["checked_at"] is not None


async def test_probes_publish_full_info_to_every_subscriber_and_probe_all_is_sequential(
    tmp_path: Path,
) -> None:
    log = tmp_path / "order"
    first = script(tmp_path, "first", f'printf "first\\n" >>"{log}"\nexit 1')
    second = script(tmp_path, "second", f'printf "second\\n" >>"{log}"\nexit 0')
    config = Config(environments=parse_environments({
        "first": declaration(check=[str(first)]),
        "second": declaration(check=[str(second)]),
    }))
    hub = EventHub()
    queues = [hub.subscribe(), hub.subscribe()]
    registry = EnvironmentRegistry(config, hub)
    result = await registry.probe_all()
    assert log.read_text().splitlines() == ["first", "second"]
    assert [info["status"]["state"] for info in result] == ["degraded", "ready"]
    for queue in queues:
        for info in result:
            event = queue.get_nowait()
            assert event.type == "environment_status"
            assert event.payload == {"environment": info}
        assert queue.empty()
        hub.unsubscribe(queue)


async def test_concurrent_same_environment_probes_are_serialized(tmp_path: Path) -> None:
    counter = tmp_path / "count"
    check = script(
        tmp_path,
        "check",
        "import time\nfrom pathlib import Path\n"
        f"counter = Path({str(counter)!r})\n"
        "count = int(counter.read_text()) if counter.exists() else 0\n"
        "counter.write_text(str(count + 1))\n"
        "if count == 0:\n    time.sleep(0.1)\n    raise SystemExit(1)\n",
        python=True,
    )
    hub = EventHub()
    queue = hub.subscribe()
    registry = registry_for(check, hub=hub)
    first, second = await asyncio.gather(registry.probe("qa-env"), registry.probe("qa-env"))
    assert first["status"]["state"] == "degraded"
    assert second["status"]["state"] == "ready"
    assert registry.current() == [second]
    assert queue.get_nowait().payload == {"environment": first}
    assert queue.get_nowait().payload == {"environment": second}
    hub.unsubscribe(queue)


async def test_empty_registry_and_unknown_probe_publish_nothing() -> None:
    hub = EventHub()
    queue = hub.subscribe()
    registry = EnvironmentRegistry(Config(), hub)
    assert registry.current() == []
    assert await registry.probe_all() == []
    with pytest.raises(KeyError):
        await registry.probe("absent")
    assert queue.empty()
    hub.unsubscribe(queue)
