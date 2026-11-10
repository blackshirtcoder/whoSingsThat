"""Resolve persistent playlists and orchestrate authoritative Start Game."""

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from math import isfinite
from numbers import Real
from os import PathLike
from pathlib import Path
from stat import S_ISDIR

from sqlalchemy import Engine

from who_sings_that.catalog import MediaStatus, TrackMetadata, extract_track_metadata
from who_sings_that.database import (
    PlaylistNotFoundError,
    StoredTrack,
    get_playlist_by_id,
    get_playlist_tracks,
)
from who_sings_that.domain import (
    EmptyDeckError,
    GameEngine,
    GameSession,
    ResolvedPlaylist,
    SelectedPlaylistSnapshot,
    TrackReference,
)

MediaInspector = Callable[[str | PathLike[str], str | PathLike[str]], TrackMetadata]


class InvalidMusicRootError(RuntimeError):
    """Raised when the configured music root cannot be used for resolution."""

    def __init__(self, music_root: Path, reason: str):
        super().__init__(f"configured music root is invalid: {music_root} ({reason})")
        self.music_root = music_root
        self.reason = reason


class InvalidSelectedPlaylistIdsError(ValueError):
    """Raised when selected playlist identities are malformed or repeated."""


class SelectedPlaylistNotFoundError(LookupError):
    """Raised when an exact selected persistent playlist no longer exists."""

    def __init__(self, playlist_id: str):
        super().__init__(f"selected playlist does not exist: {playlist_id}")
        self.playlist_id = playlist_id


class TrackExclusionReason(Enum):
    """Why one persistent member was excluded from the structural deck."""

    CATALOG_DISABLED = "catalog_disabled"
    CATALOG_INELIGIBLE = "catalog_ineligible"
    INVALID_RELATIVE_PATH = "invalid_relative_path"
    FILE_MISSING = "file_missing"
    FILE_UNREADABLE = "file_unreadable"
    INVALID_MEDIA = "invalid_media"
    UNUSABLE_DURATION = "unusable_duration"


@dataclass(frozen=True, slots=True)
class TrackExclusion:
    """Internal diagnostic for one unique candidate excluded at resolution."""

    track_id: int
    playlist_ids: tuple[str, ...]
    reason: TrackExclusionReason
    detail: str


@dataclass(frozen=True, slots=True)
class PlaylistResolution:
    """Resolved domain inputs plus non-playable member diagnostics."""

    resolved_playlists: tuple[ResolvedPlaylist, ...]
    exclusions: tuple[TrackExclusion, ...]


class NoStructurallyEligibleTracksError(EmptyDeckError):
    """Raised when resolution leaves transition B without a structural deck."""

    def __init__(self, resolution: PlaylistResolution):
        super().__init__("selected playlists contain no structurally eligible tracks")
        self.resolution = resolution


@dataclass(frozen=True, slots=True)
class StartGameResult:
    """The authoritative game after Start Game and its resolution report."""

    game: GameSession
    resolution: PlaylistResolution


@dataclass(frozen=True, slots=True)
class _PersistentPlaylist:
    playlist_id: str
    name: str
    created_at: datetime
    song_count: int
    tracks: tuple[StoredTrack, ...]


@dataclass(frozen=True, slots=True)
class _CandidateResult:
    track: TrackReference | None
    reason: TrackExclusionReason | None = None
    detail: str = ""


def resolve_selected_playlists(
    database_engine: Engine,
    music_root: str | PathLike[str],
    selected_playlist_ids: Iterable[str],
    *,
    inspect_media: MediaInspector = extract_track_metadata,
) -> PlaylistResolution:
    """Resolve exact persistent selections into structurally valid domain inputs.

    Playlists are returned by playlist ID for deterministic behavior. Each
    unique catalog track is structurally inspected at most once, then the same
    resolution result is reused for every selected playlist membership.
    """

    root = _validated_music_root(music_root)
    playlist_ids = _validated_playlist_ids(selected_playlist_ids)
    persistent_playlists = _load_playlists(database_engine, playlist_ids)

    candidate_tracks: dict[int, StoredTrack] = {}
    membership_ids: dict[int, list[str]] = {}
    for playlist in persistent_playlists:
        for track in playlist.tracks:
            candidate_tracks.setdefault(track.track_id, track)
            membership_ids.setdefault(track.track_id, []).append(playlist.playlist_id)

    candidate_results = {
        track_id: _resolve_candidate(root, track, inspect_media)
        for track_id, track in candidate_tracks.items()
    }

    resolved_playlists = tuple(
        ResolvedPlaylist(
            snapshot=SelectedPlaylistSnapshot(
                playlist_id=playlist.playlist_id,
                playlist_name=playlist.name,
                playlist_created_at=playlist.created_at,
                playlist_song_count=playlist.song_count,
            ),
            tracks=tuple(
                result.track
                for track in playlist.tracks
                if (result := candidate_results[track.track_id]).track is not None
            ),
        )
        for playlist in persistent_playlists
    )
    exclusions = tuple(
        TrackExclusion(
            track_id=track_id,
            playlist_ids=tuple(membership_ids[track_id]),
            reason=result.reason,
            detail=result.detail,
        )
        for track_id, result in candidate_results.items()
        if result.track is None and result.reason is not None
    )
    return PlaylistResolution(resolved_playlists, exclusions)


def start_game_from_persistent_playlists(
    game_engine: GameEngine,
    database_engine: Engine,
    music_root: str | PathLike[str],
    requesting_browser_token: str,
    *,
    inspect_media: MediaInspector = extract_track_metadata,
) -> StartGameResult:
    """Resolve authoritative lobby selections and request transition B."""

    selected_playlist_ids = game_engine.validate_start_game_request(
        requesting_browser_token
    )
    resolution = resolve_selected_playlists(
        database_engine,
        music_root,
        selected_playlist_ids,
        inspect_media=inspect_media,
    )
    try:
        game = game_engine.start_game(
            requesting_browser_token,
            resolution.resolved_playlists,
        )
    except EmptyDeckError as error:
        raise NoStructurallyEligibleTracksError(resolution) from error
    return StartGameResult(game, resolution)


def _validated_music_root(music_root: str | PathLike[str]) -> Path:
    root = Path(music_root)
    if not root.is_absolute():
        raise InvalidMusicRootError(root, "path must be absolute")
    try:
        root_status = root.stat()
        if not S_ISDIR(root_status.st_mode):
            raise InvalidMusicRootError(root, "path is not a directory")
        if not os.access(root, os.R_OK | os.X_OK):
            raise InvalidMusicRootError(root, "directory cannot be accessed")
    except InvalidMusicRootError:
        raise
    except FileNotFoundError as error:
        raise InvalidMusicRootError(root, "path does not exist") from error
    except OSError as error:
        raise InvalidMusicRootError(
            root,
            f"directory cannot be accessed ({type(error).__name__})",
        ) from error
    return root


def _validated_playlist_ids(selected_playlist_ids: Iterable[str]) -> tuple[str, ...]:
    try:
        playlist_ids = tuple(selected_playlist_ids)
    except TypeError as error:
        raise InvalidSelectedPlaylistIdsError(
            "selected_playlist_ids must be iterable"
        ) from error

    for playlist_id in playlist_ids:
        if not isinstance(playlist_id, str) or not playlist_id.strip():
            raise InvalidSelectedPlaylistIdsError(
                "selected playlist IDs must be non-empty strings"
            )
    if len(playlist_ids) != len(set(playlist_ids)):
        raise InvalidSelectedPlaylistIdsError(
            "selected playlist IDs must not contain duplicates"
        )
    return tuple(sorted(playlist_ids))


def _load_playlists(
    database_engine: Engine,
    playlist_ids: tuple[str, ...],
) -> tuple[_PersistentPlaylist, ...]:
    playlists: list[_PersistentPlaylist] = []
    for playlist_id in playlist_ids:
        playlist = get_playlist_by_id(database_engine, playlist_id)
        if playlist is None:
            raise SelectedPlaylistNotFoundError(playlist_id)
        try:
            tracks = get_playlist_tracks(database_engine, playlist_id)
        except PlaylistNotFoundError as error:
            raise SelectedPlaylistNotFoundError(playlist_id) from error
        playlists.append(
            _PersistentPlaylist(
                playlist_id=playlist.playlist_id,
                name=playlist.name,
                created_at=playlist.created_at,
                song_count=playlist.song_count,
                tracks=tracks,
            )
        )
    return tuple(playlists)


def _resolve_candidate(
    music_root: Path,
    track: StoredTrack,
    inspect_media: MediaInspector,
) -> _CandidateResult:
    if not track.enabled:
        return _CandidateResult(
            track=None,
            reason=TrackExclusionReason.CATALOG_DISABLED,
            detail="the catalog track is disabled",
        )
    if not track.is_catalog_eligible:
        return _CandidateResult(
            track=None,
            reason=TrackExclusionReason.CATALOG_INELIGIBLE,
            detail=(
                f"enabled={track.enabled}, "
                f"metadata_status={track.metadata_status.value}, "
                f"media_status={track.media_status.value}"
            ),
        )

    try:
        inspected = inspect_media(music_root, track.relative_path)
    except ValueError as error:
        return _CandidateResult(
            track=None,
            reason=TrackExclusionReason.INVALID_RELATIVE_PATH,
            detail=str(error),
        )
    except FileNotFoundError as error:
        return _CandidateResult(
            track=None,
            reason=TrackExclusionReason.FILE_MISSING,
            detail=str(error) or "file was not found",
        )
    except OSError as error:
        return _CandidateResult(
            track=None,
            reason=TrackExclusionReason.FILE_UNREADABLE,
            detail=f"media could not be inspected ({type(error).__name__})",
        )

    if inspected.media_status is MediaStatus.MISSING:
        reason = TrackExclusionReason.FILE_MISSING
    elif inspected.media_status is MediaStatus.UNREADABLE:
        reason = TrackExclusionReason.FILE_UNREADABLE
    elif inspected.media_status is MediaStatus.INVALID:
        reason = TrackExclusionReason.INVALID_MEDIA
    else:
        reason = None

    if reason is not None:
        return _CandidateResult(
            track=None,
            reason=reason,
            detail=_inspection_detail(inspected),
        )

    duration = inspected.duration_seconds
    if (
        isinstance(duration, bool)
        or not isinstance(duration, Real)
        or not isfinite(duration)
        or duration <= 0
    ):
        return _CandidateResult(
            track=None,
            reason=TrackExclusionReason.UNUSABLE_DURATION,
            detail="structural inspection did not provide a positive finite duration",
        )
    return _CandidateResult(TrackReference(track.track_id, duration))


def _inspection_detail(inspected: TrackMetadata) -> str:
    if inspected.problems:
        return "; ".join(problem.message for problem in inspected.problems)
    return f"current media status is {inspected.media_status.value}"
