"""Tests for `ompire_daemon.gpg`."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from ompire_daemon.config import Config
from ompire_daemon.events import EventHub
from ompire_daemon.gpg import GpgProbe


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


@pytest.fixture
def config() -> Config:
    return Config()


@pytest.fixture
def fake_gpg_keygrip_output() -> str:
    return """
/tmp/test/pubring.kbx
-------------------------
sec   rsa2048 2026-07-22 [SCEAR]
      B4C4207720270E2FB99002559F1C030DE2985A55
      Keygrip = CF3C56F940AB4655356F13A2A375FD304CD88A71
uid           [ultimate] Test User <test@example.com>
ssb   rsa2048 2026-07-22 [SEA]
      Keygrip = 8C9301DF2FFD432192448A04C8F2A6BA372A1830
"""


async def _setup_bin(
    monkeypatch,
    bin_dir: Path,
    keygrip_output: str,
    keyinfo_lines: str,
    git_signing_key: str | None = None,
    gpg_exit: int = 0,
    gpg_stderr: str = "",
) -> None:
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    _write_script(
        bin_dir,
        "gpg-connect-agent",
        f"#!/bin/sh\necho '{keyinfo_lines}'\necho 'OK'\n",
    )
    if gpg_exit == 0:
        _write_script(
            bin_dir,
            "gpg",
            f"#!/bin/sh\ncat <<'EOF'\n{keygrip_output}\nEOF\n",
        )
    else:
        _write_script(
            bin_dir,
            "gpg",
            f"#!/bin/sh\necho {gpg_stderr!r} >&2\nexit {gpg_exit}\n",
        )

    if git_signing_key is not None:
        _write_script(
            bin_dir,
            "git",
            "#!/bin/sh\n"
            "if [ \"$1\" = 'config' ] && [ \"$2\" = '--get' ] && [ \"$3\" = 'user.signingkey' ]; then\n"
            f"    echo '{git_signing_key}'\n"
            "    exit 0\n"
            "fi\n"
            "exit 1\n",
        )


async def test_probe_cached(
    monkeypatch, bin_dir, config, event_hub, fake_gpg_keygrip_output
):
    await _setup_bin(
        monkeypatch,
        bin_dir,
        fake_gpg_keygrip_output,
        "S KEYINFO 8C9301DF2FFD432192448A04C8F2A6BA372A1830 D - - 1 C - - -",
    )
    probe = GpgProbe(Config(gpg_signing_key="test@example.com"), event_hub)
    assert probe.current().state == "unknown"

    result = await probe.probe()
    assert result.state == "cached"
    assert result.key == "test@example.com"
    assert result.keygrip == "8C9301DF2FFD432192448A04C8F2A6BA372A1830"


async def test_probe_locked(
    monkeypatch, bin_dir, config, event_hub, fake_gpg_keygrip_output
):
    await _setup_bin(
        monkeypatch,
        bin_dir,
        fake_gpg_keygrip_output,
        "S KEYINFO 8C9301DF2FFD432192448A04C8F2A6BA372A1830 D - - - C - - -",
    )
    probe = GpgProbe(Config(gpg_signing_key="test@example.com"), event_hub)

    result = await probe.probe()
    assert result.state == "locked"
    assert result.keygrip == "8C9301DF2FFD432192448A04C8F2A6BA372A1830"


async def test_probe_unknown_when_no_key_configured(
    monkeypatch, bin_dir, event_hub
):
    # Place a fake git that reports no user.signingkey.
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    _write_script(
        bin_dir,
        "git",
        "#!/bin/sh\nif [ \"$1\" = 'config' ] && [ \"$2\" = '--get' ] && [ \"$3\" = 'user.signingkey' ]; then exit 1; fi\nexit 1\n",
    )

    probe = GpgProbe(Config(), event_hub)
    result = await probe.probe()
    assert result.state == "unknown"
    assert result.key is None
    assert "no signing key configured" in (result.detail or "")


async def test_probe_falls_back_to_git_user_signingkey(
    monkeypatch, bin_dir, event_hub, fake_gpg_keygrip_output
):
    await _setup_bin(
        monkeypatch,
        bin_dir,
        fake_gpg_keygrip_output,
        "S KEYINFO 8C9301DF2FFD432192448A04C8F2A6BA372A1830 D - - 1 C - - -",
        git_signing_key="fallback-key",
    )

    probe = GpgProbe(Config(), event_hub)
    result = await probe.probe()
    assert result.state == "cached"
    assert result.key == "fallback-key"


async def test_probe_unknown_when_gpg_fails(monkeypatch, bin_dir, event_hub):
    await _setup_bin(
        monkeypatch,
        bin_dir,
        "",
        "",
        gpg_exit=2,
        gpg_stderr="gpg: no secret key",
    )

    probe = GpgProbe(Config(gpg_signing_key="missing@example.com"), event_hub)
    result = await probe.probe()
    assert result.state == "unknown"
    assert result.key == "missing@example.com"
    assert result.keygrip is None


async def test_probe_publishes_event(
    monkeypatch, bin_dir, event_hub, fake_gpg_keygrip_output
):
    await _setup_bin(
        monkeypatch,
        bin_dir,
        fake_gpg_keygrip_output,
        "S KEYINFO 8C9301DF2FFD432192448A04C8F2A6BA372A1830 D - - - C - - -",
    )

    queue = event_hub.subscribe()
    probe = GpgProbe(Config(gpg_signing_key="test@example.com"), event_hub)
    await probe.probe()
    event = queue.get_nowait()
    event_hub.unsubscribe(queue)

    assert event.type == "gpg_status"
    assert event.payload["status"]["state"] == "locked"
