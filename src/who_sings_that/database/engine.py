"""Caller-configured SQLite engine construction."""

from os import PathLike
from pathlib import Path
from sqlite3 import Connection

from sqlalchemy import URL, Engine, create_engine, event


def sqlite_url(database_path: str | PathLike[str]) -> URL:
    """Build a SQLite URL without hand-escaping the caller's filesystem path."""

    path = Path(database_path)
    if path.exists() and path.is_dir():
        raise IsADirectoryError(f"database path identifies a directory: {path}")
    return URL.create("sqlite+pysqlite", database=str(path))


def create_sqlite_engine(database_path: str | PathLike[str]) -> Engine:
    """Create an engine for an existing or future caller-selected SQLite file."""

    engine = create_engine(sqlite_url(database_path))

    @event.listens_for(engine, "connect")
    def enable_sqlite_foreign_keys(dbapi_connection: Connection, _: object) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    return engine
