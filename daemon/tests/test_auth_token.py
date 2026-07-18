import stat
from pathlib import Path

from ompire_daemon.auth import load_or_create_token, token_path_for


def test_generates_token_on_first_run(tmp_path: Path) -> None:
    token = load_or_create_token(tmp_path)

    assert len(token) > 20
    path = token_path_for(tmp_path)
    assert path.read_text().strip() == token


def test_token_file_is_owner_only(tmp_path: Path) -> None:
    load_or_create_token(tmp_path)

    mode = stat.S_IMODE(token_path_for(tmp_path).stat().st_mode)
    assert mode == 0o600


def test_reuses_existing_token(tmp_path: Path) -> None:
    first = load_or_create_token(tmp_path)
    second = load_or_create_token(tmp_path)

    assert first == second
