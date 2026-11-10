"""Persistent SQLite storage for Who Sings That?."""

from .catalog import (
    DuplicateTrackPathError,
    StoredTrack,
    get_track_by_id,
    get_track_by_relative_path,
    import_tracks,
    list_tracks,
)
from .engine import create_sqlite_engine
from .migration import upgrade_database

__all__ = [
    "DuplicateTrackPathError",
    "StoredTrack",
    "create_sqlite_engine",
    "get_track_by_id",
    "get_track_by_relative_path",
    "import_tracks",
    "list_tracks",
    "upgrade_database",
]
