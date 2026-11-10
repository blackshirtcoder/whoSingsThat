"""Application operations connecting persistence and the game domain."""

from .start_game import (
    InvalidMusicRootError,
    InvalidSelectedPlaylistIdsError,
    NoStructurallyEligibleTracksError,
    PlaylistResolution,
    SelectedPlaylistNotFoundError,
    StartGameResult,
    TrackExclusion,
    TrackExclusionReason,
    resolve_selected_playlists,
    start_game_from_persistent_playlists,
)

__all__ = [
    "InvalidMusicRootError",
    "InvalidSelectedPlaylistIdsError",
    "NoStructurallyEligibleTracksError",
    "PlaylistResolution",
    "SelectedPlaylistNotFoundError",
    "StartGameResult",
    "TrackExclusion",
    "TrackExclusionReason",
    "resolve_selected_playlists",
    "start_game_from_persistent_playlists",
]
