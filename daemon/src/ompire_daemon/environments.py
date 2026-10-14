"""Operator-declared environments: immutable definitions and read-only probes.

Infrastructure remains TOML-only (ADR-0013). Only the check argv is executed;
provisioning and the safety of declared argv/diagnostics belong to the operator.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import signal
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ompire_daemon.config import Config
    from ompire_daemon.events import EventHub

PROBE_TIMEOUT = 60.0
MAX_DIAGNOSTIC_LENGTH = 1000

STATE_UNKNOWN = "unknown"
STATE_READY = "ready"
STATE_DEGRADED = "degraded"
STATE_MISSING = "missing"
STATE_ERROR = "error"

PROVISION_SCRIPT_MISSING = "provision-script-missing"
CHECK_PROGRAM_MISSING = "check-program-missing"
CHECK_FAILED = "check-failed"
CHECK_TIMED_OUT = "check-timed-out"
CHECK_ERROR = "check-error"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_HOSTS = frozenset({"container", "vm", "host"})
_KEYS = frozenset({"host", "capabilities", "provision", "check"})


@dataclass(frozen=True)
class EnvironmentDefinition:
    name: str
    host: str
    provision: tuple[str, ...]
    check: tuple[str, ...]
    capabilities: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # A frozen dataclass alone would still expose mutable nested values.
        object.__setattr__(self, "capabilities", MappingProxyType(dict(self.capabilities)))
        object.__setattr__(self, "provision", tuple(self.provision))
        object.__setattr__(self, "check", tuple(self.check))


@dataclass(frozen=True)
class EnvironmentBlocker:
    code: str
    message: str


@dataclass(frozen=True)
class EnvironmentStatus:
    state: str = STATE_UNKNOWN
    blockers: tuple[EnvironmentBlocker, ...] = ()
    checked_at: str | None = None


def parse_environments(value: Any) -> tuple[EnvironmentDefinition, ...]:
    """Validate the complete environments table, naming offending dotted keys."""
    # Config delegates here; importing only while parsing avoids a module cycle.
    from ompire_daemon.config import ConfigError

    if not isinstance(value, dict):
        raise ConfigError("config key 'environments' must be a table")
    definitions = []
    for name, table in value.items():
        key = f"environments.{name}"
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
            raise ConfigError(f"config key '{key}' must have a kebab-case environment name")
        if not isinstance(table, dict):
            raise ConfigError(f"config key '{key}' must be a table")
        for child in table:
            if child not in _KEYS:
                raise ConfigError(f"unknown config key '{key}.{child}'")
        host = table.get("host")
        if not isinstance(host, str) or host not in _HOSTS:
            raise ConfigError(f"config key '{key}.host' must be container, vm, or host")

        commands: dict[str, tuple[str, ...]] = {}
        for command in ("provision", "check"):
            argv = table.get(command)
            if not isinstance(argv, list) or not argv:
                raise ConfigError(f"config key '{key}.{command}' must be a non-empty argv list")
            for index, arg in enumerate(argv):
                if not isinstance(arg, str) or not arg.strip() or "\0" in arg:
                    raise ConfigError(
                        f"config key '{key}.{command}.{index}' must be a non-empty string without NUL"
                    )
            commands[command] = (os.path.expanduser(argv[0]), *argv[1:])

        capabilities = table.get("capabilities", {})
        if not isinstance(capabilities, dict):
            raise ConfigError(f"config key '{key}.capabilities' must be a table")
        for capability, provider in capabilities.items():
            cap_key = f"{key}.capabilities.{capability}"
            if not isinstance(capability, str) or not capability.strip():
                raise ConfigError(f"config key '{cap_key}' must have a non-empty capability name")
            if not isinstance(provider, str) or not provider.strip():
                raise ConfigError(f"config key '{cap_key}' must be a non-empty provider string")
        definitions.append(
            EnvironmentDefinition(
                name=name,
                host=host,
                capabilities=capabilities,
                provision=commands["provision"],
                check=commands["check"],
            )
        )
    return tuple(definitions)


def _program_path(program: str) -> str | None:
    """Resolve PATH names, distinguishing absence from an unusable program."""
    program = os.path.expanduser(program)
    if os.path.dirname(program):
        candidates: tuple[str, ...] = (program,)
    else:
        found = shutil.which(program)
        if found is not None:
            return found
        # which() excludes non-executable files; those are execution errors,
        # not missing infrastructure. stat also preserves permission errors.
        candidates = tuple(str(Path(part) / program) for part in os.get_exec_path())
    for candidate in candidates:
        try:
            os.stat(candidate)
        except (FileNotFoundError, NotADirectoryError):
            continue
        return candidate
    return None


async def _drain_first_line(stream: asyncio.StreamReader) -> str:
    """Drain every byte, retaining only a bounded first non-empty line."""
    retained = bytearray()
    complete = False
    while chunk := await stream.read(4096):
        if complete:
            continue
        for byte in chunk:
            if byte in (10, 13):
                if retained.strip():
                    complete = True
                    break
                retained.clear()
            elif not retained and byte in (9, 11, 12, 32):
                continue
            elif len(retained) < MAX_DIAGNOSTIC_LENGTH * 4:
                retained.append(byte)
            elif retained.strip():
                complete = True
                break
    return retained.decode("utf-8", errors="replace").strip()[:MAX_DIAGNOSTIC_LENGTH]


@dataclass(frozen=True)
class _CheckResult:
    code: int
    stdout: str
    stderr: str


async def _run(argv: tuple[str, ...], timeout: float) -> _CheckResult:
    """Bound both execution and pipe draining; kill the whole check group."""
    spawn = asyncio.create_task(
        asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    )
    process: asyncio.subprocess.Process | None = None
    completion: asyncio.Future[Any] | None = None
    try:
        async with asyncio.timeout(timeout):
            # Shield spawn so a cancellation cannot lose ownership of a child
            # created just as the deadline fires.
            process = await asyncio.shield(spawn)
            assert process.stdout is not None and process.stderr is not None
            completion = asyncio.gather(
                process.wait(),
                _drain_first_line(process.stdout),
                _drain_first_line(process.stderr),
            )
            # Readers must keep draining during cleanup. Cancelling them can
            # leave a full pipe paused and make process.wait() hang after kill.
            code, stdout, stderr = await asyncio.shield(completion)
            return _CheckResult(code, stdout, stderr)
    finally:
        if process is None:
            process = await spawn
        # The leader may have exited while a descendant still holds a pipe.
        # SIGKILL is intentional: a check cannot extend its fixed deadline.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        if completion is None:
            assert process.stdout is not None and process.stderr is not None
            completion = asyncio.gather(
                process.wait(),
                _drain_first_line(process.stdout),
                _drain_first_line(process.stderr),
            )
        await completion


class EnvironmentRegistry:
    """Cached observations, serialized per environment and published in full."""

    def __init__(
        self, config: Config, hub: EventHub, *, timeout: float | None = None
    ) -> None:
        self._definitions = {definition.name: definition for definition in config.environments}
        self._statuses = {name: EnvironmentStatus() for name in self._definitions}
        self._locks = {name: asyncio.Lock() for name in self._definitions}
        self._hub = hub
        self._timeout = PROBE_TIMEOUT if timeout is None else timeout

    def _info(self, name: str) -> dict[str, Any]:
        definition = self._definitions[name]
        status = self._statuses[name]
        return {
            "name": name,
            "host": definition.host,
            "capabilities": dict(definition.capabilities),
            "provision": list(definition.provision),
            "check": list(definition.check),
            "status": {
                "state": status.state,
                "blockers": [
                    {"code": blocker.code, "message": blocker.message}
                    for blocker in status.blockers
                ],
                "checked_at": status.checked_at,
            },
        }

    def current(self) -> list[dict[str, Any]]:
        """Return every definition and cached result, without running commands."""
        return [self._info(name) for name in self._definitions]

    async def probe(self, name: str) -> dict[str, Any]:
        """Probe one known environment; unknown names raise KeyError."""
        async with self._locks[name]:
            self._statuses[name] = await self._do_probe(self._definitions[name])
            info = self._info(name)
            self._hub.publish("environment_status", {"environment": info})
            return info

    async def probe_all(self) -> list[dict[str, Any]]:
        """Prime declarations sequentially, each with its own bounded deadline."""
        for name in self._definitions:
            await self.probe(name)
        return self.current()

    async def _do_probe(self, definition: EnvironmentDefinition) -> EnvironmentStatus:
        blockers = []
        state = STATE_READY
        provision_missing = False
        try:
            if _program_path(definition.provision[0]) is None:
                provision_missing = True
                blockers.append(
                    EnvironmentBlocker(
                        PROVISION_SCRIPT_MISSING,
                        f"provision program not found: {definition.provision[0]}",
                    )
                )
            program = _program_path(definition.check[0])
            if program is None:
                state = STATE_MISSING
                blockers.append(
                    EnvironmentBlocker(
                        CHECK_PROGRAM_MISSING,
                        f"check program not found: {definition.check[0]}",
                    )
                )
            else:
                result = await _run((program, *definition.check[1:]), self._timeout)
                if result.code != 0:
                    state = STATE_DEGRADED
                    blockers.append(
                        EnvironmentBlocker(
                            CHECK_FAILED,
                            result.stderr or result.stdout or f"check exited with status {result.code}",
                        )
                    )
        except TimeoutError:
            state = STATE_DEGRADED
            blockers.append(
                EnvironmentBlocker(CHECK_TIMED_OUT, f"check exceeded {self._timeout:g}s deadline")
            )
        except (OSError, ValueError) as exc:
            state = STATE_ERROR
            detail = str(exc).splitlines()
            blockers.append(
                EnvironmentBlocker(
                    CHECK_ERROR,
                    (detail[0] if detail else "check could not be executed")[:MAX_DIAGNOSTIC_LENGTH],
                )
            )
        return EnvironmentStatus(
            state=STATE_MISSING if provision_missing else state,
            blockers=tuple(blockers),
            checked_at=datetime.now(UTC).isoformat(),
        )
