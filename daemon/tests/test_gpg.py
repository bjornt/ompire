"""Tests for `ompire_daemon.gpg`.

Process boundaries are exercised with executable fakes on `PATH` (ADR-0014),
driven by output captured from real GnuPG in `gpg_fixtures`. The last section
drops the fakes and runs the probe against real `gpg` and real `gpg-agent`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ompire_daemon.config import Config
from ompire_daemon.events import EventHub
from ompire_daemon.gpg import (
    GpgProbe,
    gpg_signing_refusal,
    parse_candidates,
    parse_keyinfo,
)

from . import gpg_fixtures as fx


def _write_script(bin_dir: Path, name: str, content: str) -> None:
    script = bin_dir / name
    script.write_text(content, encoding="utf-8")
    script.chmod(0o755)


@pytest.fixture
def bin_dir(tmp_path: Path) -> Path:
    return tmp_path / "bin"


@pytest.fixture
def event_hub() -> EventHub:
    return EventHub()


def _install(
    monkeypatch,
    bin_dir: Path,
    *,
    listing: str = fx.PROTECTED_ONLY,
    keyinfo: dict[str, str] | None = None,
    agent_stderr: str = "",
    gpg_missing: bool = False,
    agent_missing: bool = False,
    listing_exit: int = 0,
    git_signing_key: str | None = None,
) -> None:
    """Put fake `gpg`, `gpg-connect-agent`, and `git` on PATH.

    `keyinfo` maps keygrip → the agent's response for that keygrip, so a
    multi-key listing can answer differently per key.
    """
    bin_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("PATH", str(bin_dir))

    def quote(text: str) -> str:
        """Single-quote for /bin/sh. GPG's own output contains apostrophes."""
        return "'" + text.replace("'", "'\\''") + "'"

    if not gpg_missing:
        _write_script(
            bin_dir,
            "gpg",
            f"#!/bin/sh\nprintf '%s' {quote(listing)}\nexit {listing_exit}\n",
        )

    if not agent_missing:
        cases = "".join(
            f"    *{grp}*) printf '%s' {quote(response)} ;;\n"
            for grp, response in (keyinfo or {}).items()
        )
        _write_script(
            bin_dir,
            "gpg-connect-agent",
            "#!/bin/sh\n"
            f"printf '%s' {quote(agent_stderr)} >&2\n"
            'case "$1" in\n'
            f"{cases}"
            "    *) : ;;\n"
            "esac\n",
        )

    _write_script(
        bin_dir,
        "git",
        "#!/bin/sh\n"
        'if [ "$1" = config ] && [ "$3" = user.signingkey ]; then\n'
        + (f"    echo '{git_signing_key}'\n    exit 0\n" if git_signing_key else "")
        + "    exit 1\n"
        "fi\n"
        "exit 1\n",
    )


# --- listing parser --------------------------------------------------------


def test_parses_protected_signing_subkey_not_certify_only_primary():
    candidates = parse_candidates(fx.PROTECTED_ONLY)

    assert [c.fingerprint for c in candidates] == [fx.PROTECTED_FPR]
    only = candidates[0]
    assert only.keygrip == fx.PROTECTED_KEYGRIP
    assert only.key_id == fx.PROTECTED_KEY_ID
    # The subkey inherits the primary's user ID for display, and remembers
    # the primary so an operator can still name it by that fingerprint.
    assert only.uid == fx.PROTECTED_UID
    assert only.primary_fingerprint == fx.PROTECTED_PRIMARY_FPR


def test_a_signing_primary_is_its_own_primary():
    only = parse_candidates(fx.UNPROTECTED_ONLY)[0]

    assert only.primary_fingerprint == only.fingerprint


def test_parses_passphrase_less_primary_that_signs_without_a_subkey():
    candidates = parse_candidates(fx.UNPROTECTED_ONLY)

    assert [c.fingerprint for c in candidates] == [fx.UNPROTECTED_FPR]
    assert candidates[0].uid == fx.UNPROTECTED_UID


@pytest.mark.parametrize(
    "listing",
    [fx.EXPIRED_ONLY, fx.REVOKED_ONLY, fx.STUB_SECRET_ONLY, fx.EMPTY],
    ids=["expired", "revoked", "secret-key-stub", "empty"],
)
def test_unusable_keys_are_not_candidates(listing):
    assert parse_candidates(listing) == ()


def test_two_usable_keys_are_both_enumerated_and_the_expired_one_is_not():
    fingerprints = [c.fingerprint for c in parse_candidates(fx.TWO_USABLE)]

    assert fingerprints == [fx.PROTECTED_FPR, fx.UNPROTECTED_FPR]
    assert fx.EXPIRED_FPR not in fingerprints


def test_malformed_records_are_skipped_rather_than_raising():
    assert parse_candidates("sec:u:255\nnonsense\n:::\n") == ()


# --- KEYINFO parser --------------------------------------------------------


def test_keyinfo_distinguishes_protection_from_cache_state():
    grp = fx.PROTECTED_KEYGRIP
    assert parse_keyinfo(fx.PROTECTED_COLD, grp) == (False, "protected", None)
    assert parse_keyinfo(fx.PROTECTED_WARM, grp) == (True, "protected", None)
    assert parse_keyinfo(fx.UNPROTECTED_COLD, fx.UNPROTECTED_KEYGRIP) == (
        False,
        "unprotected",
        None,
    )


def test_keyinfo_reports_a_real_ttl_and_nothing_otherwise():
    grp = fx.PROTECTED_KEYGRIP
    warm_with_ttl = fx.keyinfo(grp, cached=True, protection="P", ttl="1800")

    assert parse_keyinfo(warm_with_ttl, grp) == (True, "protected", 1800)
    assert parse_keyinfo(fx.PROTECTED_WARM, grp)[2] is None


def test_keyinfo_ignores_a_line_for_a_different_keygrip():
    assert parse_keyinfo(fx.PROTECTED_COLD, fx.UNPROTECTED_KEYGRIP) is None
    assert parse_keyinfo(fx.KEYINFO_NOT_FOUND, fx.PROTECTED_KEYGRIP) is None


# --- probe states ----------------------------------------------------------


async def test_protected_warm_key_is_ready(monkeypatch, bin_dir, event_hub):
    _install(
        monkeypatch, bin_dir, keyinfo={fx.PROTECTED_KEYGRIP: fx.PROTECTED_WARM}
    )
    status = await GpgProbe(Config(), event_hub).probe()

    assert status.state == "ready"
    assert status.selected is not None
    assert status.selected.fingerprint == fx.PROTECTED_FPR
    assert status.selected.protection == "protected"
    assert status.selected.source == "auto"


async def test_protected_cold_key_is_locked(monkeypatch, bin_dir, event_hub):
    _install(
        monkeypatch, bin_dir, keyinfo={fx.PROTECTED_KEYGRIP: fx.PROTECTED_COLD}
    )
    status = await GpgProbe(Config(), event_hub).probe()

    assert status.state == "locked"
    assert status.selected is not None
    assert status.selected.fingerprint == fx.PROTECTED_FPR
    assert status.selected.protection == "protected"


async def test_passphrase_less_key_is_ready_not_locked(
    monkeypatch, bin_dir, event_hub
):
    """The bug this change exists to fix: nothing to cache is not a lock."""
    _install(
        monkeypatch,
        bin_dir,
        listing=fx.UNPROTECTED_ONLY,
        keyinfo={fx.UNPROTECTED_KEYGRIP: fx.UNPROTECTED_COLD},
    )
    status = await GpgProbe(Config(), event_hub).probe()

    assert status.state == "ready"
    assert status.selected is not None
    assert status.selected.protection == "unprotected"
    assert status.cache_ttl is None


async def test_no_signing_capable_key_is_no_key(monkeypatch, bin_dir, event_hub):
    _install(monkeypatch, bin_dir, listing=fx.EXPIRED_ONLY)
    status = await GpgProbe(Config(), event_hub).probe()

    assert status.state == "no_key"
    assert status.selected is None


async def test_several_usable_keys_are_ambiguous(monkeypatch, bin_dir, event_hub):
    _install(monkeypatch, bin_dir, listing=fx.TWO_USABLE)
    status = await GpgProbe(Config(), event_hub).probe()

    assert status.state == "ambiguous"
    assert status.selected is None
    # The operator still needs the choices in order to make one.
    assert [c.fingerprint for c in status.candidates] == [
        fx.PROTECTED_FPR,
        fx.UNPROTECTED_FPR,
    ]
    assert "2 usable signing keys" in (status.detail or "")


async def test_missing_gpg_is_missing(monkeypatch, bin_dir, event_hub):
    _install(monkeypatch, bin_dir, gpg_missing=True)
    status = await GpgProbe(Config(), event_hub).probe()

    assert status.state == "missing"
    assert "gpg" in (status.detail or "")


async def test_missing_gpg_connect_agent_is_missing(monkeypatch, bin_dir, event_hub):
    _install(monkeypatch, bin_dir, agent_missing=True)
    status = await GpgProbe(Config(), event_hub).probe()

    assert status.state == "missing"


async def test_unreachable_agent_is_agent_unavailable(
    monkeypatch, bin_dir, event_hub
):
    """gpg-connect-agent exits 0 here, so only its output distinguishes this."""
    _install(monkeypatch, bin_dir, keyinfo={}, agent_stderr=fx.AGENT_DOWN_STDERR)
    status = await GpgProbe(Config(), event_hub).probe()

    assert status.state == "agent_unavailable"
    assert status.selected is not None


async def test_agent_that_does_not_know_the_keygrip_is_error(
    monkeypatch, bin_dir, event_hub
):
    _install(
        monkeypatch,
        bin_dir,
        keyinfo={fx.PROTECTED_KEYGRIP: fx.KEYINFO_NOT_FOUND},
    )
    status = await GpgProbe(Config(), event_hub).probe()

    assert status.state == "error"
    assert "Not found" in (status.detail or "")


async def test_failed_listing_is_error(monkeypatch, bin_dir, event_hub):
    _install(monkeypatch, bin_dir, listing="", listing_exit=2)
    status = await GpgProbe(Config(), event_hub).probe()

    assert status.state == "error"


async def test_current_is_unknown_before_the_first_probe(event_hub):
    assert GpgProbe(Config(), event_hub).current().state == "unknown"


# --- selection precedence --------------------------------------------------


class _Store:
    """Minimal stand-in for the layered settings store."""

    def __init__(self, value: str | None) -> None:
        self._value = value

    def effective(self) -> dict[str, object]:
        return {"gpg_signing_key": self._value}


async def test_override_selects_a_specific_key(monkeypatch, bin_dir, event_hub):
    _install(
        monkeypatch,
        bin_dir,
        listing=fx.TWO_USABLE,
        keyinfo={fx.UNPROTECTED_KEYGRIP: fx.UNPROTECTED_COLD},
    )
    probe = GpgProbe(Config(), event_hub, _Store(fx.UNPROTECTED_FPR))
    status = await probe.probe()

    assert status.state == "ready"
    assert status.selected is not None
    assert status.selected.fingerprint == fx.UNPROTECTED_FPR
    assert status.selected.source == "override"


async def test_override_wins_over_config(monkeypatch, bin_dir, event_hub):
    _install(
        monkeypatch,
        bin_dir,
        listing=fx.TWO_USABLE,
        keyinfo={fx.PROTECTED_KEYGRIP: fx.PROTECTED_WARM},
    )
    config = Config(gpg_signing_key=fx.UNPROTECTED_FPR)
    status = await GpgProbe(config, event_hub, _Store(fx.PROTECTED_FPR)).probe()

    assert status.selected is not None
    assert status.selected.fingerprint == fx.PROTECTED_FPR
    assert status.selected.source == "override"


async def test_clearing_the_override_falls_back_to_config(
    monkeypatch, bin_dir, event_hub
):
    _install(
        monkeypatch,
        bin_dir,
        listing=fx.TWO_USABLE,
        keyinfo={fx.UNPROTECTED_KEYGRIP: fx.UNPROTECTED_COLD},
    )
    config = Config(gpg_signing_key=fx.UNPROTECTED_FPR)
    status = await GpgProbe(config, event_hub, _Store(None)).probe()

    assert status.selected is not None
    assert status.selected.fingerprint == fx.UNPROTECTED_FPR
    assert status.selected.source == "config"


async def test_vanished_selection_is_error_and_never_silently_reassigned(
    monkeypatch, bin_dir, event_hub
):
    _install(monkeypatch, bin_dir, listing=fx.PROTECTED_ONLY)
    probe = GpgProbe(Config(), event_hub, _Store(fx.UNPROTECTED_FPR))
    status = await probe.probe()

    assert status.state == "error"
    # Falling back to the one remaining key would sign as an identity the
    # operator did not choose.
    assert status.selected is None
    assert fx.UNPROTECTED_FPR in (status.detail or "")


@pytest.mark.parametrize(
    "identifier",
    [fx.UNPROTECTED_FPR, fx.UNPROTECTED_FPR.lower(), fx.UNPROTECTED_KEY_ID],
    ids=["fingerprint", "lowercase-fingerprint", "long-key-id"],
)
async def test_config_accepts_the_identifier_forms_gpg_accepts(
    monkeypatch, bin_dir, event_hub, identifier
):
    _install(
        monkeypatch,
        bin_dir,
        listing=fx.TWO_USABLE,
        keyinfo={fx.UNPROTECTED_KEYGRIP: fx.UNPROTECTED_COLD},
    )
    config = Config(gpg_signing_key=identifier)
    status = await GpgProbe(config, event_hub).probe()

    assert status.selected is not None
    assert status.selected.fingerprint == fx.UNPROTECTED_FPR


@pytest.mark.parametrize(
    "identifier",
    [fx.PROTECTED_PRIMARY_FPR, fx.PROTECTED_PRIMARY_KEY_ID],
    ids=["primary-fingerprint", "primary-key-id"],
)
async def test_config_naming_the_primary_selects_its_signing_subkey(
    monkeypatch, bin_dir, event_hub, identifier
):
    """`gpg -u <primary>` signs with the subkey; naming the primary is normal."""
    _install(
        monkeypatch,
        bin_dir,
        listing=fx.TWO_USABLE,
        keyinfo={fx.PROTECTED_KEYGRIP: fx.PROTECTED_WARM},
    )
    config = Config(gpg_signing_key=identifier)
    status = await GpgProbe(config, event_hub).probe()

    assert status.state == "ready"
    assert status.selected is not None
    assert status.selected.fingerprint == fx.PROTECTED_FPR


async def test_config_naming_an_absent_key_is_error(monkeypatch, bin_dir, event_hub):
    _install(monkeypatch, bin_dir, listing=fx.PROTECTED_ONLY)
    config = Config(gpg_signing_key="DEADBEEF" * 5)
    status = await GpgProbe(config, event_hub).probe()

    assert status.state == "error"
    assert "config.toml" in (status.detail or "")


async def test_git_signingkey_is_used_when_config_says_nothing(
    monkeypatch, bin_dir, event_hub
):
    _install(
        monkeypatch,
        bin_dir,
        listing=fx.TWO_USABLE,
        keyinfo={fx.UNPROTECTED_KEYGRIP: fx.UNPROTECTED_COLD},
        git_signing_key=fx.UNPROTECTED_FPR,
    )
    status = await GpgProbe(Config(), event_hub).probe()

    assert status.selected is not None
    assert status.selected.fingerprint == fx.UNPROTECTED_FPR
    assert status.selected.source == "git"


# --- publication -----------------------------------------------------------


async def test_probe_publishes_the_status(monkeypatch, bin_dir, event_hub):
    _install(
        monkeypatch, bin_dir, keyinfo={fx.PROTECTED_KEYGRIP: fx.PROTECTED_COLD}
    )
    queue = event_hub.subscribe()
    await GpgProbe(Config(), event_hub).probe()
    event = queue.get_nowait()
    event_hub.unsubscribe(queue)

    assert event.type == "gpg_status"
    assert event.payload["status"]["state"] == "locked"


async def test_published_payload_carries_no_key_material(
    monkeypatch, bin_dir, event_hub
):
    _install(
        monkeypatch, bin_dir, keyinfo={fx.PROTECTED_KEYGRIP: fx.PROTECTED_WARM}
    )
    queue = event_hub.subscribe()
    await GpgProbe(Config(), event_hub).probe()
    payload = queue.get_nowait().payload["status"]
    event_hub.unsubscribe(queue)

    assert set(payload) == {
        "state",
        "selected",
        "candidates",
        "cache_ttl",
        "detail",
        "checked_at",
    }
    assert set(payload["selected"]) == {
        "fingerprint",
        "key_id",
        "uid",
        "keygrip",
        "source",
        "protection",
    }
    assert set(payload["candidates"][0]) == {
        "fingerprint",
        "key_id",
        "uid",
        "keygrip",
        "created_at",
        "expires_at",
        "primary_fingerprint",
    }


def test_every_blocking_state_explains_itself():
    from ompire_daemon.gpg import GpgStatus

    for state in (
        "locked",
        "ambiguous",
        "no_key",
        "missing",
        "agent_unavailable",
        "unknown",
        "error",
    ):
        message = gpg_signing_refusal(GpgStatus(state=state))
        assert message and "not cached" not in message


# --- against real GnuPG ----------------------------------------------------

real_gpg = pytest.mark.skipif(
    shutil.which("gpg") is None or shutil.which("gpg-connect-agent") is None,
    reason="requires real gpg and gpg-connect-agent",
)


def _gnupg_home(tmp_path: Path) -> Path:
    home = tmp_path / "gnupg"
    home.mkdir(mode=0o700)
    (home / "gpg-agent.conf").write_text("allow-loopback-pinentry\n")
    return home


def _generate(home: Path, uid: str, passphrase: str) -> str:
    """Create a signing key and return its fingerprint."""
    env = {**os.environ, "GNUPGHOME": str(home)}
    subprocess.run(
        [
            "gpg", "--batch", "--quiet", "--pinentry-mode", "loopback",
            "--passphrase", passphrase,
            "--quick-generate-key", uid, "ed25519", "sign", "never",
        ],
        env=env, check=True, capture_output=True,
    )
    listing = subprocess.run(
        ["gpg", "--list-secret-keys", "--with-colons", uid],
        env=env, check=True, capture_output=True, text=True,
    ).stdout
    for line in listing.splitlines():
        if line.startswith("fpr:"):
            return line.split(":")[9]
    raise AssertionError(f"no fingerprint for {uid}")


def _stop_agent(home: Path) -> None:
    subprocess.run(
        ["gpgconf", "--homedir", str(home), "--kill", "gpg-agent"],
        capture_output=True,
        check=False,
    )


@real_gpg
async def test_real_unprotected_key_is_ready(tmp_path, monkeypatch, event_hub):
    home = _gnupg_home(tmp_path)
    fingerprint = _generate(home, "Real Unprotected <u@example.com>", "")
    monkeypatch.setenv("GNUPGHOME", str(home))
    try:
        status = await GpgProbe(Config(), event_hub).probe()

        assert status.state == "ready"
        assert status.selected is not None
        assert status.selected.fingerprint == fingerprint
        assert status.selected.protection == "unprotected"
    finally:
        _stop_agent(home)


@real_gpg
async def test_real_protected_key_is_locked_when_cold_and_ready_when_warm(
    tmp_path, monkeypatch, event_hub
):
    home = _gnupg_home(tmp_path)
    fingerprint = _generate(home, "Real Protected <p@example.com>", "hunter2")
    monkeypatch.setenv("GNUPGHOME", str(home))
    try:
        _stop_agent(home)
        cold = await GpgProbe(Config(), event_hub).probe()
        assert cold.state == "locked"
        assert cold.selected is not None
        assert cold.selected.protection == "protected"

        subprocess.run(
            [
                "gpg", "--batch", "--quiet", "--pinentry-mode", "loopback",
                "--passphrase", "hunter2", "--clearsign", "-u", fingerprint,
            ],
            env={**os.environ, "GNUPGHOME": str(home)},
            input=b"warm", check=True, capture_output=True,
        )

        warm = await GpgProbe(Config(), event_hub).probe()
        assert warm.state == "ready"
        assert warm.selected is not None
        assert warm.selected.fingerprint == fingerprint
    finally:
        _stop_agent(home)


@real_gpg
async def test_real_keyring_with_two_keys_is_ambiguous_until_one_is_selected(
    tmp_path, monkeypatch, event_hub
):
    home = _gnupg_home(tmp_path)
    first = _generate(home, "Real One <one@example.com>", "")
    second = _generate(home, "Real Two <two@example.com>", "")
    monkeypatch.setenv("GNUPGHOME", str(home))
    try:
        ambiguous = await GpgProbe(Config(), event_hub).probe()
        assert ambiguous.state == "ambiguous"
        assert {c.fingerprint for c in ambiguous.candidates} == {first, second}

        chosen = await GpgProbe(Config(), event_hub, _Store(second)).probe()
        assert chosen.state == "ready"
        assert chosen.selected is not None
        assert chosen.selected.fingerprint == second
    finally:
        _stop_agent(home)
