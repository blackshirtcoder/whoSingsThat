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
from .playlists import (
    DuplicatePlaylistNameError,
    InvalidPlaylistNameError,
    PlaylistNotFoundError,
    StoredPlaylist,
    TrackNotFoundError,
    add_tracks_to_playlist,
    create_playlist,
    delete_playlist,
    get_playlist_by_id,
    get_playlist_tracks,
    list_playlists,
    normalize_playlist_name,
    playlist_name_uniqueness_key,
    remove_tracks_from_playlist,
    update_playlist,
)

__all__ = [
    "DuplicatePlaylistNameError",
    "DuplicateTrackPathError",
    "InvalidPlaylistNameError",
    "PlaylistNotFoundError",
    "StoredPlaylist",
    "StoredTrack",
    "TrackNotFoundError",
    "add_tracks_to_playlist",
    "create_playlist",
    "create_sqlite_engine",
    "delete_playlist",
    "get_playlist_by_id",
    "get_playlist_tracks",
    "get_track_by_id",
    "get_track_by_relative_path",
    "import_tracks",
    "list_playlists",
    "list_tracks",
    "normalize_playlist_name",
    "playlist_name_uniqueness_key",
    "remove_tracks_from_playlist",
    "update_playlist",
    "upgrade_database",
]
