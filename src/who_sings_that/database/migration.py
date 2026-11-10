"""Alembic-backed schema migration entry point."""

from os import PathLike
from pathlib import Path

from alembic import command
from alembic.config import Config

from .engine import sqlite_url


def upgrade_database(database_path: str | PathLike[str]) -> None:
    """Upgrade the caller-selected SQLite database to the latest schema."""

    migrations_path = Path(__file__).with_name("migrations")
    config = Config()
    config.set_main_option("script_location", str(migrations_path))
    config.set_main_option(
        "sqlalchemy.url",
        _alembic_config_value(
            sqlite_url(database_path).render_as_string(hide_password=False)
        ),
    )
    command.upgrade(config, "head")


def _alembic_config_value(value: str) -> str:
    return value.replace("%", "%%")
