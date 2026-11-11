import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import Engine

from who_sings_that.application import (
    DEFAULT_PREPARATION_TIMEOUT_SECONDS,
    AcknowledgeAudioPreparationFailureRequest,
    AudioPreparationFailedEvent,
    AudioPreparationReadyEvent,
    AudioPreparationTimeoutEvent,
    ControllerFailedError,
    GameController,
    PlaySongRequest,
    SystemEvent,
)
from who_sings_that.database import (
    create_sqlite_engine,
    upgrade_database,
)
from who_sings_that.domain import (
    GameEngine,
    GameState,
    PreparationEventInapplicableError,
    PreparationFailureAcknowledgementNotAllowedError,
    ResolvedPlaylist,
    SelectedPlaylistSnapshot,
    TrackReference,
)

GAME_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
PREPARATION_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
ROUND_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
CREATED_AT = datetime(2026, 11, 10, 10, 0, tzinfo=UTC)
STARTED_AT = datetime(2026, 11, 10, 10, 1, tzinfo=UTC)
PLAYBACK_STARTED_AT = datetime(2026, 11, 10, 10, 2, tzinfo=UTC)
ENDED_AT = datetime(2026, 11, 10, 10, 3, tzinfo=UTC)
PLAYLIST_CREATED_AT = datetime(2025, 1, 2, 3, 4, tzinfo=UTC)


class SequenceClock:
    def __init__(self, *values: datetime) -> None:
        self._values = iter(values)

    def __call__(self) -> datetime:
        return next(self._values)


class ControlledSleep:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


class ObservingController(GameController):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.preparation_events_processed = 0
        self.preparation_event_processed = asyncio.Event()
        self.dispatch_order: list[str] = []

    def _dispatch(self, message: object) -> object:
        is_preparation_event = isinstance(
            message,
            (
                AudioPreparationReadyEvent,
                AudioPreparationFailedEvent,
                AudioPreparationTimeoutEvent,
            ),
        )
        if is_preparation_event:
            self.dispatch_order.append(type(message).__name__)
        try:
            return super()._dispatch(message)  # type: ignore[arg-type]
        finally:
            if is_preparation_event:
                self.preparation_events_processed += 1
                self.preparation_event_processed.set()


class UnknownSystemEvent(SystemEvent):
    pass


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


def prepared_engine(
    *tracks: TrackReference,
    audio_targets: tuple[str, ...] = ("songmaster-token",),
) -> GameEngine:
    engine = GameEngine(
        clock=SequenceClock(CREATED_AT, STARTED_AT, PLAYBACK_STARTED_AT, ENDED_AT),
        game_id_factory=lambda: GAME_ID,
        preparation_id_factory=lambda: PREPARATION_ID,
        round_id_factory=lambda: ROUND_ID,
        shuffle=lambda tracks: None,
    )
    engine.create_game("songmaster-token", "Songmaster")
    for index, token in enumerate(audio_targets):
        if token == "songmaster-token":
            continue
        engine.join_game(token, f"Player {index}")
        engine.set_audio_preference(token, True)
    for token in audio_targets:
        engine.set_browser_audio_capability(token, True)
    engine.select_playlist("songmaster-token", "playlist")
    engine.start_game("songmaster-token", [resolved_playlist(*tracks)])
    return engine


def make_controller(
    controller_storage: tuple[Engine, Path],
    engine: GameEngine,
    controlled_sleep: ControlledSleep,
) -> ObservingController:
    database_engine, music_root = controller_storage
    controller = ObservingController(
        database_engine,
        music_root,
        game_engine=engine,
        preparation_timeout_sleep=controlled_sleep,
    )
    game = engine.active_game
    assert game is not None
    for token in game.members:
        connection_id = (
            "songmaster-connection"
            if token == "songmaster-token"
            else "player-connection"
        )
        controller.connection_registry.register(GAME_ID, token, connection_id)
    return controller


async def play_song(controller: GameController):
    return await controller.submit_request(
        PlaySongRequest(GAME_ID, "songmaster-token")
    )


@pytest.mark.asyncio
async def test_play_song_schedules_default_timeout_without_blocking_queue(
    controller_storage: tuple[Engine, Path],
) -> None:
    sleep = ControlledSleep()
    engine = prepared_engine(
        TrackReference(1, 180),
        audio_targets=("songmaster-token", "player-token"),
    )
    controller = make_controller(controller_storage, engine, sleep)
    await controller.start()

    pending = await play_song(controller)
    await sleep.started.wait()
    controller.post_system_event(
        AudioPreparationReadyEvent(
            GAME_ID,
            pending.preparation_id,
            "songmaster-token",
            "songmaster-connection",
        )
    )
    await controller.preparation_event_processed.wait()

    assert sleep.delays == [DEFAULT_PREPARATION_TIMEOUT_SECONDS]
    assert controller.preparation_timeout_active is True
    assert engine.state is GameState.AUDIO_PREPARING
    assert engine.active_game is not None
    assert engine.active_game.audio_preparation_responses is not None
    await controller.stop()


@pytest.mark.asyncio
async def test_ready_and_failed_events_preserve_fifo_and_complete_e_immediately(
    controller_storage: tuple[Engine, Path],
) -> None:
    sleep = ControlledSleep()
    engine = prepared_engine(
        TrackReference(1, 180),
        audio_targets=("songmaster-token", "player-token"),
    )
    controller = make_controller(controller_storage, engine, sleep)
    await controller.start()
    pending = await play_song(controller)
    await sleep.started.wait()

    controller.post_system_event(
        AudioPreparationReadyEvent(
            GAME_ID,
            pending.preparation_id,
            "songmaster-token",
            "songmaster-connection",
        )
    )
    controller.post_system_event(
        AudioPreparationFailedEvent(
            GAME_ID,
            pending.preparation_id,
            "player-token",
            "player-connection",
        )
    )
    while controller.preparation_events_processed < 2:
        controller.preparation_event_processed.clear()
        await controller.preparation_event_processed.wait()

    assert controller.dispatch_order == [
        "AudioPreparationReadyEvent",
        "AudioPreparationFailedEvent",
    ]
    assert engine.state is GameState.PLAYING
    assert engine.active_game is not None
    assert engine.active_game.active_round is not None
    assert engine.active_game.active_round.playback_clients == frozenset(
        {"songmaster-token"}
    )
    assert controller.preparation_timeout_active is False
    await sleep.cancelled.wait()
    await controller.stop()


@pytest.mark.asyncio
async def test_timer_expiry_posts_timeout_through_fifo_and_enters_playing(
    controller_storage: tuple[Engine, Path],
) -> None:
    sleep = ControlledSleep()
    engine = prepared_engine(
        TrackReference(1, 180),
        audio_targets=("songmaster-token", "player-token"),
    )
    controller = make_controller(controller_storage, engine, sleep)
    await controller.start()
    pending = await play_song(controller)
    await sleep.started.wait()
    controller.post_system_event(
        AudioPreparationReadyEvent(
            GAME_ID,
            pending.preparation_id,
            "songmaster-token",
            "songmaster-connection",
        )
    )
    await controller.preparation_event_processed.wait()
    controller.preparation_event_processed.clear()

    sleep.release.set()
    await controller.preparation_event_processed.wait()

    assert controller.dispatch_order == [
        "AudioPreparationReadyEvent",
        "AudioPreparationTimeoutEvent",
    ]
    assert engine.state is GameState.PLAYING
    assert engine.active_game is not None
    assert engine.active_game.active_round is not None
    assert engine.active_game.active_round.playback_clients == frozenset(
        {"songmaster-token"}
    )
    await controller.stop()


@pytest.mark.asyncio
async def test_zero_ready_timeout_then_acknowledgement_request_takes_d(
    controller_storage: tuple[Engine, Path],
) -> None:
    sleep = ControlledSleep()
    engine = prepared_engine(
        TrackReference(1, 180),
        TrackReference(2, 180),
    )
    controller = make_controller(controller_storage, engine, sleep)
    await controller.start()
    pending = await play_song(controller)
    await sleep.started.wait()

    sleep.release.set()
    await controller.preparation_event_processed.wait()
    assert engine.state is GameState.AUDIO_PREPARING
    assert engine.active_game is not None
    assert engine.active_game.deck_pointer == 0

    result = await controller.submit_request(
        AcknowledgeAudioPreparationFailureRequest(
            GAME_ID,
            pending.preparation_id,
            "songmaster-token",
        )
    )

    assert result is engine.active_game
    assert engine.state is GameState.ROUND_COMPLETE
    assert result.deck_pointer == 1
    assert result.active_round is None
    assert controller.preparation_timeout_active is False
    await controller.stop()


@pytest.mark.asyncio
async def test_all_failed_completion_retires_timer_before_acknowledgement(
    controller_storage: tuple[Engine, Path],
) -> None:
    sleep = ControlledSleep()
    engine = prepared_engine(TrackReference(1, 180), TrackReference(2, 180))
    controller = make_controller(controller_storage, engine, sleep)
    await controller.start()
    pending = await play_song(controller)
    await sleep.started.wait()

    controller.post_system_event(
        AudioPreparationFailedEvent(
            GAME_ID,
            pending.preparation_id,
            "songmaster-token",
            "songmaster-connection",
        )
    )
    await controller.preparation_event_processed.wait()
    await sleep.cancelled.wait()

    assert engine.state is GameState.AUDIO_PREPARING
    assert controller.preparation_timeout_active is False
    await controller.stop()


@pytest.mark.asyncio
async def test_stale_timeout_after_failure_is_rejected_without_resurrection(
    controller_storage: tuple[Engine, Path],
) -> None:
    sleep = ControlledSleep()
    engine = prepared_engine(TrackReference(1, 180), TrackReference(2, 180))
    controller = make_controller(controller_storage, engine, sleep)
    await controller.start()
    pending = await play_song(controller)
    controller.post_system_event(
        AudioPreparationFailedEvent(
            GAME_ID,
            pending.preparation_id,
            "songmaster-token",
            "songmaster-connection",
        )
    )
    await controller.preparation_event_processed.wait()
    controller.preparation_event_processed.clear()

    controller.post_system_event(
        AudioPreparationTimeoutEvent(GAME_ID, pending.preparation_id)
    )
    await controller.preparation_event_processed.wait()

    assert engine.state is GameState.AUDIO_PREPARING
    assert engine.active_game is not None
    assert engine.active_game.active_round is None
    assert engine.active_game.audio_preparation_responses is not None
    assert (
        engine.active_game.audio_preparation_responses.failure_acknowledgement_pending
        is True
    )
    assert any(
        isinstance(rejection.error, PreparationEventInapplicableError)
        for rejection in controller.system_event_rejections
    )
    await controller.stop()


@pytest.mark.asyncio
async def test_stale_timeout_after_success_is_expected_and_has_no_effect(
    controller_storage: tuple[Engine, Path],
) -> None:
    sleep = ControlledSleep()
    engine = prepared_engine(TrackReference(1, 180))
    controller = make_controller(controller_storage, engine, sleep)
    await controller.start()
    pending = await play_song(controller)
    controller.post_system_event(
        AudioPreparationReadyEvent(
            GAME_ID,
            pending.preparation_id,
            "songmaster-token",
            "songmaster-connection",
        )
    )
    await controller.preparation_event_processed.wait()
    assert engine.active_game is not None
    round_before = engine.active_game.active_round
    controller.preparation_event_processed.clear()

    controller.post_system_event(
        AudioPreparationTimeoutEvent(GAME_ID, pending.preparation_id)
    )
    with pytest.raises(PreparationFailureAcknowledgementNotAllowedError):
        await controller.submit_request(
            AcknowledgeAudioPreparationFailureRequest(
                GAME_ID,
                pending.preparation_id,
                "songmaster-token",
            )
        )

    assert engine.state is GameState.PLAYING
    assert engine.active_game.active_round is round_before
    assert any(
        isinstance(rejection.error, PreparationEventInapplicableError)
        for rejection in controller.system_event_rejections
    )
    assert controller.is_running is True
    await controller.stop()


@pytest.mark.asyncio
async def test_controller_shutdown_cancels_preparation_timer(
    controller_storage: tuple[Engine, Path],
) -> None:
    sleep = ControlledSleep()
    engine = prepared_engine(TrackReference(1, 180))
    controller = make_controller(controller_storage, engine, sleep)
    await controller.start()
    await play_song(controller)
    await sleep.started.wait()

    await controller.stop()

    assert controller.preparation_timeout_active is False
    assert sleep.cancelled.is_set()


@pytest.mark.asyncio
async def test_fatal_controller_failure_cancels_preparation_timer(
    controller_storage: tuple[Engine, Path],
) -> None:
    sleep = ControlledSleep()
    engine = prepared_engine(TrackReference(1, 180))
    controller = make_controller(controller_storage, engine, sleep)
    await controller.start()
    await play_song(controller)
    await sleep.started.wait()

    controller.post_system_event(UnknownSystemEvent())
    with pytest.raises(ControllerFailedError):
        await controller.submit_request(
            AcknowledgeAudioPreparationFailureRequest(
                GAME_ID,
                PREPARATION_ID,
                "songmaster-token",
            )
        )
    await sleep.cancelled.wait()

    assert controller.preparation_timeout_active is False
    with pytest.raises(ControllerFailedError):
        await controller.stop()
