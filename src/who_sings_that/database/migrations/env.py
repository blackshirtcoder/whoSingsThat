"""Alembic online and offline migration environment."""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from who_sings_that.database import models  # noqa: F401
from who_sings_that.database.base import Base
from who_sings_that.database.engine import sqlite_url

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _configured_url() -> str:
    command_arguments = context.get_x_argument(as_dictionary=True)
    database_path = command_arguments.get("database_path")
    if database_path is not None:
        return sqlite_url(database_path).render_as_string(hide_password=False)

    url = config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError(
            "SQLite database location is required; pass "
            "-x database_path=/path/to/database.sqlite3"
        )
    return url


def run_migrations_offline() -> None:
    """Run migrations without creating an Engine."""

    context.configure(
        url=_configured_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against the configured SQLite database."""

    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = _configured_url()
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
