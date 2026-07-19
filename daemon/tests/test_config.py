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
