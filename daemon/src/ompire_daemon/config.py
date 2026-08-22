"""Daemon configuration: loaded from ~/.config/ompire/config.toml with defaults."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path("~/.config/ompire/config.toml").expanduser()

DEFAULT_PORT = 4173
# Local single-user boundary: ADR-0002
# (docs/adr/0002-run-as-local-daemon-with-stateless-web-ui.md)
DEFAULT_BIND = "127.0.0.1"


def _default_data_dir() -> Path:
    """Use per-user snap data, otherwise the XDG data directory."""
    snap_user_data = os.environ.get("SNAP_USER_DATA")
    if snap_user_data:
        return Path(snap_user_data)

    data_home = Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")).expanduser()
    return data_home / "ompire"


DEFAULT_DATA_DIR = _default_data_dir()
DEFAULT_TASK_DIR_ROOT = Path("~/tasks").expanduser()
DEFAULT_CHECKOUT_ROOT = Path("~/proj").expanduser()
DEFAULT_BRANCH_PATTERN = "ompire/<slug>"
DEFAULT_SPAWN_STEP_TIMEOUT = 120
DEFAULT_MY_WORKSHOP_COMMAND = ("my-workshop",)
DEFAULT_LLMVET_COMMAND = ("llmvet",)
# Port range for the daemon-run llmvet review server (design D-4). The
# daemon probes this range with an ephemeral bind to avoid collisions
# between concurrent reviews.
DEFAULT_REVIEW_PORT_RANGE = (7180, 7280)
DEFAULT_GPG_SIGNING_KEY: str | None = None
DEFAULT_GH_COMMAND = ("gh",)
# Interval between PR-state poll ticks (merge-poll capability, design D-2):
# serial `gh pr view` calls for shipped, not-yet-terminal PRs.
DEFAULT_PR_POLL_INTERVAL = 60
# Workshop launch includes SDK installs; the spike measured 7.34s warm-cache
# and cold starts are slower still, so this is deliberately much larger than
# the git-step timeout.
DEFAULT_WORKSHOP_STEP_TIMEOUT = 600
# Ready handshake covers container-side omp startup; the spike saw ~1.6s.
DEFAULT_AGENT_READY_TIMEOUT = 30
DEFAULT_AGENT_RING_BUFFER_SIZE = 1000
# Turn-boundary debounce before a session goes idle (SPEC D4): chained
# agent_end → agent_start hops must not flicker through idle.
DEFAULT_SESSION_IDLE_DEBOUNCE = 2.0
# Silence past which a `working` session is considered `stalled` (design D-4).
DEFAULT_STALL_THRESHOLD = 300
# Re-notify interval for an unanswered notify/interrupt-tier attention entry
# (design D-3).
DEFAULT_RENOTIFY_INTERVAL = 300
# Context-percent crossing that fires a `context-high` advisory (design D-5).
DEFAULT_CONTEXT_ADVISORY_THRESHOLD = 80
# Minimum spacing between `stats` events for the same task (design D-5).
DEFAULT_STATS_THROTTLE_INTERVAL = 10
# Desktop notifications on/off switch (design: graceful degradation/opt-out).
DEFAULT_NOTIFICATIONS_ENABLED = True
# SIGTERM-to-SIGKILL grace period for agent children on daemon shutdown
# (crash-recovery capability, design D-6): long enough for omp's teardown
# handlers to flush the session file.
DEFAULT_SHUTDOWN_GRACE = 10.0
# Startup-recovery fan-out bound (crash-recovery capability, design D-4):
# deliberately small — each resume is a real container-side omp startup.
DEFAULT_RECOVERY_CONCURRENCY = 4
# Model for the workflow engine's LLM-judge session (bugfix-workflow change,
# design D-4): None = omp's configured default model. Model naming is
# deployment-specific, so the daemon hardcodes nothing.
DEFAULT_JUDGE_MODEL: str | None = None

_KNOWN_KEYS = {
    "port",
    "bind",
    "data_dir",
    "task_dir_root",
    "checkout_root",
    "default_branch_pattern",
    "spawn_step_timeout",
    "my_workshop_command",
    "llmvet_command",
    "review_port_range",
    "workshop_step_timeout",
    "agent_env",
    "agent_ready_timeout",
    "agent_ring_buffer_size",
    "session_idle_debounce",
    "stall_threshold",
    "renotify_interval",
    "context_advisory_threshold",
    "stats_throttle_interval",
    "notifications_enabled",
    "shutdown_grace",
    "recovery_concurrency",
    "gpg_signing_key",
    "gh_command",
    "pr_poll_interval",
    "judge_model",
}


class ConfigError(Exception):
    """Raised when the config file is malformed or contains unknown/invalid keys."""


@dataclass(frozen=True)
class Config:
    # Source values from the operator's config.toml for keys the daemon
    # exposes through the layered settings store. Empty when the daemon is
    # constructed from defaults/tests; populated by load_config when a file
    # exists. Not compared so Config() == Config() regardless of source.
    config_source: dict[str, Any] = field(default_factory=dict, compare=False)

    port: int = DEFAULT_PORT
    bind: str = DEFAULT_BIND
    data_dir: Path = DEFAULT_DATA_DIR
    task_dir_root: Path = DEFAULT_TASK_DIR_ROOT
    checkout_root: Path = DEFAULT_CHECKOUT_ROOT
    default_branch_pattern: str = DEFAULT_BRANCH_PATTERN
    spawn_step_timeout: int = DEFAULT_SPAWN_STEP_TIMEOUT
    my_workshop_command: tuple[str, ...] = DEFAULT_MY_WORKSHOP_COMMAND
    llmvet_command: tuple[str, ...] = DEFAULT_LLMVET_COMMAND
    review_port_range: tuple[int, int] = DEFAULT_REVIEW_PORT_RANGE
    workshop_step_timeout: int = DEFAULT_WORKSHOP_STEP_TIMEOUT
    # Injected verbatim into agent children (design D-3): the daemon does not
    # know what a credential is, it forwards what the operator configured.
    agent_env: dict[str, str] = field(default_factory=dict)
    agent_ready_timeout: int = DEFAULT_AGENT_READY_TIMEOUT
    agent_ring_buffer_size: int = DEFAULT_AGENT_RING_BUFFER_SIZE
    session_idle_debounce: float = DEFAULT_SESSION_IDLE_DEBOUNCE
    stall_threshold: float = DEFAULT_STALL_THRESHOLD
    renotify_interval: float = DEFAULT_RENOTIFY_INTERVAL
    context_advisory_threshold: int = DEFAULT_CONTEXT_ADVISORY_THRESHOLD
    stats_throttle_interval: float = DEFAULT_STATS_THROTTLE_INTERVAL
    notifications_enabled: bool = DEFAULT_NOTIFICATIONS_ENABLED
    shutdown_grace: float = DEFAULT_SHUTDOWN_GRACE
    recovery_concurrency: int = DEFAULT_RECOVERY_CONCURRENCY
    gpg_signing_key: str | None = DEFAULT_GPG_SIGNING_KEY
    gh_command: tuple[str, ...] = DEFAULT_GH_COMMAND
    pr_poll_interval: float = DEFAULT_PR_POLL_INTERVAL
    judge_model: str | None = DEFAULT_JUDGE_MODEL


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

    llmvet_command = data.get("llmvet_command")
    if llmvet_command is None:
        llmvet_command = DEFAULT_LLMVET_COMMAND
    elif (
        not isinstance(llmvet_command, list)
        or not llmvet_command
        or not all(isinstance(part, str) for part in llmvet_command)
    ):
        raise ConfigError(
            f"config key 'llmvet_command' must be a non-empty list of strings, "
            f"got {llmvet_command!r}"
        )
    else:
        llmvet_command = tuple(llmvet_command)

    gpg_signing_key = data.get("gpg_signing_key", DEFAULT_GPG_SIGNING_KEY)
    if gpg_signing_key is not None and not isinstance(gpg_signing_key, str):
        raise ConfigError(
            f"config key 'gpg_signing_key' must be a string or unset, got {gpg_signing_key!r}"
        )

    judge_model = data.get("judge_model", DEFAULT_JUDGE_MODEL)
    if judge_model is not None and not isinstance(judge_model, str):
        raise ConfigError(
            f"config key 'judge_model' must be a string or unset, got {judge_model!r}"
        )

    gh_command = data.get("gh_command")
    if gh_command is None:
        gh_command = DEFAULT_GH_COMMAND
    elif (
        not isinstance(gh_command, list)
        or not gh_command
        or not all(isinstance(part, str) for part in gh_command)
    ):
        raise ConfigError(
            f"config key 'gh_command' must be a non-empty list of strings, "
            f"got {gh_command!r}"
        )
    else:
        gh_command = tuple(gh_command)

    review_port_range = data.get("review_port_range")
    if review_port_range is None:
        review_port_range = DEFAULT_REVIEW_PORT_RANGE
    elif (
        not isinstance(review_port_range, list)
        or len(review_port_range) != 2
        or not all(isinstance(n, int) and not isinstance(n, bool) for n in review_port_range)
        or review_port_range[0] <= 0
        or review_port_range[1] < review_port_range[0]
    ):
        raise ConfigError(
            f"config key 'review_port_range' must be a two-element list of positive integers "
            f"[low, high] with low <= high, got {review_port_range!r}"
        )
    else:
        review_port_range = tuple(review_port_range)

    workshop_step_timeout = data.get("workshop_step_timeout", DEFAULT_WORKSHOP_STEP_TIMEOUT)
    if not isinstance(workshop_step_timeout, int) or isinstance(workshop_step_timeout, bool):
        raise ConfigError(
            f"config key 'workshop_step_timeout' must be an integer, got {workshop_step_timeout!r}"
        )

    agent_env = data.get("agent_env", {})
    if not isinstance(agent_env, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in agent_env.items()
    ):
        raise ConfigError(
            f"config key 'agent_env' must be a table of string values, got {agent_env!r}"
        )

    agent_ready_timeout = data.get("agent_ready_timeout", DEFAULT_AGENT_READY_TIMEOUT)
    if (
        not isinstance(agent_ready_timeout, int)
        or isinstance(agent_ready_timeout, bool)
        or agent_ready_timeout <= 0
    ):
        raise ConfigError(
            f"config key 'agent_ready_timeout' must be a positive integer, got {agent_ready_timeout!r}"
        )

    agent_ring_buffer_size = data.get("agent_ring_buffer_size", DEFAULT_AGENT_RING_BUFFER_SIZE)
    if (
        not isinstance(agent_ring_buffer_size, int)
        or isinstance(agent_ring_buffer_size, bool)
        or agent_ring_buffer_size <= 0
    ):
        raise ConfigError(
            f"config key 'agent_ring_buffer_size' must be a positive integer, "
            f"got {agent_ring_buffer_size!r}"
        )

    session_idle_debounce = data.get("session_idle_debounce", DEFAULT_SESSION_IDLE_DEBOUNCE)
    if (
        not isinstance(session_idle_debounce, (int, float))
        or isinstance(session_idle_debounce, bool)
        or session_idle_debounce < 0
    ):
        raise ConfigError(
            f"config key 'session_idle_debounce' must be a non-negative number, "
            f"got {session_idle_debounce!r}"
        )

    stall_threshold = data.get("stall_threshold", DEFAULT_STALL_THRESHOLD)
    if (
        not isinstance(stall_threshold, (int, float))
        or isinstance(stall_threshold, bool)
        or stall_threshold <= 0
    ):
        raise ConfigError(
            f"config key 'stall_threshold' must be a positive number, got {stall_threshold!r}"
        )

    renotify_interval = data.get("renotify_interval", DEFAULT_RENOTIFY_INTERVAL)
    if (
        not isinstance(renotify_interval, (int, float))
        or isinstance(renotify_interval, bool)
        or renotify_interval <= 0
    ):
        raise ConfigError(
            f"config key 'renotify_interval' must be a positive number, got {renotify_interval!r}"
        )

    context_advisory_threshold = data.get(
        "context_advisory_threshold", DEFAULT_CONTEXT_ADVISORY_THRESHOLD
    )
    if (
        not isinstance(context_advisory_threshold, int)
        or isinstance(context_advisory_threshold, bool)
        or not (0 < context_advisory_threshold <= 100)
    ):
        raise ConfigError(
            f"config key 'context_advisory_threshold' must be an integer in (0, 100], "
            f"got {context_advisory_threshold!r}"
        )

    stats_throttle_interval = data.get(
        "stats_throttle_interval", DEFAULT_STATS_THROTTLE_INTERVAL
    )
    if (
        not isinstance(stats_throttle_interval, (int, float))
        or isinstance(stats_throttle_interval, bool)
        or stats_throttle_interval < 0
    ):
        raise ConfigError(
            f"config key 'stats_throttle_interval' must be a non-negative number, "
            f"got {stats_throttle_interval!r}"
        )

    notifications_enabled = data.get("notifications_enabled", DEFAULT_NOTIFICATIONS_ENABLED)
    if not isinstance(notifications_enabled, bool):
        raise ConfigError(
            f"config key 'notifications_enabled' must be a boolean, got {notifications_enabled!r}"
        )

    shutdown_grace = data.get("shutdown_grace", DEFAULT_SHUTDOWN_GRACE)
    if (
        not isinstance(shutdown_grace, (int, float))
        or isinstance(shutdown_grace, bool)
        or shutdown_grace <= 0
    ):
        raise ConfigError(
            f"config key 'shutdown_grace' must be a positive number, got {shutdown_grace!r}"
        )

    recovery_concurrency = data.get("recovery_concurrency", DEFAULT_RECOVERY_CONCURRENCY)
    if (
        not isinstance(recovery_concurrency, int)
        or isinstance(recovery_concurrency, bool)
        or recovery_concurrency <= 0
    ):
        raise ConfigError(
            f"config key 'recovery_concurrency' must be a positive integer, "
            f"got {recovery_concurrency!r}"
        )

    pr_poll_interval = data.get("pr_poll_interval", DEFAULT_PR_POLL_INTERVAL)
    if (
        not isinstance(pr_poll_interval, (int, float))
        or isinstance(pr_poll_interval, bool)
        or pr_poll_interval <= 0
    ):
        raise ConfigError(
            f"config key 'pr_poll_interval' must be a positive number, "
            f"got {pr_poll_interval!r}"
        )

    config_source: dict[str, Any] = {}
    if "renotify_interval" in data:
        config_source["renotify_interval"] = float(renotify_interval)
    if "stall_threshold" in data:
        config_source["stall_threshold"] = float(stall_threshold)
    if "context_advisory_threshold" in data:
        config_source["context_advisory_threshold"] = int(context_advisory_threshold)

    return Config(
        config_source=config_source,
        port=port,
        bind=bind,
        data_dir=data_dir,
        task_dir_root=task_dir_root,
        checkout_root=checkout_root,
        default_branch_pattern=default_branch_pattern,
        spawn_step_timeout=spawn_step_timeout,
        my_workshop_command=my_workshop_command,
        llmvet_command=llmvet_command,
        review_port_range=review_port_range,
        workshop_step_timeout=workshop_step_timeout,
        agent_env=agent_env,
        agent_ready_timeout=agent_ready_timeout,
        agent_ring_buffer_size=agent_ring_buffer_size,
        session_idle_debounce=float(session_idle_debounce),
        stall_threshold=float(stall_threshold),
        renotify_interval=float(renotify_interval),
        context_advisory_threshold=context_advisory_threshold,
        stats_throttle_interval=float(stats_throttle_interval),
        notifications_enabled=notifications_enabled,
        shutdown_grace=float(shutdown_grace),
        recovery_concurrency=recovery_concurrency,
        gpg_signing_key=gpg_signing_key,
        gh_command=gh_command,
        pr_poll_interval=float(pr_poll_interval),
        judge_model=judge_model,
    )


def _path_value(data: dict, key: str, default: Path) -> Path:
    value = data.get(key)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ConfigError(f"config key '{key}' must be a string, got {value!r}")
    return Path(value).expanduser()
