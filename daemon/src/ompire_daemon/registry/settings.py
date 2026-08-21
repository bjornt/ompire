"""Layered daemon settings store (daemon-settings capability, design D-1).

Effective value resolution:

    DB override (settings row)  →  config.toml value  →  built-in default

The daemon never writes to config.toml; the UI persists overrides in the
registry `settings` table as JSON-encoded scalar values.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import CursorResult, Engine, text
from sqlalchemy.orm import Session

from ompire_daemon.config import Config
from ompire_daemon.db import settings as settings_table


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
# Design D-1 tier defaults match Settings.dc.html: desktop on for
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
}

# Keys that may be seeded from config.toml and the Config attribute that
# supplies the value. Tier preferences are default-only.
_CONFIG_KEYS: dict[str, str] = {
    "renotify_interval": "renotify_interval",
    "stall_threshold": "stall_threshold",
    "context_advisory_threshold": "context_advisory_threshold",
}


def _validate(key: str, value: Any) -> Any:
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
        if value < 30:
            raise SettingsValidationError(
                key, f"{key} must be at least 30 seconds, got {value!r}"
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

    # Unreachable because of the key check above, but keep exhaustive.
    raise SettingsValidationError(key, f"{key} has no validator")


def _default_for(key: str) -> Any:
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
    return _default_for(key), "default"


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
            validated[key] = _validate(key, value)
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


def get_settings(engine: Engine, config: Config) -> Settings:
    """Convenience shortcut for one-off reads."""
    return SettingsStore(engine, config).get()
