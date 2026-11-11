from collections.abc import Callable, Iterator
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

import pytest
from sqlalchemy import Engine, text

import who_sings_that.application.start_game as start_game_module
from who_sings_that.application import (
    InvalidMusicRootError,
    NoStructurallyEligibleTracksError,
    PlaylistResolution,
    SelectedPlaylistNotFoundError,
    TrackExclusionReason,
    resolve_selected_playlists,
    start_game_from_persistent_playlists,
)
from who_sings_that.catalog import (
    MediaStatus,
    MetadataSource,
    MetadataStatus,
    TrackMetadata,
    extract_track_metadata,
)
from who_sings_that.database import (
    add_tracks_to_playlist,
    create_playlist,
    create_sqlite_engine,
    get_playlist_by_id,
    get_playlist_tracks,
    import_tracks,
    list_playlists,
    list_tracks,
    upgrade_database,
)
from who_sings_that.domain import (
    DeckSkipReason,
    EmptyDeckError,
    GameEndReason,
    GameEngine,
    GameSession,
    GameState,
    ResolvedPlaylist,
    SkippedDeckEntry,
    StartGameNotAllowedError,
    TrackReference,
    UnauthorizedStartGameError,
)

CREATED_AT = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
STARTED_AT = datetime(2026, 11, 10, 12, 1, tzinfo=UTC)
ENDED_AT = datetime(2026, 11, 10, 12, 2, tzinfo=UTC)


class SequenceClock:
    def __init__(self, *values: datetime) -> None:
        self._values = iter(values)

    def __call__(self) -> datetime:
        return next(self._values)


@pytest.fixture
def catalog_database(tmp_path: Path) -> Iterator[tuple[Engine, Path]]:
    database_path = tmp_path / "catalog.sqlite3"
    upgrade_database(database_path)
    engine = create_sqlite_engine(database_path)
    music_root = tmp_path / "music"
    music_root.mkdir()
    yield engine, music_root
    engine.dispose()


def import_catalog_track(
    engine: Engine,
    relative_path: str,
    *,
    stored_duration: float = 180.0,
) -> int:
    (track,) = import_tracks(
        engine,
        [
            TrackMetadata(
                relative_path=Path(relative_path),
                artist="Stored Artist",
                title="Stored Title",
                duration_seconds=stored_duration,
                file_size_bytes=10_000,
                metadata_source=MetadataSource.FILENAME,
                metadata_status=MetadataStatus.COMPLETE,
                media_status=MediaStatus.VALID,
            )
        ],
    )
    return track.track_id


def create_catalog_playlist(engine: Engine, name: str, track_ids: list[int]) -> str:
    playlist = create_playlist(engine, name)
    add_tracks_to_playlist(engine, playlist.playlist_id, track_ids)
    return playlist.playlist_id


def write_test_mp3(path: Path, *, frame_count: int = 20) -> None:
    """Write deterministic MPEG frames that Mutagen can inspect."""

    path.parent.mkdir(parents=True, exist_ok=True)
    header = bytes.fromhex("fffb9064")
    frame = header + bytes(417 - len(header))
    path.write_bytes(frame * frame_count)


def inspected_metadata(
    relative_path: str | Path,
    duration: float | None,
    *,
    media_status: MediaStatus = MediaStatus.VALID,
) -> TrackMetadata:
    return TrackMetadata(
        relative_path=Path(relative_path),
        artist="Inspected Artist",
        title="Inspected Title",
        duration_seconds=duration,
        file_size_bytes=20_000,
        metadata_source=MetadataSource.FILENAME,
        metadata_status=MetadataStatus.COMPLETE,
        media_status=media_status,
    )


def duration_inspector(
    durations: dict[str, float],
    calls: list[str] | None = None,
) -> Callable[[str | Path, str | Path], TrackMetadata]:
    def inspect_media(
        music_root: str | Path,
        relative_path: str | Path,
    ) -> TrackMetadata:
        relative = Path(relative_path).as_posix()
        if calls is not None:
            calls.append(relative)
        return inspected_metadata(relative, durations[relative])

    return inspect_media


def make_game_engine(
    *,
    shuffle: Callable[[list[TrackReference]], None] | None = None,
) -> GameEngine:
    clock = SequenceClock(CREATED_AT, STARTED_AT, ENDED_AT)
    if shuffle is None:
        return GameEngine(clock=clock)
    return GameEngine(clock=clock, shuffle=shuffle)


def selected_lobby(game_engine: GameEngine, *playlist_ids: str) -> GameSession:
    game = game_engine.create_game("songmaster-token", "Songmaster")
    for playlist_id in playlist_ids:
        game_engine.select_playlist("songmaster-token", playlist_id)
    return game


def resolved_by_id(resolution: PlaylistResolution) -> dict[str, ResolvedPlaylist]:
    return {
        playlist.snapshot.playlist_id: playlist
        for playlist in resolution.resolved_playlists
    }


def test_resolves_persistent_snapshots_and_overlaps_with_one_inspection_per_track(
    catalog_database: tuple[Engine, Path],
) -> None:
    database_engine, music_root = catalog_database
    shared_id = import_catalog_track(database_engine, "Rock/A - Shared.mp3")
    first_only_id = import_catalog_track(database_engine, "Rock/B - First.mp3")
    second_only_id = import_catalog_track(database_engine, "Rock/C - Second.mp3")
    first_id = create_catalog_playlist(
        database_engine,
        "First List",
        [shared_id, first_only_id],
    )
    second_id = create_catalog_playlist(
        database_engine,
        "Second List",
        [shared_id, second_only_id],
    )
    calls: list[str] = []
    inspector = duration_inspector(
        {
            "Rock/A - Shared.mp3": 101.0,
            "Rock/B - First.mp3": 102.0,
            "Rock/C - Second.mp3": 103.0,
        },
        calls,
    )

    resolution = resolve_selected_playlists(
        database_engine,
        music_root,
        [second_id, first_id],
        inspect_media=inspector,
    )

    assert [playlist.snapshot.playlist_id for playlist in resolution.resolved_playlists] == (
        sorted([first_id, second_id])
    )
    playlists = resolved_by_id(resolution)
    first = playlists[first_id]
    second = playlists[second_id]
    stored_first = get_playlist_by_id(database_engine, first_id)
    stored_second = get_playlist_by_id(database_engine, second_id)
    assert stored_first is not None
    assert stored_second is not None
    assert first.snapshot.playlist_name == stored_first.name
    assert first.snapshot.playlist_created_at == stored_first.created_at
    assert first.snapshot.playlist_song_count == 2
    assert second.snapshot.playlist_name == stored_second.name
    assert second.snapshot.playlist_created_at == stored_second.created_at
    assert second.snapshot.playlist_song_count == 2
    first_tracks = {track.track_id: track for track in first.tracks}
    second_tracks = {track.track_id: track for track in second.tracks}
    assert first_tracks[shared_id] is second_tracks[shared_id]
    assert first_tracks[first_only_id].duration_seconds == 102.0
    assert first_tracks[first_only_id].artist == "Stored Artist"
    assert first_tracks[first_only_id].title == "Stored Title"
    assert second_tracks[second_only_id].duration_seconds == 103.0
    assert sorted(calls) == [
        "Rock/A - Shared.mp3",
        "Rock/B - First.mp3",
        "Rock/C - Second.mp3",
    ]
    assert resolution.exclusions == ()


def test_mixed_members_report_exclusions_and_preserve_full_membership_count(
    catalog_database: tuple[Engine, Path],
) -> None:
    database_engine, music_root = catalog_database
    valid_id = import_catalog_track(database_engine, "Rock/A - Valid.mp3")
    disabled_id = import_catalog_track(database_engine, "Rock/B - Disabled.mp3")
    missing_id = import_catalog_track(database_engine, "Rock/C - Missing.mp3")
    invalid_id = import_catalog_track(database_engine, "Rock/D - Invalid.mp3")
    unreadable_id = import_catalog_track(database_engine, "Rock/E - Unreadable.mp3")
    playlist_id = create_catalog_playlist(
        database_engine,
        "Mixed",
        [valid_id, disabled_id, missing_id, invalid_id, unreadable_id],
    )
    with database_engine.begin() as connection:
        connection.execute(
            text("UPDATE tracks SET enabled = 0 WHERE track_id = :track_id"),
            {"track_id": disabled_id},
        )
    write_test_mp3(music_root / "Rock/A - Valid.mp3", frame_count=1600)
    invalid_path = music_root / "Rock/D - Invalid.mp3"
    invalid_path.parent.mkdir(parents=True, exist_ok=True)
    invalid_path.write_bytes(b"not MPEG audio")
    write_test_mp3(music_root / "Rock/E - Unreadable.mp3")
    inspection_calls: list[str] = []

    def inspect_media(
        root: str | Path,
        relative_path: str | Path,
    ) -> TrackMetadata:
        relative = Path(relative_path).as_posix()
        inspection_calls.append(relative)
        if relative == "Rock/E - Unreadable.mp3":
            raise PermissionError("deterministic test access denial")
        return extract_track_metadata(root, relative_path)

    resolution = resolve_selected_playlists(
        database_engine,
        music_root,
        [playlist_id],
        inspect_media=inspect_media,
    )

    (resolved,) = resolution.resolved_playlists
    assert resolved.snapshot.playlist_song_count == 5
    assert [track.track_id for track in resolved.tracks] == [valid_id]
    exclusions = {exclusion.track_id: exclusion for exclusion in resolution.exclusions}
    assert exclusions[disabled_id].reason is TrackExclusionReason.CATALOG_DISABLED
    assert exclusions[missing_id].reason is TrackExclusionReason.FILE_MISSING
    assert exclusions[invalid_id].reason is TrackExclusionReason.INVALID_MEDIA
    assert exclusions[unreadable_id].reason is TrackExclusionReason.FILE_UNREADABLE
    assert all(exclusion.playlist_ids == (playlist_id,) for exclusion in exclusions.values())
    assert "Rock/B - Disabled.mp3" not in inspection_calls
    assert len(inspection_calls) == 4


def test_uses_current_inspected_duration_without_updating_stored_duration(
    catalog_database: tuple[Engine, Path],
) -> None:
    database_engine, music_root = catalog_database
    track_id = import_catalog_track(
        database_engine,
        "Portable/Artist - Song.mp3",
        stored_duration=999.0,
    )
    playlist_id = create_catalog_playlist(database_engine, "Duration", [track_id])
    write_test_mp3(music_root / "Portable/Artist - Song.mp3", frame_count=20)
    before = list_tracks(database_engine)

    resolution = resolve_selected_playlists(
        database_engine,
        music_root,
        [playlist_id],
    )

    (track,) = resolution.resolved_playlists[0].tracks
    assert track.duration_seconds == pytest.approx(0.52125)
    assert track.duration_seconds != 999.0
    assert list_tracks(database_engine) == before


@pytest.mark.parametrize("duration", [None, 0.0, float("nan")])
def test_valid_media_without_usable_duration_is_excluded(
    catalog_database: tuple[Engine, Path],
    duration: float | None,
) -> None:
    database_engine, music_root = catalog_database
    relative_path = "Rock/Artist - No Duration.mp3"
    track_id = import_catalog_track(database_engine, relative_path)
    playlist_id = create_catalog_playlist(database_engine, "No Duration", [track_id])

    resolution = resolve_selected_playlists(
        database_engine,
        music_root,
        [playlist_id],
        inspect_media=lambda root, path: inspected_metadata(path, duration),
    )

    assert resolution.resolved_playlists[0].tracks == ()
    assert resolution.exclusions[0].track_id == track_id
    assert resolution.exclusions[0].reason is TrackExclusionReason.UNUSABLE_DURATION


def test_same_relative_path_resolves_portably_against_different_music_roots(
    catalog_database: tuple[Engine, Path], tmp_path: Path
) -> None:
    database_engine, first_root = catalog_database
    relative_path = Path("Mirror/Artist - Song.mp3")
    track_id = import_catalog_track(database_engine, relative_path.as_posix())
    playlist_id = create_catalog_playlist(database_engine, "Portable", [track_id])
    second_root = tmp_path / "nas-mirror"
    second_root.mkdir()
    write_test_mp3(first_root / relative_path, frame_count=20)
    write_test_mp3(second_root / relative_path, frame_count=40)

    first = resolve_selected_playlists(database_engine, first_root, [playlist_id])
    second = resolve_selected_playlists(database_engine, second_root, [playlist_id])

    first_duration = first.resolved_playlists[0].tracks[0].duration_seconds
    second_duration = second.resolved_playlists[0].tracks[0].duration_seconds
    assert first_duration == pytest.approx(0.52125)
    assert second_duration == pytest.approx(1.0425)
    assert first_duration != second_duration


def test_empty_playlist_resolves_without_inspection(
    catalog_database: tuple[Engine, Path],
) -> None:
    database_engine, music_root = catalog_database
    playlist_id = create_playlist(database_engine, "Empty").playlist_id

    def unexpected_inspection(*args: object) -> NoReturn:
        raise AssertionError("empty playlists must not inspect media")

    resolution = resolve_selected_playlists(
        database_engine,
        music_root,
        [playlist_id],
        inspect_media=unexpected_inspection,
    )

    (resolved,) = resolution.resolved_playlists
    assert resolved.snapshot.playlist_song_count == 0
    assert resolved.tracks == ()
    assert resolution.exclusions == ()


def test_missing_selected_playlist_is_rejected_explicitly(
    catalog_database: tuple[Engine, Path],
) -> None:
    database_engine, music_root = catalog_database

    with pytest.raises(SelectedPlaylistNotFoundError) as raised:
        resolve_selected_playlists(database_engine, music_root, ["deleted-playlist"])

    assert raised.value.playlist_id == "deleted-playlist"


@pytest.mark.parametrize("root_kind", ["relative", "missing", "file", "inaccessible"])
def test_invalid_or_inaccessible_music_root_is_an_operational_error(
    catalog_database: tuple[Engine, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    root_kind: str,
) -> None:
    database_engine, valid_root = catalog_database
    playlist_id = create_playlist(database_engine, "Empty").playlist_id
    if root_kind == "relative":
        music_root = Path("relative/music")
    elif root_kind == "missing":
        music_root = tmp_path / "missing"
    elif root_kind == "file":
        music_root = tmp_path / "not-a-directory"
        music_root.write_text("file")
    else:
        music_root = valid_root

        def deny_root_access(path: object, mode: int) -> bool:
            return False

        monkeypatch.setattr(start_game_module.os, "access", deny_root_access)

    with pytest.raises(InvalidMusicRootError):
        resolve_selected_playlists(database_engine, music_root, [playlist_id])


def test_resolution_does_not_mutate_catalog_playlists_or_memberships(
    catalog_database: tuple[Engine, Path],
) -> None:
    database_engine, music_root = catalog_database
    track_id = import_catalog_track(database_engine, "Rock/A - Stable.mp3")
    playlist_id = create_catalog_playlist(database_engine, "Stable", [track_id])
    before_tracks = list_tracks(database_engine)
    before_playlists = list_playlists(database_engine)
    before_members = get_playlist_tracks(database_engine, playlist_id)

    resolve_selected_playlists(
        database_engine,
        music_root,
        [playlist_id],
        inspect_media=duration_inspector({"Rock/A - Stable.mp3": 181.5}),
    )

    assert list_tracks(database_engine) == before_tracks
    assert list_playlists(database_engine) == before_playlists
    assert get_playlist_tracks(database_engine, playlist_id) == before_members


def test_application_start_builds_deduplicated_immutable_deck_and_shuffles_once(
    catalog_database: tuple[Engine, Path],
) -> None:
    database_engine, music_root = catalog_database
    shared_id = import_catalog_track(database_engine, "Rock/A - Shared.mp3")
    first_id = import_catalog_track(database_engine, "Rock/B - First.mp3")
    second_id = import_catalog_track(database_engine, "Rock/C - Second.mp3")
    rock_id = create_catalog_playlist(database_engine, "Rock", [shared_id, first_id])
    favorites_id = create_catalog_playlist(
        database_engine,
        "Favorites",
        [shared_id, second_id],
    )
    shuffle_calls: list[list[int]] = []

    def recording_shuffle(tracks: list[TrackReference]) -> None:
        shuffle_calls.append([track.track_id for track in tracks])
        tracks.reverse()

    game_engine = make_game_engine(shuffle=recording_shuffle)
    lobby = selected_lobby(game_engine, rock_id, favorites_id)
    before_tracks = list_tracks(database_engine)
    before_playlists = list_playlists(database_engine)
    calls: list[str] = []
    inspector = duration_inspector(
        {
            "Rock/A - Shared.mp3": 180.0,
            "Rock/B - First.mp3": 181.0,
            "Rock/C - Second.mp3": 182.0,
        },
        calls,
    )

    result = start_game_from_persistent_playlists(
        game_engine,
        database_engine,
        music_root,
        "songmaster-token",
        inspect_media=inspector,
    )

    game = result.game
    assert game is lobby
    assert game_engine.state is GameState.ROUND_COMPLETE
    assert len(shuffle_calls) == 1
    assert set(shuffle_calls[0]) == {shared_id, first_id, second_id}
    assert len(shuffle_calls[0]) == 3
    assert sorted(calls) == [
        "Rock/A - Shared.mp3",
        "Rock/B - First.mp3",
        "Rock/C - Second.mp3",
    ]
    assert isinstance(game.deck, tuple)
    assert game.deck is not None
    assert [entry.track.track_id for entry in game.deck] == list(
        reversed(shuffle_calls[0])
    )
    assert [entry.original_position for entry in game.deck] == [1, 2, 3]
    assert game.original_deck_size == 3
    assert game.selected_playlist_ids == frozenset({rock_id, favorites_id})
    assert len(game.selected_playlist_snapshots) == 2
    assert list_tracks(database_engine) == before_tracks
    assert list_playlists(database_engine) == before_playlists


def test_structurally_valid_short_track_enters_initial_deck_then_is_skipped(
    catalog_database: tuple[Engine, Path],
) -> None:
    database_engine, music_root = catalog_database
    short_id = import_catalog_track(database_engine, "Rock/A - Short.mp3")
    long_id = import_catalog_track(database_engine, "Rock/B - Long.mp3")
    playlist_id = create_catalog_playlist(database_engine, "Mixed Length", [short_id, long_id])
    game_engine = make_game_engine(shuffle=lambda tracks: None)
    selected_lobby(game_engine, playlist_id)

    result = start_game_from_persistent_playlists(
        game_engine,
        database_engine,
        music_root,
        "songmaster-token",
        inspect_media=duration_inspector(
            {
                "Rock/A - Short.mp3": 10.0,
                "Rock/B - Long.mp3": 60.0,
            }
        ),
    )

    game = result.game
    assert game_engine.state is GameState.ROUND_COMPLETE
    assert game.deck is not None
    assert [entry.track.track_id for entry in game.deck] == [short_id, long_id]
    assert game.original_deck_size == 2
    assert game.deck_pointer == 1
    assert game.skipped_deck_entries == (
        SkippedDeckEntry(1, short_id, DeckSkipReason.DURATION_INELIGIBLE),
    )


def test_all_structurally_valid_tracks_too_short_preserve_automatic_finished(
    catalog_database: tuple[Engine, Path],
) -> None:
    database_engine, music_root = catalog_database
    first_id = import_catalog_track(database_engine, "Rock/A - Short.mp3")
    second_id = import_catalog_track(database_engine, "Rock/B - Shorter.mp3")
    playlist_id = create_catalog_playlist(database_engine, "All Short", [first_id, second_id])
    game_engine = make_game_engine(shuffle=lambda tracks: None)
    selected_lobby(game_engine, playlist_id)

    result = start_game_from_persistent_playlists(
        game_engine,
        database_engine,
        music_root,
        "songmaster-token",
        inspect_media=duration_inspector(
            {
                "Rock/A - Short.mp3": 10.0,
                "Rock/B - Shorter.mp3": 20.0,
            }
        ),
    )

    game = result.game
    assert game_engine.state is GameState.FINISHED
    assert game.original_deck_size == 2
    assert game.deck_exhausted is True
    assert game.end_reason is GameEndReason.DECK_EXHAUSTED
    assert game.ended_at == ENDED_AT
    assert len(game.skipped_deck_entries) == 2


def test_all_structurally_ineligible_tracks_reject_start_and_preserve_lobby(
    catalog_database: tuple[Engine, Path],
) -> None:
    database_engine, music_root = catalog_database
    track_id = import_catalog_track(database_engine, "Rock/A - Disabled.mp3")
    with database_engine.begin() as connection:
        connection.execute(
            text("UPDATE tracks SET enabled = 0 WHERE track_id = :track_id"),
            {"track_id": track_id},
        )
    playlist_id = create_catalog_playlist(database_engine, "Disabled", [track_id])
    game_engine = make_game_engine()
    lobby = selected_lobby(game_engine, playlist_id)
    before = deepcopy(lobby)

    def unexpected_inspection(*args: object) -> NoReturn:
        raise AssertionError("disabled tracks must not be inspected")

    with pytest.raises(NoStructurallyEligibleTracksError) as raised:
        start_game_from_persistent_playlists(
            game_engine,
            database_engine,
            music_root,
            "songmaster-token",
            inspect_media=unexpected_inspection,
        )

    assert isinstance(raised.value, EmptyDeckError)
    assert raised.value.resolution.resolved_playlists[0].tracks == ()
    assert raised.value.resolution.exclusions[0].track_id == track_id
    assert raised.value.resolution.exclusions[0].reason is (
        TrackExclusionReason.CATALOG_DISABLED
    )
    assert game_engine.state is GameState.LOBBY
    assert game_engine.active_game is lobby
    assert lobby == before


def test_missing_persistent_selection_rejects_start_and_preserves_lobby(
    catalog_database: tuple[Engine, Path],
) -> None:
    database_engine, music_root = catalog_database
    game_engine = make_game_engine()
    lobby = selected_lobby(game_engine, "deleted-playlist")
    before = deepcopy(lobby)

    with pytest.raises(SelectedPlaylistNotFoundError):
        start_game_from_persistent_playlists(
            game_engine,
            database_engine,
            music_root,
            "songmaster-token",
        )

    assert game_engine.state is GameState.LOBBY
    assert lobby == before


def test_unauthorized_start_is_rejected_before_media_inspection(
    catalog_database: tuple[Engine, Path],
) -> None:
    database_engine, music_root = catalog_database
    track_id = import_catalog_track(database_engine, "Rock/A - Song.mp3")
    playlist_id = create_catalog_playlist(database_engine, "Rock", [track_id])
    game_engine = make_game_engine()
    lobby = selected_lobby(game_engine, playlist_id)
    game_engine.join_game("player-token", "Player")
    before = deepcopy(lobby)

    def unexpected_inspection(*args: object) -> NoReturn:
        raise AssertionError("unauthorized requests must not inspect media")

    with pytest.raises(UnauthorizedStartGameError):
        start_game_from_persistent_playlists(
            game_engine,
            database_engine,
            music_root,
            "player-token",
            inspect_media=unexpected_inspection,
        )

    assert game_engine.state is GameState.LOBBY
    assert lobby == before


def test_wrong_state_start_is_rejected_before_root_or_media_access(
    catalog_database: tuple[Engine, Path],
) -> None:
    database_engine, _ = catalog_database
    game_engine = make_game_engine()

    with pytest.raises(StartGameNotAllowedError):
        start_game_from_persistent_playlists(
            game_engine,
            database_engine,
            Path("relative-root-that-must-not-be-checked"),
            "songmaster-token",
        )
