"""Daemon configuration: loaded from ~/.config/ompire/config.toml with defaults."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = Path("~/.config/ompire/config.toml").expanduser()

DEFAULT_PORT = 4173
DEFAULT_BIND = "127.0.0.1"
DEFAULT_DATA_DIR = Path("~/.local/share/ompire").expanduser()
DEFAULT_TASK_DIR_ROOT = Path("~/tasks").expanduser()
DEFAULT_CHECKOUT_ROOT = Path("~/proj").expanduser()
DEFAULT_BRANCH_PATTERN = "ompire/<slug>"
DEFAULT_SPAWN_STEP_TIMEOUT = 120
DEFAULT_MY_WORKSHOP_COMMAND = ("my-workshop",)
# Workshop launch includes SDK installs; the spike measured 7.34s warm-cache
# and cold starts are slower still, so this is deliberately much larger than
# the git-step timeout.
DEFAULT_WORKSHOP_STEP_TIMEOUT = 600

_KNOWN_KEYS = {
    "port",
    "bind",
    "data_dir",
    "task_dir_root",
    "checkout_root",
    "default_branch_pattern",
    "spawn_step_timeout",
    "my_workshop_command",
    "workshop_step_timeout",
}


class ConfigError(Exception):
    """Raised when the config file is malformed or contains unknown/invalid keys."""


@dataclass(frozen=True)
class Config:
    port: int = DEFAULT_PORT
    bind: str = DEFAULT_BIND
    data_dir: Path = DEFAULT_DATA_DIR
    task_dir_root: Path = DEFAULT_TASK_DIR_ROOT
    checkout_root: Path = DEFAULT_CHECKOUT_ROOT
    default_branch_pattern: str = DEFAULT_BRANCH_PATTERN
    spawn_step_timeout: int = DEFAULT_SPAWN_STEP_TIMEOUT
    my_workshop_command: tuple[str, ...] = DEFAULT_MY_WORKSHOP_COMMAND
    workshop_step_timeout: int = DEFAULT_WORKSHOP_STEP_TIMEOUT


def load_config(path: Path | None = None) -> Config:
    """Load config from `path` (default `~/.config/ompire/config.toml`).

    Runs with all defaults when the file is absent. Raises `ConfigError` on
    invalid TOML or unknown/invalid keys, naming the offending key.
    """
    config_path = path if path is not None else DEFAULT_CONFIG_PATH

    if not config_path.exists():
        return Config()

    try:
        raw_bytes = config_path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"cannot read config file {config_path}: {exc}") from exc

    try:
        data = tomllib.loads(raw_bytes.decode("utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"malformed TOML in {config_path}: {exc}") from exc

    unknown_keys = set(data) - _KNOWN_KEYS
    if unknown_keys:
        raise ConfigError(
            f"unknown config key(s) in {config_path}: {', '.join(sorted(unknown_keys))}"
        )

    port = data.get("port", DEFAULT_PORT)
    if not isinstance(port, int) or isinstance(port, bool):
        raise ConfigError(f"config key 'port' must be an integer, got {port!r}")

    bind = data.get("bind", DEFAULT_BIND)
    if not isinstance(bind, str):
        raise ConfigError(f"config key 'bind' must be a string, got {bind!r}")

    data_dir = _path_value(data, "data_dir", DEFAULT_DATA_DIR)
    task_dir_root = _path_value(data, "task_dir_root", DEFAULT_TASK_DIR_ROOT)
    checkout_root = _path_value(data, "checkout_root", DEFAULT_CHECKOUT_ROOT)

    default_branch_pattern = data.get("default_branch_pattern", DEFAULT_BRANCH_PATTERN)
    if not isinstance(default_branch_pattern, str):
        raise ConfigError(
            f"config key 'default_branch_pattern' must be a string, got {default_branch_pattern!r}"
        )

    spawn_step_timeout = data.get("spawn_step_timeout", DEFAULT_SPAWN_STEP_TIMEOUT)
    if not isinstance(spawn_step_timeout, int) or isinstance(spawn_step_timeout, bool):
        raise ConfigError(
            f"config key 'spawn_step_timeout' must be an integer, got {spawn_step_timeout!r}"
        )

    my_workshop_command = data.get("my_workshop_command")
    if my_workshop_command is None:
        my_workshop_command = DEFAULT_MY_WORKSHOP_COMMAND
    elif (
        not isinstance(my_workshop_command, list)
        or not my_workshop_command
        or not all(isinstance(part, str) for part in my_workshop_command)
    ):
        raise ConfigError(
            f"config key 'my_workshop_command' must be a non-empty list of strings, "
            f"got {my_workshop_command!r}"
        )
    else:
        my_workshop_command = tuple(my_workshop_command)

    workshop_step_timeout = data.get("workshop_step_timeout", DEFAULT_WORKSHOP_STEP_TIMEOUT)
    if not isinstance(workshop_step_timeout, int) or isinstance(workshop_step_timeout, bool):
        raise ConfigError(
            f"config key 'workshop_step_timeout' must be an integer, got {workshop_step_timeout!r}"
        )

    return Config(
        port=port,
        bind=bind,
        data_dir=data_dir,
        task_dir_root=task_dir_root,
        checkout_root=checkout_root,
        default_branch_pattern=default_branch_pattern,
        spawn_step_timeout=spawn_step_timeout,
        my_workshop_command=my_workshop_command,
        workshop_step_timeout=workshop_step_timeout,
    )


def _path_value(data: dict, key: str, default: Path) -> Path:
    value = data.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ConfigError(f"config key '{key}' must be a string, got {value!r}")
    return Path(value).expanduser()
