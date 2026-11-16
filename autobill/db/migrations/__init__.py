"""Alembic migration runner for Sistema-Monitoramento-Boletos."""

from pathlib import Path

from alembic import command
from alembic.config import Config


def run_migrations() -> None:
    """Run `alembic upgrade head` using project alembic.ini."""
    ini_path = Path(__file__).resolve().parents[3] / "alembic.ini"
    cfg = Config(str(ini_path))
    command.upgrade(cfg, "head")
