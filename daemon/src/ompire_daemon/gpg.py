"""GPG signing-key lock probe (SPEC Decision 7, design D-4).

Architecture: ADR-0011
(docs/adr/0011-keep-review-and-publishing-authority-outside-agent-sandbox.md)

Derives the signing subkey's keygrip once, then probes `gpg-agent` via
`gpg-connect-agent KEYINFO --no-ask` without triggering a pinentry.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ompire_daemon.review import _run_git_output

if TYPE_CHECKING:
    from ompire_daemon.config import Config
    from ompire_daemon.events import EventHub

logger = logging.getLogger(__name__)

_KEYGRIP_RE = re.compile(r"^Keygrip\s*=\s*([0-9A-Fa-f]+)")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class GpgStatus:
    state: str  # cached | locked | unknown
    key: str | None
    keygrip: str | None
    detail: str | None
    checked_at: str = field(default_factory=_now_iso)


class GpgProbe:
    """Shared lock condition for the chrome chip and the ship commit gate."""

    def __init__(self, config: Config, hub: EventHub) -> None:
        self._config = config
        self._hub = hub
        self._key: str | None = None
        self._keygrip: str | None = None
        self._status: GpgStatus | None = None

    def current(self) -> GpgStatus:
        """Return the most recently probed status without re-probing."""
        if self._status is None:
            return GpgStatus(
                state="unknown",
                key=self._key,
                keygrip=self._keygrip,
                detail="not probed yet",
            )
        return self._status

    async def probe(self) -> GpgStatus:
        """Refresh the lock status and broadcast it on the hub."""
        status = await self._do_probe()
        self._status = status
        self._hub.publish("gpg_status", {"status": asdict(status)})
        return status

    # --- internals ---------------------------------------------------------

    async def _do_probe(self) -> GpgStatus:
        key = await self._resolve_key()
        if key is None:
            return GpgStatus(
                state="unknown",
                key=None,
                keygrip=None,
                detail="no signing key configured",
            )
        self._key = key

        keygrip = await self._derive_keygrip(key)
        self._keygrip = keygrip
        if keygrip is None:
            return GpgStatus(
                state="unknown",
                key=key,
                keygrip=None,
                detail="could not find signing subkey keygrip",
            )

        return await self._probe_keyinfo(key, keygrip)

    async def _resolve_key(self) -> str | None:
        if self._config.gpg_signing_key is not None:
            return self._config.gpg_signing_key
        try:
            out = await _run_git_output(
                ["git", "config", "--get", "user.signingkey"],
                cwd=".",
                timeout=10,
                step_name="git-config-signingkey",
            )
        except Exception as exc:  # noqa: BLE001 — missing git config is normal
            logger.debug("git config user.signingkey lookup failed: %s", exc)
            return None
        value = out.strip()
        return value if value else None

    async def _derive_keygrip(self, key: str) -> str | None:
        try:
            stdout = await _run_git_output(
                ["gpg", "--with-keygrip", "--list-secret-keys", key],
                cwd=".",
                timeout=10,
                step_name="gpg-keygrip",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("gpg keygrip lookup failed for %r: %s", key, exc)
            return None
        return _parse_signing_keygrip(stdout)

    async def _probe_keyinfo(self, key: str, keygrip: str) -> GpgStatus:
        try:
            stdout = await _run_git_output(
                ["gpg-connect-agent", f"KEYINFO --no-ask {keygrip}", "/bye"],
                cwd=".",
                timeout=10,
                step_name="gpg-keyinfo",
            )
        except Exception as exc:  # noqa: BLE001
            detail = f"KEYINFO probe failed: {exc}"
            logger.warning(detail)
            return GpgStatus(state="unknown", key=key, keygrip=keygrip, detail=detail)

        for line in stdout.splitlines():
            if not line.startswith("S KEYINFO "):
                continue
            parts = line.split()
            # S KEYINFO <keygrip> <type> <serialno> <idstr> <cached> <protection> ...
            if len(parts) < 7:
                continue
            cached = parts[6]
            state = "cached" if cached == "1" else "locked"
            return GpgStatus(
                state=state,
                key=key,
                keygrip=keygrip,
                detail=None,
            )

        return GpgStatus(
            state="unknown",
            key=key,
            keygrip=keygrip,
            detail="no KEYINFO status line in gpg-connect-agent output",
        )


def _parse_signing_keygrip(output: str) -> str | None:
    """Find the keygrip of the signing-capable secret subkey.

    Sample block:
        sec   rsa2048 ... [SCEAR]
              <fingerprint>
              Keygrip = <primary keygrip>
        uid ...
        ssb   rsa2048 ... [SEA]
              <fingerprint>
              Keygrip = <signing subkey keygrip>

    We want the `ssb` line whose capability string includes `S`, then the
    first `Keygrip =` line that follows it.
    """
    lines = output.splitlines()
    for i, line in enumerate(lines):
        if not line.startswith("ssb"):
            continue
        if not _has_sign_capability(line):
            continue
        for j in range(i + 1, len(lines)):
            match = _KEYGRIP_RE.match(lines[j].strip())
            if match:
                return match.group(1).upper()
    return None


def _has_sign_capability(line: str) -> bool:
    start = line.find("[")
    end = line.find("]", start)
    if start == -1 or end == -1:
        return False
    return "S" in line[start + 1 : end]
