"""Caller-configured SQLite engine construction."""

from os import PathLike
from pathlib import Path

from sqlalchemy import URL, Engine, create_engine


def sqlite_url(database_path: str | PathLike[str]) -> URL:
    """Build a SQLite URL without hand-escaping the caller's filesystem path."""

    path = Path(database_path)
    if path.exists() and path.is_dir():
        raise IsADirectoryError(f"database path identifies a directory: {path}")
    return URL.create("sqlite+pysqlite", database=str(path))


def create_sqlite_engine(database_path: str | PathLike[str]) -> Engine:
    """Create an engine for an existing or future caller-selected SQLite file."""

    return create_engine(sqlite_url(database_path))
