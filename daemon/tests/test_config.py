from pathlib import Path

import pytest

from ompire_daemon.config import Config, ConfigError, load_config


def test_defaults_when_file_absent(tmp_path: Path) -> None:
    config = load_config(tmp_path / "does-not-exist.toml")
    assert config == Config()


def test_loads_overrides(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('port = 9999\nbind = "0.0.0.0"\ndata_dir = "/tmp/ompire-data"\n')

    config = load_config(config_path)

    assert config.port == 9999
    assert config.bind == "0.0.0.0"
    assert config.data_dir == Path("/tmp/ompire-data")
    assert config.task_dir_root == Config().task_dir_root


def test_malformed_toml_fails_fast(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("this is not [ valid toml")

    with pytest.raises(ConfigError, match="malformed TOML"):
        load_config(config_path)


def test_unknown_key_fails_fast(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('bogus_key = "value"\n')

    with pytest.raises(ConfigError, match="bogus_key"):
        load_config(config_path)


def test_invalid_port_type_fails_fast(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('port = "not-a-number"\n')

    with pytest.raises(ConfigError, match="port"):
        load_config(config_path)


def test_workshop_keys_load(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        'my_workshop_command = ["my-workshop", "--quiet"]\nworkshop_step_timeout = 1200\n'
    )

    config = load_config(config_path)

    assert config.my_workshop_command == ("my-workshop", "--quiet")
    assert config.workshop_step_timeout == 1200


def test_workshop_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path / "does-not-exist.toml")

    assert config.my_workshop_command == ("my-workshop",)
    assert config.workshop_step_timeout == 600


def test_invalid_my_workshop_command_fails_fast(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('my_workshop_command = "my-workshop"\n')

    with pytest.raises(ConfigError, match="my_workshop_command"):
        load_config(config_path)


def test_empty_my_workshop_command_fails_fast(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('my_workshop_command = []\n')

    with pytest.raises(ConfigError, match="my_workshop_command"):
        load_config(config_path)


def test_invalid_workshop_step_timeout_fails_fast(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('workshop_step_timeout = "long"\n')

    with pytest.raises(ConfigError, match="workshop_step_timeout"):
        load_config(config_path)


def test_agent_config_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path / "does-not-exist.toml")

    assert config.agent_env == {}
    assert config.agent_ready_timeout == 30
    assert config.agent_ring_buffer_size == 1000


def test_agent_config_values(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "agent_ready_timeout = 60\n"
        "agent_ring_buffer_size = 500\n"
        "[agent_env]\n"
        'ANTHROPIC_BASE_URL = "http://localhost:4000"\n'
        'ANTHROPIC_API_KEY = "sk-test"\n'
    )

    config = load_config(config_path)

    assert config.agent_env == {
        "ANTHROPIC_BASE_URL": "http://localhost:4000",
        "ANTHROPIC_API_KEY": "sk-test",
    }
    assert config.agent_ready_timeout == 60
    assert config.agent_ring_buffer_size == 500


def test_agent_env_must_be_string_table(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("[agent_env]\nTIMEOUT = 5\n")

    with pytest.raises(ConfigError, match="agent_env"):
        load_config(config_path)


def test_invalid_agent_ready_timeout_fails_fast(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("agent_ready_timeout = 0\n")

    with pytest.raises(ConfigError, match="agent_ready_timeout"):
        load_config(config_path)


def test_invalid_agent_ring_buffer_size_fails_fast(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('agent_ring_buffer_size = "big"\n')

    with pytest.raises(ConfigError, match="agent_ring_buffer_size"):
        load_config(config_path)


def test_session_idle_debounce_default_and_override(tmp_path: Path) -> None:
    assert load_config(tmp_path / "does-not-exist.toml").session_idle_debounce == 2.0

    config_path = tmp_path / "config.toml"
    config_path.write_text("session_idle_debounce = 0.5\n")
    assert load_config(config_path).session_idle_debounce == 0.5


def test_invalid_session_idle_debounce_fails_fast(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("session_idle_debounce = -1\n")

    with pytest.raises(ConfigError, match="session_idle_debounce"):
        load_config(config_path)


def test_attention_config_defaults(tmp_path: Path) -> None:
    config = load_config(tmp_path / "does-not-exist.toml")

    assert config.stall_threshold == 300
    assert config.renotify_interval == 300
    assert config.context_advisory_threshold == 80
    assert config.stats_throttle_interval == 10
    assert config.notifications_enabled is True


def test_attention_config_overrides(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "stall_threshold = 60\n"
        "renotify_interval = 30\n"
        "context_advisory_threshold = 90\n"
        "stats_throttle_interval = 5\n"
        "notifications_enabled = false\n"
    )

    config = load_config(config_path)

    assert config.stall_threshold == 60
    assert config.renotify_interval == 30
    assert config.context_advisory_threshold == 90
    assert config.stats_throttle_interval == 5
    assert config.notifications_enabled is False


def test_invalid_stall_threshold_fails_fast(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("stall_threshold = 0\n")

    with pytest.raises(ConfigError, match="stall_threshold"):
        load_config(config_path)


def test_invalid_renotify_interval_fails_fast(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("renotify_interval = -5\n")

    with pytest.raises(ConfigError, match="renotify_interval"):
        load_config(config_path)


def test_invalid_context_advisory_threshold_fails_fast(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("context_advisory_threshold = 101\n")

    with pytest.raises(ConfigError, match="context_advisory_threshold"):
        load_config(config_path)


def test_invalid_stats_throttle_interval_fails_fast(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text("stats_throttle_interval = -1\n")

    with pytest.raises(ConfigError, match="stats_throttle_interval"):
        load_config(config_path)


def test_invalid_notifications_enabled_fails_fast(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('notifications_enabled = "yes"\n')

    with pytest.raises(ConfigError, match="notifications_enabled"):
        load_config(config_path)
