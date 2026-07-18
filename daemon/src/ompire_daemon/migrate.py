"""Programmatic Alembic migration runner, so operators never invoke alembic by hand."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config as AlembicConfig

_DAEMON_ROOT = Path(__file__).resolve().parent.parent.parent
_ALEMBIC_INI = _DAEMON_ROOT / "alembic.ini"


def upgrade_head(db_path: Path, *, alembic_ini: Path = _ALEMBIC_INI) -> None:
    cfg = AlembicConfig(str(alembic_ini))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")
