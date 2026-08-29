"""Layered daemon settings store. Architectural boundary: ADR-0013.

Effective value resolution:

    DB override (settings row)  →  config.toml value  →  built-in default

The daemon never writes to config.toml; the UI persists overrides in the
registry `settings` table as JSON-encoded scalar values.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from sqlalchemy import CursorResult, Engine, text
from sqlalchemy.orm import Session

from ompire_daemon.config import DEFAULT_CHECKOUT_ROOT, Config
from ompire_daemon.db import settings as settings_table
from ompire_daemon.gpg import FINGERPRINT_RE


class SettingsValidationError(Exception):
    """Raised when a settings update contains an unknown key or invalid value.

    `key` names the offending key so the REST layer can build a 422.
    """

    def __init__(self, key: str, message: str) -> None:
        super().__init__(message)
        self.key = key
        self.message = message


# Recognised settings keys and their default values.
#
# Tier defaults preserve the original preference matrix: desktop on for
# interrupt/notify, sound on for interrupt only, badge on for
# interrupt/notify/badge, everything else off.
_DEFAULTS: dict[str, Any] = {
    "tier.interrupt.desktop": True,
    "tier.interrupt.sound": True,
    "tier.interrupt.badge": True,
    "tier.notify.desktop": True,
    "tier.notify.sound": False,
    "tier.notify.badge": True,
    "tier.badge.desktop": False,
    "tier.badge.sound": False,
    "tier.badge.badge": True,
    "tier.silent.desktop": False,
    "tier.silent.sound": False,
    "tier.silent.badge": False,
    "renotify_interval": 300,
    "stall_threshold": 300,
    "context_advisory_threshold": 80,
    # ADR-0021: the one setting that selects an identity. Its override is
    # bounded to a full fingerprint here and to a key the host keyring already
    # holds at the REST boundary, so it can never name a key off this host.
    "gpg_signing_key": None,
    # ADR-0023: the parent directory clone-mode project setup creates
    # checkouts under. Unlike the signing key this always has a value, so the
    # effective map never makes a client guess what "unset" resolves to. The
    # default layer is `Config.checkout_root`, which already *is* "config.toml
    # value or built-in default" — see `_default_value`.
    "checkout_root": str(DEFAULT_CHECKOUT_ROOT),
}

# Keys that may be seeded from config.toml and the Config attribute that
# supplies the value. Tier preferences are default-only.
_CONFIG_KEYS: dict[str, str] = {
    "renotify_interval": "renotify_interval",
    "stall_threshold": "stall_threshold",
    "context_advisory_threshold": "context_advisory_threshold",
    "gpg_signing_key": "gpg_signing_key",
    "checkout_root": "checkout_root",
}



def _validate(key: str, value: Any, config: Config) -> Any:
    """Return a normalized valid value or raise `SettingsValidationError`."""
    if key not in _DEFAULTS:
        raise SettingsValidationError(key, f"unknown setting: {key}")

    if key.startswith("tier."):
        if not isinstance(value, bool):
            raise SettingsValidationError(
                key, f"{key} must be a boolean, got {value!r}"
            )
        return value

    if key == "renotify_interval":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SettingsValidationError(
                key, f"{key} must be a number, got {value!r}"
            )
            
        if value != 0 and value < 30:
            raise SettingsValidationError(
                key, f"{key} must be 0 or at least 30 seconds, got {value!r}"
            )
        return int(value)

    if key == "stall_threshold":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SettingsValidationError(
                key, f"{key} must be a number, got {value!r}"
            )
        if value <= 0:
            raise SettingsValidationError(
                key, f"{key} must be positive, got {value!r}"
            )

        return int(value)

    if key == "context_advisory_threshold":
        if isinstance(value, bool) or not isinstance(value, int):
            raise SettingsValidationError(
                key, f"{key} must be an integer, got {value!r}"
            )
        if not 1 <= value <= 100:
            raise SettingsValidationError(
                key, f"{key} must be between 1 and 100, got {value!r}"
            )
        return value

    if key == "checkout_root":
        return _validate_checkout_root(value, config.task_dir_root)

    if key == "gpg_signing_key":
        # Only a full fingerprint identifies exactly one key. Key IDs and
        # user-ID substrings stay a config.toml convenience; a stored
        # selection must be unambiguous (ADR-0021).
        if not isinstance(value, str) or not FINGERPRINT_RE.match(value):
            raise SettingsValidationError(
                key,
                f"{key} must be a 40-character OpenPGP fingerprint, got {value!r}",
            )
        return value.upper()

    # Unreachable because of the key check above, but keep exhaustive.
    raise SettingsValidationError(key, f"{key} has no validator")


def _validate_checkout_root(value: Any, task_dir_root: Path) -> str:
    """Bound the one filesystem path the UI may set (ADR-0023).

    Absolute after `~` expansion, no traversal, and disjoint from the daemon's
    task root — a base checkout that lived inside the task root would sit
    where task cleanup deletes.
    """
    key = "checkout_root"
    if not isinstance(value, str) or not value.strip():
        raise SettingsValidationError(key, f"{key} must be a non-empty path, got {value!r}")
    raw = value.strip()
    if ".." in Path(raw).parts:
        raise SettingsValidationError(key, f"{key} must not contain '..', got {raw!r}")
    expanded = Path(raw).expanduser()
    if not expanded.is_absolute():
        raise SettingsValidationError(
            key, f"{key} must be an absolute path, got {raw!r}"
        )
    resolved = Path(os.path.normpath(str(expanded)))
    task_root = Path(os.path.normpath(str(Path(task_dir_root).expanduser())))
    if resolved == task_root or task_root in resolved.parents:
        raise SettingsValidationError(
            key,
            f"{key} must not be inside the task root {task_root} — task "
            "cleanup deletes there",
        )
    if resolved == Path(resolved.anchor):
        raise SettingsValidationError(
            key, f"{key} must not be the filesystem root, got {raw!r}"
        )
    return str(resolved)


def _default_for(key: str) -> Any:
    return _DEFAULTS[key]


def _default_value(config: Config, key: str) -> Any:
    """The bottom layer for `key`.

    `checkout_root` is the one key whose built-in default is already resolved
    on `Config`: startup turns the operator's value or the product default
    into one path, and both the daemon and this store must agree on it.
    """
    if key == "checkout_root":
        return str(config.checkout_root)
    return _DEFAULTS[key]


def _config_value(config: Config, key: str) -> Any | None:
    """Return the config.toml value for `key` if that key exists in the
    operator's config file, otherwise `None`."""
    attr = _CONFIG_KEYS.get(key)
    if attr is None:
        return None
    source = getattr(config, "config_source", {})
    return source.get(key)


def _resolve_key(config: Config, key: str, override: Any | None) -> tuple[Any, str]:
    if override is not None:
        return override, "override"
    config_value = _config_value(config, key)
    if config_value is not None:
        return config_value, "config"
    return _default_value(config, key), "default"


def _json_load(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        # Rows are written only by this module; malformed data is a bug.
        raise RuntimeError(f"malformed settings value: {value!r}") from exc


@dataclass(frozen=True)
class Settings:
    """Effective settings plus per-key provenance."""

    settings: dict[str, Any]
    provenance: dict[str, str]


class SettingsStore:
    """Read and write layered daemon settings."""

    def __init__(self, engine: Engine, config: Config) -> None:
        self._engine = engine
        self._config = config

    def _load_overrides(self) -> dict[str, Any]:
        with Session(self._engine) as session:
            rows = session.execute(settings_table.select()).all()
        return {row.key: _json_load(row.value) for row in rows}

    def get(self) -> Settings:
        """Return effective settings and provenance for every known key."""
        overrides = self._load_overrides()
        settings: dict[str, Any] = {}
        provenance: dict[str, str] = {}
        for key in _DEFAULTS:
            value, layer = _resolve_key(self._config, key, overrides.get(key))
            settings[key] = value
            provenance[key] = layer
        return Settings(settings, provenance)

    def effective(self) -> dict[str, Any]:
        """Return the effective settings map only."""
        return self.get().settings

    def validate(self, changes: Mapping[str, Any]) -> dict[str, Any]:
        """Validate a partial update without writing anything.

        Raises `SettingsValidationError` on the first offending key.
        """
        validated: dict[str, Any] = {}
        for key, value in changes.items():
            validated[key] = _validate(key, value, self._config)
        return validated

    def update(self, changes: Mapping[str, Any]) -> Settings:
        """Validate and atomically persist a partial update.

        All keys are validated before any row is written.
        """
        validated = self.validate(changes)
        with Session(self._engine) as session:
            for key, value in validated.items():
                session.execute(
                    text("INSERT OR REPLACE INTO settings (key, value) VALUES (:key, :value)"),
                    {"key": key, "value": json.dumps(value)},
                )
            session.commit()
        return self.get()

    def delete(self, key: str) -> bool:
        """Delete a DB override. Returns True if a row existed, False if the
        key is unknown or had no override (caller should 404)."""
        if key not in _DEFAULTS:
            return False
        with Session(self._engine) as session:
            result = cast(
                CursorResult[Any],
                session.execute(
                    settings_table.delete().where(settings_table.c.key == key)
                ),
            )
            session.commit()
            return result.rowcount > 0


def effective_checkout_root(settings: Mapping[str, Any]) -> Path:
    """The parent directory a clone-mode project's checkout is created under.

    Expanded here rather than at write time so a `~`-relative value from
    `config.toml` keeps meaning "this operator's home".
    """
    value = settings.get("checkout_root")
    if not isinstance(value, str) or not value.strip():
        return DEFAULT_CHECKOUT_ROOT
    return Path(value.strip()).expanduser()


def get_settings(engine: Engine, config: Config) -> Settings:
    """Convenience shortcut for one-off reads."""
    return SettingsStore(engine, config).get()
