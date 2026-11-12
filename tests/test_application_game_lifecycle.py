import asyncio
from collections.abc import Iterator
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import Engine

from who_sings_that.application import (
    AudioPreparationReadyEvent,
    AudioPreparationTimeoutEvent,
    AudioStopReason,
    BuzzRequest,
    CancelGameRequest,
    CloseGameRequest,
    CreateGameRequest,
    EndGameRequest,
    GameController,
    JoinGameRequest,
    PlaybackTimeoutEvent,
    PlaySongRequest,
    SelectPlaylistRequest,
    StaleGameSessionError,
    StartGameRequest,
    StopAudioCommand,
)
from who_sings_that.catalog import (
    MediaStatus,
    MetadataSource,
    MetadataStatus,
    TrackMetadata,
)
from who_sings_that.database import (
    add_tracks_to_playlist,
    create_playlist,
    create_sqlite_engine,
    import_tracks,
    upgrade_database,
)
from who_sings_that.domain import (
    BuzzNotAllowedError,
    CloseGameNotAllowedError,
    GameEndReason,
    GameEngine,
    GameFinalizationResult,
    GameSettings,
    GameState,
    PlaybackTimeoutInapplicableError,
    PreparationEventInapplicableError,
    ResolvedPlaylist,
    RoundTerminationReason,
    SelectedPlaylistSnapshot,
    TrackReference,
    UnauthorizedCancelGameError,
    UnauthorizedEndGameError,
)

GAME_IDS = (UUID(int=1), UUID(int=2), UUID(int=3))
PLAYLIST_CREATED_AT = datetime(2025, 1, 2, 3, 4, tzinfo=UTC)
TRACKS = (
    TrackReference(10, 180, "First Artist", "First Title"),
    TrackReference(20, 180, "Second Artist", "Second Title"),
)


class AdvancingClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 11, 11, 10, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        result = self.value
        self.value += timedelta(minutes=1)
        return result


class MutableMonotonicClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


class UUIDFactory:
    def __init__(self, values: tuple[UUID, ...]) -> None:
        self._values = iter(values)

    def __call__(self) -> UUID:
        return next(self._values)


@pytest.fixture
def controller_storage(tmp_path: Path) -> Iterator[tuple[Engine, Path]]:
    database_path = tmp_path / "controller.sqlite3"
    upgrade_database(database_path)
    database_engine = create_sqlite_engine(database_path)
    music_root = tmp_path / "music"
    music_root.mkdir()
    yield database_engine, music_root
    database_engine.dispose()


def resolved_playlist(*tracks: TrackReference) -> ResolvedPlaylist:
    return ResolvedPlaylist(
        snapshot=SelectedPlaylistSnapshot(
            playlist_id="playlist",
            playlist_name="Playlist",
            playlist_created_at=PLAYLIST_CREATED_AT,
            playlist_song_count=len(tracks),
        ),
        tracks=tracks,
    )


def make_engine() -> tuple[GameEngine, MutableMonotonicClock]:
    monotonic_clock = MutableMonotonicClock()
    engine = GameEngine(
        GameSettings(playback_duration_seconds=30),
        clock=AdvancingClock(),
        monotonic_clock=monotonic_clock,
        game_id_factory=UUIDFactory(GAME_IDS),
        preparation_id_factory=UUIDFactory(
            (UUID(int=100), UUID(int=101), UUID(int=102))
        ),
        round_id_factory=UUIDFactory((UUID(int=200), UUID(int=201), UUID(int=202))),
        shuffle=lambda values: None,
    )
    return engine, monotonic_clock


def started_engine(*tracks: TrackReference):
    engine, monotonic_clock = make_engine()
    game = engine.create_game("songmaster-token", "Songmaster")
    engine.join_game("player-token", "Player")
    engine.set_browser_audio_capability("songmaster-token", True)
    engine.select_playlist("songmaster-token", "playlist")
    engine.start_game("songmaster-token", [resolved_playlist(*tracks)])
    return engine, game, monotonic_clock


def make_controller(
    storage: tuple[Engine, Path],
    engine: GameEngine,
    *,
    inspect_media=None,
) -> GameController:
    database_engine, music_root = storage
    kwargs = {} if inspect_media is None else {"inspect_media": inspect_media}
    controller = GameController(
        database_engine,
        music_root,
        game_engine=engine,
        **kwargs,
    )
    game = engine.active_game
    if game is not None:
        for token in game.members:
            connection_id = (
                "connection-1" if token == "songmaster-token" else "player-connection"
            )
            controller.connection_registry.register(
                game.game_session_id, token, connection_id
            )
    return controller


def test_lifecycle_requests_have_only_identity_and_authorization_fields() -> None:
    expected = ["game_session_id", "requesting_browser_token"]
    assert [field.name for field in fields(CancelGameRequest)] == expected
    assert [field.name for field in fields(EndGameRequest)] == expected
    assert [field.name for field in fields(CloseGameRequest)] == expected


@pytest.mark.asyncio
async def test_controller_cancel_rejections_and_success_keep_consumer_running(
    controller_storage: tuple[Engine, Path],
) -> None:
    engine, _ = make_engine()
    controller = make_controller(controller_storage, engine)
    await controller.start()
    game = await controller.submit_request(
        CreateGameRequest("songmaster-token", "Songmaster", "connection-1")
    )
    await controller.submit_request(
        JoinGameRequest("player-token", "player-connection", "Player")
    )

    with pytest.raises(StaleGameSessionError):
        await controller.submit_request(
            CancelGameRequest(UUID(int=999), "songmaster-token")
        )
    with pytest.raises(UnauthorizedCancelGameError):
        await controller.submit_request(CancelGameRequest(GAME_IDS[0], "player-token"))

    result = await controller.submit_request(
        CancelGameRequest(GAME_IDS[0], "songmaster-token")
    )

    assert result is None
    assert engine.state is GameState.NO_GAME
    assert engine.active_game is None
    assert game.final_standings == ()
    assert controller.is_running is True
    fresh = await controller.submit_request(
        CreateGameRequest("songmaster-token", "Fresh Name", "connection-2")
    )
    assert fresh.game_session_id == GAME_IDS[1]
    assert fresh.songmaster.display_name == "Fresh Name"
    await controller.stop()


@pytest.mark.asyncio
async def test_end_game_from_audio_preparing_retires_timer_and_late_event_is_harmless(
    controller_storage: tuple[Engine, Path],
) -> None:
    engine, game, _ = started_engine(*TRACKS)
    controller = make_controller(controller_storage, engine)
    await controller.start()
    pending = await controller.submit_request(
        PlaySongRequest(GAME_IDS[0], "songmaster-token")
    )
    assert controller.preparation_timeout_active is True

    finalization = await controller.submit_request(
        EndGameRequest(GAME_IDS[0], "songmaster-token")
    )

    assert isinstance(finalization, GameFinalizationResult)
    assert engine.state is GameState.FINISHED
    assert game.last_voided_round is None
    assert game.pending_audio_preparation is None
    assert controller.preparation_timeout_active is False
    controller.post_system_event(
        AudioPreparationReadyEvent(
            GAME_IDS[0],
            pending.preparation_id,
            "songmaster-token",
            "connection-1",
        )
    )
    await controller._queue.join()
    assert isinstance(
        controller.system_event_rejections[-1].error,
        PreparationEventInapplicableError,
    )
    assert engine.state is GameState.FINISHED
    assert controller.is_running is True
    await controller.stop()


@pytest.mark.asyncio
async def test_end_game_from_playing_retires_timer_before_late_events(
    controller_storage: tuple[Engine, Path],
) -> None:
    engine, game, _ = started_engine(*TRACKS)
    controller = make_controller(controller_storage, engine)
    await controller.start()
    pending = await controller.submit_request(
        PlaySongRequest(GAME_IDS[0], "songmaster-token")
    )
    controller.post_system_event(
        AudioPreparationReadyEvent(
            GAME_IDS[0],
            pending.preparation_id,
            "songmaster-token",
            "connection-1",
        )
    )
    await controller._queue.join()
    assert game.active_round is not None
    active_round = game.active_round
    assert controller.playback_timeout_active is True
    controls = []
    controller.add_audio_control_listener(controls.append)

    finalization_task = asyncio.create_task(
        controller.submit_request(EndGameRequest(GAME_IDS[0], "songmaster-token"))
    )
    await asyncio.sleep(0)
    controller.post_system_event(
        PlaybackTimeoutEvent(GAME_IDS[0], active_round.round_id)
    )
    await finalization_task
    await controller._queue.join()

    assert engine.state is GameState.FINISHED
    assert game.last_voided_round is not None
    assert game.last_voided_round.termination is None
    assert controller.playback_timeout_active is False
    assert [
        command.reason for command in controls if isinstance(command, StopAudioCommand)
    ] == [AudioStopReason.GAME_TERMINATION]
    assert isinstance(
        controller.system_event_rejections[-1].error,
        PlaybackTimeoutInapplicableError,
    )
    with pytest.raises(BuzzNotAllowedError):
        await controller.submit_request(
            BuzzRequest(
                GAME_IDS[0],
                active_round.round_id,
                "player-token",
                "player-connection",
            )
        )
    assert controller.is_running is True
    await controller.stop()


@pytest.mark.asyncio
async def test_fifo_buzz_before_end_is_retained_in_void_record(
    controller_storage: tuple[Engine, Path],
) -> None:
    engine, game, monotonic_clock = started_engine(*TRACKS)
    controller = make_controller(controller_storage, engine)
    await controller.start()
    pending = await controller.submit_request(
        PlaySongRequest(GAME_IDS[0], "songmaster-token")
    )
    controller.post_system_event(
        AudioPreparationReadyEvent(
            GAME_IDS[0],
            pending.preparation_id,
            "songmaster-token",
            "connection-1",
        )
    )
    await controller._queue.join()
    assert game.active_round is not None
    active_round = game.active_round
    monotonic_clock.value = active_round.playback_started_monotonic + 5

    buzz_task = asyncio.create_task(
        controller.submit_request(
            BuzzRequest(
                GAME_IDS[0],
                active_round.round_id,
                "player-token",
                "player-connection",
            )
        )
    )
    await asyncio.sleep(0)
    end_task = asyncio.create_task(
        controller.submit_request(EndGameRequest(GAME_IDS[0], "songmaster-token"))
    )
    termination, _ = await asyncio.gather(buzz_task, end_task)

    assert termination.reason is RoundTerminationReason.BUZZ
    assert game.last_voided_round is not None
    assert game.last_voided_round.termination is termination
    assert controller.playback_timeout_active is False
    await controller.stop()


def install_temporary_playlist(database_engine: Engine, music_root: Path) -> str:
    relative_path = Path("Synthetic/Artist - Song.mp3")
    absolute_path = music_root / relative_path
    absolute_path.parent.mkdir(parents=True)
    absolute_path.write_bytes(b"synthetic test media")
    (track,) = import_tracks(
        database_engine,
        [
            TrackMetadata(
                relative_path=relative_path,
                artist="Artist",
                title="Song",
                duration_seconds=180.0,
                file_size_bytes=20,
                metadata_source=MetadataSource.FILENAME,
                metadata_status=MetadataStatus.COMPLETE,
                media_status=MediaStatus.VALID,
            )
        ],
    )
    playlist = create_playlist(database_engine, "Synthetic Lifecycle")
    add_tracks_to_playlist(database_engine, playlist.playlist_id, [track.track_id])
    return playlist.playlist_id


def synthetic_inspector(
    _: str | Path,
    relative_path: str | Path,
) -> TrackMetadata:
    return TrackMetadata(
        relative_path=Path(relative_path),
        artist="Artist",
        title="Song",
        duration_seconds=180.0,
        file_size_bytes=20,
        metadata_source=MetadataSource.FILENAME,
        metadata_status=MetadataStatus.COMPLETE,
        media_status=MediaStatus.VALID,
    )


@pytest.mark.asyncio
async def test_controller_create_start_end_close_and_create_fresh_game(
    controller_storage: tuple[Engine, Path],
) -> None:
    database_engine, music_root = controller_storage
    playlist_id = install_temporary_playlist(database_engine, music_root)
    engine, _ = make_engine()
    controller = make_controller(
        controller_storage,
        engine,
        inspect_media=synthetic_inspector,
    )
    await controller.start()

    first = await controller.submit_request(
        CreateGameRequest("songmaster-token", "First Name", "connection-1")
    )
    await controller.submit_request(
        SelectPlaylistRequest(
            first.game_session_id,
            "songmaster-token",
            playlist_id,
        )
    )
    await controller.submit_request(
        StartGameRequest(first.game_session_id, "songmaster-token")
    )
    finalization = await controller.submit_request(
        EndGameRequest(first.game_session_id, "songmaster-token")
    )

    assert finalization.end_reason is GameEndReason.SONGMASTER_ENDED_GAME
    assert engine.state is GameState.FINISHED
    assert first.final_standings[0].display_name == "First Name"

    close_result = await controller.submit_request(
        CloseGameRequest(first.game_session_id, "songmaster-token")
    )
    assert close_result is None
    assert engine.state is GameState.NO_GAME
    assert engine.active_game is None
    assert controller.preparation_timeout_active is False
    assert controller.playback_timeout_active is False
    assert not controller.connection_registry.is_current(
        first.game_session_id, "songmaster-token", "connection-1"
    )

    second = await controller.submit_request(
        CreateGameRequest("songmaster-token", "Second Name", "connection-2")
    )
    controller.post_system_event(
        AudioPreparationTimeoutEvent(first.game_session_id, UUID(int=999))
    )
    await controller._queue.join()

    assert engine.state is GameState.LOBBY
    assert engine.active_game is second
    assert second.game_session_id != first.game_session_id
    assert second.songmaster.display_name == "Second Name"
    assert second.songmaster.committed_score == 0
    assert controller.connection_registry.is_current(
        second.game_session_id, "songmaster-token", "connection-2"
    )
    assert second.selected_playlist_ids == set()
    assert second.deck is None
    assert isinstance(
        controller.system_event_rejections[-1].error,
        PreparationEventInapplicableError,
    )
    assert controller.preparation_timeout_active is False
    assert controller.playback_timeout_active is False
    assert controller.is_running is True

    with pytest.raises(StaleGameSessionError):
        await controller.submit_request(
            CloseGameRequest(first.game_session_id, "songmaster-token")
        )
    with pytest.raises(CloseGameNotAllowedError):
        await controller.submit_request(
            CloseGameRequest(second.game_session_id, "songmaster-token")
        )
    assert controller.is_running is True
    await controller.stop()


@pytest.mark.asyncio
async def test_controller_end_game_rejection_leaves_consumer_operational(
    controller_storage: tuple[Engine, Path],
) -> None:
    engine, _, _ = started_engine(*TRACKS)
    controller = make_controller(controller_storage, engine)
    await controller.start()

    with pytest.raises(UnauthorizedEndGameError):
        await controller.submit_request(EndGameRequest(GAME_IDS[0], "player-token"))

    result = await controller.submit_request(
        EndGameRequest(GAME_IDS[0], "songmaster-token")
    )
    assert result.end_reason is GameEndReason.SONGMASTER_ENDED_GAME
    assert controller.is_running is True
    await controller.stop()
