"""GPG signing-key enumeration, selection, and agent lock probe.

Architecture: ADR-0011
(docs/adr/0011-keep-review-and-publishing-authority-outside-agent-sandbox.md)
and ADR-0021
(docs/adr/0021-admit-signing-key-selection-as-bounded-daemon-writable-setting.md)

Two non-prompting GPG calls back the one shared status the chrome chip, the
Settings panel, and the ship commit gate all read:

1. ``gpg --list-secret-keys --with-colons --with-keygrip`` enumerates
   signing-capable secret keys.
2. ``gpg-connect-agent KEYINFO --no-ask`` classifies the *selected* key only.

Neither can raise a pinentry.  `gpg-connect-agent` exits 0 even when the agent
is unreachable or the keygrip is unknown, so this module classifies from its
output rather than from an exit code.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ompire_daemon.config import Config
    from ompire_daemon.events import EventHub
    from ompire_daemon.registry.settings import SettingsStore

logger = logging.getLogger(__name__)

_TIMEOUT = 10

# A full OpenPGP v4 fingerprint.  The settings override is bounded to this
# form so a stored selection always names exactly one key (ADR-0021).
FINGERPRINT_RE = re.compile(r"^[0-9A-Fa-f]{40}$")

# Secret-key validity letters that make a key unusable for signing.  Trust
# levels (q/n/m/f/u/o/-) are about *other people's* keys and never stop the
# operator signing with their own.
_UNUSABLE_VALIDITY = frozenset("idre")

# Colon-record field 15: "#" marks a stub whose secret half is absent, so the
# agent cannot sign with it.  "+" is a real secret key, ">" lives on a card.
_NO_SECRET_KEY = "#"

# States, ordered roughly from "nothing known" to "usable".
STATE_UNKNOWN = "unknown"
STATE_MISSING = "missing"
STATE_NO_KEY = "no_key"
STATE_AMBIGUOUS = "ambiguous"
STATE_AGENT_UNAVAILABLE = "agent_unavailable"
STATE_LOCKED = "locked"
STATE_READY = "ready"
STATE_ERROR = "error"

# Selection provenance, mirroring the settings store's own vocabulary.
SOURCE_OVERRIDE = "override"
SOURCE_CONFIG = "config"
SOURCE_GIT = "git"
SOURCE_AUTO = "auto"

_AGENT_DOWN_MARKERS = (
    "no agent running",
    "can't connect to the gpg-agent",
    "no gpg-agent running",
    "agent refused operation",
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class GpgCandidate:
    """A signing-capable secret key. Public identifiers only."""

    fingerprint: str
    key_id: str
    uid: str | None
    keygrip: str
    created_at: str | None
    expires_at: str | None
    # The primary key this one belongs to (itself, for a signing primary).
    # Operators name the primary far more often than the signing subkey,
    # and so does `gpg --list-secret-keys` output, so identifier matching
    # has to accept it.
    primary_fingerprint: str


@dataclass(frozen=True)
class GpgSelection:
    """The key the daemon will sign with, and where the choice came from."""

    fingerprint: str
    key_id: str
    uid: str | None
    keygrip: str
    source: str  # override | config | git | auto
    protection: str | None  # protected | unprotected | None when unclassified


@dataclass(frozen=True)
class GpgStatus:
    """The complete published signing status. It never carries key material."""

    state: str
    selected: GpgSelection | None = None
    candidates: tuple[GpgCandidate, ...] = ()
    cache_ttl: int | None = None
    detail: str | None = None
    checked_at: str = field(default_factory=_now_iso)


class GpgProbe:
    """Shared signing condition for the chip, Settings, and the ship gate."""

    def __init__(
        self,
        config: Config,
        hub: EventHub,
        settings: SettingsStore | None = None,
    ) -> None:
        self._config = config
        self._hub = hub
        self._settings = settings
        self._status: GpgStatus | None = None

    def current(self) -> GpgStatus:
        """Return the most recently probed status without re-probing."""
        if self._status is None:
            return GpgStatus(state=STATE_UNKNOWN, detail="not probed yet")
        return self._status

    def candidates(self) -> tuple[GpgCandidate, ...]:
        """Signing-capable keys seen by the last probe.

        The REST layer bounds a settings override to this set, so a stored
        selection can only ever name a key the host keyring already holds.
        """
        return self.current().candidates

    async def probe(self) -> GpgStatus:
        """Refresh the signing status and broadcast it on the hub."""
        status = await self._do_probe()
        self._status = status
        self._hub.publish("gpg_status", {"status": asdict(status)})
        return status

    # --- internals ---------------------------------------------------------

    async def _do_probe(self) -> GpgStatus:
        try:
            listing = await _run(
                ["gpg", "--list-secret-keys", "--with-colons", "--with-keygrip"]
            )
        except _ExecMissingError as exc:
            return GpgStatus(state=STATE_MISSING, detail=str(exc))

        if listing.code != 0 and not listing.stdout.strip():
            return GpgStatus(
                state=STATE_ERROR,
                detail=f"listing secret keys failed: {_first_line(listing.stderr)}",
            )

        candidates = parse_candidates(listing.stdout)
        if not candidates:
            return GpgStatus(
                state=STATE_NO_KEY,
                detail="no signing-capable secret key in the daemon's keyring",
            )

        selected, state, detail = await self._select(candidates)
        if selected is None:
            return GpgStatus(state=state, candidates=candidates, detail=detail)

        return await self._classify(selected, candidates)

    async def _select(
        self, candidates: tuple[GpgCandidate, ...]
    ) -> tuple[GpgSelection | None, str, str | None]:
        """Resolve override → config.toml → git config → automatic."""
        override = self._override()
        if override is not None:
            match = _match_fingerprint(candidates, override)
            if match is None:
                return (
                    None,
                    STATE_ERROR,
                    (
                        f"selected key {override} is no longer a usable signing "
                        "key in the daemon's keyring; choose another in "
                        "Templates & settings"
                    ),
                )
            return _select_from(match, SOURCE_OVERRIDE), STATE_READY, None

        # Lower layers, cheapest first: reading git config costs a subprocess,
        # so it only runs when config.toml said nothing.
        value = self._config.gpg_signing_key
        source = SOURCE_CONFIG
        if not value:
            value = await self._git_signing_key()
            source = SOURCE_GIT

        if value:
            matches = _match_identifier(candidates, value)
            if len(matches) == 1:
                return _select_from(matches[0], source), STATE_READY, None
            if len(matches) > 1:
                return (
                    None,
                    STATE_AMBIGUOUS,
                    (
                        f"{_where(source)} names {value!r}, which matches "
                        f"{len(matches)} signing keys; choose one in "
                        "Templates & settings"
                    ),
                )
            return (
                None,
                STATE_ERROR,
                (
                    f"{_where(source)} names {value!r}, which is not a usable "
                    "signing key in the daemon's keyring"
                ),
            )

        if len(candidates) == 1:
            return _select_from(candidates[0], SOURCE_AUTO), STATE_READY, None
        return (
            None,
            STATE_AMBIGUOUS,
            (
                f"{len(candidates)} usable signing keys; "
                "choose one in Templates & settings"
            ),
        )

    def _override(self) -> str | None:
        if self._settings is None:
            return None
        try:
            value = self._settings.effective().get("gpg_signing_key")
        except Exception as exc:  # noqa: BLE001 — a broken store must not hide keys
            logger.warning("reading gpg_signing_key override failed: %s", exc)
            return None
        return value.upper() if isinstance(value, str) and value else None

    async def _git_signing_key(self) -> str | None:
        try:
            result = await _run(["git", "config", "--get", "user.signingkey"])
        except _ExecMissingError:
            return None
        if result.code != 0:
            return None
        value = result.stdout.strip()
        return value or None

    async def _classify(
        self, selected: GpgSelection, candidates: tuple[GpgCandidate, ...]
    ) -> GpgStatus:
        """Ask the agent about the selected key without offering to unlock it."""
        try:
            result = await _run(
                [
                    "gpg-connect-agent",
                    f"KEYINFO --no-ask {selected.keygrip}",
                    "/bye",
                ]
            )
        except _ExecMissingError as exc:
            return GpgStatus(
                state=STATE_MISSING,
                selected=selected,
                candidates=candidates,
                detail=str(exc),
            )

        info = parse_keyinfo(result.stdout, selected.keygrip)
        if info is None:
            haystack = f"{result.stderr}\n{result.stdout}".lower()
            if any(marker in haystack for marker in _AGENT_DOWN_MARKERS):
                return GpgStatus(
                    state=STATE_AGENT_UNAVAILABLE,
                    selected=selected,
                    candidates=candidates,
                    detail="gpg-agent is not reachable",
                )
            return GpgStatus(
                state=STATE_ERROR,
                selected=selected,
                candidates=candidates,
                detail=(
                    "gpg-agent did not report the selected key: "
                    f"{_first_line(result.stderr) or _first_line(result.stdout) or 'no KEYINFO line'}"
                ),
            )

        cached, protection, ttl = info
        bound = GpgSelection(
            fingerprint=selected.fingerprint,
            key_id=selected.key_id,
            uid=selected.uid,
            keygrip=selected.keygrip,
            source=selected.source,
            protection=protection,
        )
        if protection == "unprotected":
            # Nothing to cache: the key signs on demand.  Reporting this
            # `locked` is the bug that made passphrase-less keys unshippable.
            return GpgStatus(
                state=STATE_READY, selected=bound, candidates=candidates
            )
        if cached:
            return GpgStatus(
                state=STATE_READY,
                selected=bound,
                candidates=candidates,
                cache_ttl=ttl,
            )
        return GpgStatus(state=STATE_LOCKED, selected=bound, candidates=candidates)


# --- parsing ---------------------------------------------------------------


def parse_candidates(output: str) -> tuple[GpgCandidate, ...]:
    """Signing-capable secret keys from ``--with-colons --with-keygrip``.

    A record qualifies when its own (lowercase) capability letters include
    ``s``, its validity is usable, and its secret half is present.  The
    uppercase letters describe the whole keypair, so a certify-only primary
    with a signing subkey correctly yields the subkey and not the primary.
    """
    candidates: list[GpgCandidate] = []
    pending: dict[str, str | None] | None = None
    primary_uid: str | None = None
    primary_fpr: str | None = None
    awaiting_primary_fpr = False

    def flush() -> None:
        nonlocal pending
        if pending is None:
            return
        fingerprint = pending.get("fingerprint")
        keygrip = pending.get("keygrip")
        if fingerprint and keygrip:
            candidates.append(
                GpgCandidate(
                    fingerprint=fingerprint.upper(),
                    key_id=pending.get("key_id") or fingerprint[-16:].upper(),
                    uid=pending.get("uid"),
                    keygrip=keygrip.upper(),
                    created_at=pending.get("created_at"),
                    expires_at=pending.get("expires_at"),
                    primary_fingerprint=(
                        pending.get("primary_fingerprint") or fingerprint
                    ).upper(),
                )
            )
        pending = None

    for line in output.splitlines():
        fields = line.split(":")
        record = fields[0] if fields else ""

        if record in ("sec", "ssb"):
            flush()
            if record == "sec":
                primary_uid = None
                primary_fpr = None
                awaiting_primary_fpr = True
            pending = (
                {
                    "key_id": _field(fields, 5) or None,
                    "created_at": _epoch_to_iso(_field(fields, 6)),
                    "expires_at": _epoch_to_iso(_field(fields, 7)),
                    "uid": primary_uid,
                    "fingerprint": None,
                    "keygrip": None,
                    "primary_fingerprint": primary_fpr,
                }
                if _is_signing_capable(fields)
                else None
            )
        elif record == "fpr":
            value = _field(fields, 10) or None
            # The primary's own fpr record is tracked even when the primary
            # is not itself a candidate (a certify-only primary is not).
            if awaiting_primary_fpr:
                primary_fpr = value
                awaiting_primary_fpr = False
                if pending is not None:
                    pending["primary_fingerprint"] = value
            if pending is not None:
                pending["fingerprint"] = value
        elif record == "grp" and pending is not None:
            pending["keygrip"] = _field(fields, 10) or None
        elif record == "uid":
            uid = _field(fields, 10) or None
            if primary_uid is None:
                primary_uid = uid
                # A uid record closes the primary's own fpr/grp run.
                if pending is not None and pending.get("uid") is None:
                    pending["uid"] = uid
            flush()

    flush()
    return tuple(candidates)


def _is_signing_capable(fields: list[str]) -> bool:
    validity = _field(fields, 2)
    if validity and validity[0] in _UNUSABLE_VALIDITY:
        return False
    if _field(fields, 15) == _NO_SECRET_KEY:
        return False
    capabilities = _field(fields, 12)
    # Lowercase letters are this key's own capabilities; uppercase summarize
    # the whole keypair and would wrongly promote a certify-only primary.
    return "s" in "".join(c for c in capabilities if c.islower())


def parse_keyinfo(
    stdout: str, keygrip: str
) -> tuple[bool, str | None, int | None] | None:
    """Return ``(cached, protection, ttl)`` for `keygrip`, or None if absent.

    The agent's line is::

        S KEYINFO <keygrip> <type> <serialno> <idstr> <cached> <protection> \
<fpr> <ttl> <flags>

    `protection` is ``P`` for passphrase-protected, ``C`` for unprotected, and
    ``-`` when the agent will not say.  Reading only `cached` — as the earlier
    probe did — cannot tell an unprotected key from a cold one.
    """
    for line in stdout.splitlines():
        if not line.startswith("S KEYINFO "):
            continue
        parts = line.split()
        if len(parts) < 8 or parts[2].upper() != keygrip.upper():
            continue
        cached = parts[6] == "1"
        raw_protection = parts[7]
        protection = {"P": "protected", "C": "unprotected"}.get(raw_protection)
        ttl: int | None = None
        if len(parts) >= 10:
            try:
                parsed = int(parts[9])
            except ValueError:
                parsed = 0
            ttl = parsed if parsed > 0 else None
        return cached, protection, ttl
    return None


def _field(fields: list[str], index: int) -> str:
    """One-indexed colon field, empty when the record is shorter."""
    return fields[index - 1] if len(fields) >= index else ""


def _epoch_to_iso(value: str) -> str | None:
    if not value or not value.isdigit():
        return None
    try:
        return datetime.fromtimestamp(int(value), UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _match_fingerprint(
    candidates: tuple[GpgCandidate, ...], fingerprint: str
) -> GpgCandidate | None:
    wanted = fingerprint.upper()
    for candidate in candidates:
        if candidate.fingerprint == wanted:
            return candidate
    return None


def _match_identifier(
    candidates: tuple[GpgCandidate, ...], value: str
) -> list[GpgCandidate]:
    """Match a `config.toml`/git identifier the way an operator writes one.

    GPG accepts fingerprints, long and short key IDs, and user-ID substrings;
    the override path is deliberately narrower (full fingerprint only).
    """
    wanted = value.strip().upper().replace(" ", "")
    wanted = wanted.removeprefix("0X")

    # Match the signing key itself or the primary it belongs to: `gpg -u`
    # accepts either, and operators usually have the primary's identifier.
    exact = [
        c
        for c in candidates
        if wanted in (c.fingerprint, c.primary_fingerprint)
    ]
    if exact:
        return exact
    if wanted and all(ch in "0123456789ABCDEF" for ch in wanted):
        suffix = [
            c
            for c in candidates
            if c.fingerprint.endswith(wanted)
            or c.primary_fingerprint.endswith(wanted)
        ]
        if suffix:
            return suffix
    needle = value.strip().lower()
    return [c for c in candidates if c.uid and needle in c.uid.lower()]


def _select_from(candidate: GpgCandidate, source: str) -> GpgSelection:
    return GpgSelection(
        fingerprint=candidate.fingerprint,
        key_id=candidate.key_id,
        uid=candidate.uid,
        keygrip=candidate.keygrip,
        source=source,
        protection=None,
    )


def gpg_signing_refusal(status: GpgStatus) -> str:
    """One sentence naming why signing is refused, for a `409` or ship error.

    The gate fails closed on every state but `ready`, so the operator needs
    the actual condition rather than a generic "not cached" (ADR-0011).
    """
    reasons = {
        STATE_READY: "GPG signing key is usable",
        STATE_LOCKED: "GPG signing key is locked; warm its passphrase cache",
        STATE_AMBIGUOUS: (
            "no GPG signing key is selected; choose one in Templates & settings"
        ),
        STATE_NO_KEY: "no signing-capable GPG key is available to the daemon",
        STATE_MISSING: "the GPG command-line tools are unavailable to the daemon",
        STATE_AGENT_UNAVAILABLE: (
            "gpg-agent is unreachable; start it with `gpg-connect-agent /bye`"
        ),
        STATE_UNKNOWN: "the GPG signing key has not been checked yet",
    }
    message = reasons.get(status.state, "the GPG signing key state is indeterminate")
    if status.detail:
        return f"{message} ({status.detail})"
    return message


def _where(source: str) -> str:
    return "config.toml" if source == SOURCE_CONFIG else "git config user.signingkey"


def _first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


# --- process boundary ------------------------------------------------------


class _ExecMissingError(Exception):
    """The binary itself could not be executed."""


@dataclass(frozen=True)
class _Result:
    stdout: str
    stderr: str
    code: int


async def _run(argv: list[str]) -> _Result:
    """Run a bounded, non-interactive command and capture everything.

    Unlike the review helper, a non-zero exit is *data* here: the probe must
    classify a failure rather than collapse it into one exception.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise _ExecMissingError(f"cannot execute {argv[0]!r}: {exc}") from exc
    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=_TIMEOUT
        )
    except TimeoutError:
        if process.returncode is None:
            process.kill()
        return _Result("", f"{argv[0]} timed out after {_TIMEOUT}s", 124)
    return _Result(
        stdout_bytes.decode("utf-8", errors="replace"),
        stderr_bytes.decode("utf-8", errors="replace"),
        process.returncode if process.returncode is not None else 0,
    )
