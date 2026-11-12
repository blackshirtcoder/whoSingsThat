import asyncio
from collections.abc import Iterator
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import Engine

from who_sings_that.application import (
    AudioPreparationReadyEvent,
    AudioStopReason,
    BuzzRequest,
    ControllerFailedError,
    GameController,
    PlaybackTimeoutEvent,
    PlaySongRequest,
    StopAudioCommand,
    SystemEvent,
)
from who_sings_that.database import create_sqlite_engine, upgrade_database
from who_sings_that.domain import (
    BuzzNotAllowedError,
    GameEngine,
    GameSettings,
    GameState,
    PlaybackTimeoutInapplicableError,
    ResolvedPlaylist,
    RoundTerminationReason,
    SelectedPlaylistSnapshot,
    TrackReference,
    UnknownGameMemberError,
)

GAME_ID = UUID("aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa")
PREPARATION_ID = UUID("bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb")
ROUND_ID = UUID("cccccccc-3333-4333-8333-cccccccccccc")
CREATED_AT = datetime(2026, 11, 10, 10, 0, tzinfo=UTC)
STARTED_AT = datetime(2026, 11, 10, 10, 1, tzinfo=UTC)
PLAYBACK_STARTED_AT = datetime(2026, 11, 10, 10, 2, tzinfo=UTC)
PLAYLIST_CREATED_AT = datetime(2025, 1, 2, 3, 4, tzinfo=UTC)


class SequenceClock:
    def __init__(self) -> None:
        self._values = iter((CREATED_AT, STARTED_AT, PLAYBACK_STARTED_AT))

    def __call__(self) -> datetime:
        return next(self._values)


class MutableMonotonicClock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


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


class TrackingQueue(asyncio.Queue[object]):
    def __init__(self) -> None:
        super().__init__()
        self.put_count = 0
        self.put_observed = asyncio.Event()

    def put_nowait(self, item: object) -> None:
        super().put_nowait(item)
        self.put_count += 1
        self.put_observed.set()

    async def wait_for_put_count(self, expected: int) -> None:
        while self.put_count < expected:
            self.put_observed.clear()
            if self.put_count < expected:
                await self.put_observed.wait()


class GatedController(GameController):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.release_consumer = asyncio.Event()
        self.tracking_queue = TrackingQueue()
        self._queue = self.tracking_queue  # type: ignore[assignment]

    async def _consume_messages(self) -> None:
        assert self._consumer_ready is not None
        self._consumer_ready.set()
        await self.release_consumer.wait()
        await super()._consume_messages()


class ObservingController(GameController):
    def __init__(
        self,
        *args: object,
        scheduling_clock_advance: float = 0.0,
        monotonic_clock: MutableMonotonicClock,
        **kwargs: object,
    ) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.monotonic_clock = monotonic_clock
        self.scheduling_clock_advance = scheduling_clock_advance
        self.ready_processed = asyncio.Event()
        self.playback_timeout_processed = asyncio.Event()

    def _schedule_playback_timeout(
        self,
        game_session_id: UUID,
        round_id: UUID,
        deadline_monotonic: float,
    ) -> None:
        self.monotonic_clock.value += self.scheduling_clock_advance
        super()._schedule_playback_timeout(
            game_session_id,
            round_id,
            deadline_monotonic,
        )

    def _dispatch(self, message: object) -> object:
        try:
            return super()._dispatch(message)  # type: ignore[arg-type]
        finally:
            if isinstance(message, AudioPreparationReadyEvent):
                self.ready_processed.set()
            if isinstance(message, PlaybackTimeoutEvent):
                self.playback_timeout_processed.set()


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


def resolved_playlist() -> ResolvedPlaylist:
    return ResolvedPlaylist(
        snapshot=SelectedPlaylistSnapshot(
            playlist_id="playlist",
            playlist_name="Playlist",
            playlist_created_at=PLAYLIST_CREATED_AT,
            playlist_song_count=1,
        ),
        tracks=(TrackReference(10, 180),),
    )


def round_complete_engine(
    monotonic_clock: MutableMonotonicClock,
) -> GameEngine:
    engine = GameEngine(
        GameSettings(playback_duration_seconds=30),
        clock=SequenceClock(),
        monotonic_clock=monotonic_clock,
        game_id_factory=lambda: GAME_ID,
        preparation_id_factory=lambda: PREPARATION_ID,
        round_id_factory=lambda: ROUND_ID,
        shuffle=lambda tracks: None,
    )
    engine.create_game("songmaster-token", "Songmaster")
    engine.join_game("player-token", "Player")
    engine.set_browser_audio_capability("songmaster-token", True)
    engine.select_playlist("songmaster-token", "playlist")
    engine.start_game("songmaster-token", [resolved_playlist()])
    return engine


def playing_engine(monotonic_clock: MutableMonotonicClock) -> GameEngine:
    engine = round_complete_engine(monotonic_clock)
    pending = engine.play_song("songmaster-token")
    engine.record_audio_preparation_ready(
        GAME_ID,
        pending.preparation_id,
        "songmaster-token",
    )
    return engine


def gated_controller(
    controller_storage: tuple[Engine, Path],
    engine: GameEngine,
) -> GatedController:
    database_engine, music_root = controller_storage
    controller = GatedController(database_engine, music_root, game_engine=engine)
    controller.connection_registry.register(
        GAME_ID, "songmaster-token", "songmaster-connection"
    )
    controller.connection_registry.register(
        GAME_ID, "player-token", "player-connection"
    )
    return controller


async def start_playback_through_controller(
    controller_storage: tuple[Engine, Path],
    *,
    scheduling_clock_advance: float = 0.0,
):
    monotonic_clock = MutableMonotonicClock(100.0)
    engine = round_complete_engine(monotonic_clock)
    preparation_sleep = ControlledSleep()
    playback_sleep = ControlledSleep()
    database_engine, music_root = controller_storage
    controller = ObservingController(
        database_engine,
        music_root,
        game_engine=engine,
        preparation_timeout_sleep=preparation_sleep,
        playback_timeout_sleep=playback_sleep,
        monotonic_clock=monotonic_clock,
        scheduling_clock_advance=scheduling_clock_advance,
    )
    controller.connection_registry.register(
        GAME_ID, "songmaster-token", "songmaster-connection"
    )
    controller.connection_registry.register(
        GAME_ID, "player-token", "player-connection"
    )
    await controller.start()
    pending = await controller.submit_request(
        PlaySongRequest(GAME_ID, "songmaster-token")
    )
    await preparation_sleep.started.wait()
    controller.post_system_event(
        AudioPreparationReadyEvent(
            GAME_ID,
            pending.preparation_id,
            "songmaster-token",
            "songmaster-connection",
        )
    )
    await controller.ready_processed.wait()
    await playback_sleep.started.wait()
    assert engine.active_game is not None
    assert engine.active_game.active_round is not None
    return (
        controller,
        engine,
        monotonic_clock,
        preparation_sleep,
        playback_sleep,
    )


def test_buzz_request_exposes_no_client_controlled_timing_field() -> None:
    assert [field.name for field in fields(BuzzRequest)] == [
        "game_session_id",
        "round_id",
        "requesting_browser_token",
        "source_connection_id",
    ]


@pytest.mark.asyncio
async def test_two_queued_buzzes_preserve_fifo_and_first_wins(
    controller_storage: tuple[Engine, Path],
) -> None:
    clock = MutableMonotonicClock(100.0)
    engine = playing_engine(clock)
    clock.value = 102.0
    controller = gated_controller(controller_storage, engine)
    await controller.start()

    first = asyncio.create_task(
        controller.submit_request(
            BuzzRequest(GAME_ID, ROUND_ID, "player-token", "player-connection")
        )
    )
    await controller.tracking_queue.wait_for_put_count(1)
    clock.value = 103.0
    second = asyncio.create_task(
        controller.submit_request(
            BuzzRequest(
                GAME_ID,
                ROUND_ID,
                "songmaster-token",
                "songmaster-connection",
            )
        )
    )
    await controller.tracking_queue.wait_for_put_count(2)
    controller.release_consumer.set()

    accepted = await first
    with pytest.raises(BuzzNotAllowedError):
        await second

    assert accepted.winning_browser_token == "player-token"
    assert engine.state is GameState.AWAITING_REVEAL
    assert controller.is_running is True
    await controller.stop()


@pytest.mark.asyncio
async def test_invalid_queued_buzz_does_not_block_later_valid_buzz(
    controller_storage: tuple[Engine, Path],
) -> None:
    clock = MutableMonotonicClock(100.0)
    engine = playing_engine(clock)
    clock.value = 102.0
    controller = gated_controller(controller_storage, engine)
    await controller.start()

    invalid = asyncio.create_task(
        controller.submit_request(
            BuzzRequest(GAME_ID, ROUND_ID, "unknown-token", "unknown-connection")
        )
    )
    await controller.tracking_queue.wait_for_put_count(1)
    valid = asyncio.create_task(
        controller.submit_request(
            BuzzRequest(GAME_ID, ROUND_ID, "player-token", "player-connection")
        )
    )
    await controller.tracking_queue.wait_for_put_count(2)
    controller.release_consumer.set()

    with pytest.raises(UnknownGameMemberError):
        await invalid
    accepted = await valid

    assert accepted.winning_browser_token == "player-token"
    assert controller.is_running is True
    await controller.stop()


@pytest.mark.asyncio
async def test_consumer_delay_does_not_inflate_trusted_elapsed_time(
    controller_storage: tuple[Engine, Path],
) -> None:
    clock = MutableMonotonicClock(100.0)
    engine = playing_engine(clock)
    clock.value = 102.3
    controller = gated_controller(controller_storage, engine)
    await controller.start()

    request = asyncio.create_task(
        controller.submit_request(
            BuzzRequest(GAME_ID, ROUND_ID, "player-token", "player-connection")
        )
    )
    await controller.tracking_queue.wait_for_put_count(1)
    clock.value = 129.9
    controller.release_consumer.set()

    accepted = await request

    assert accepted.buzz_elapsed_seconds == pytest.approx(2.3)
    await controller.stop()


@pytest.mark.asyncio
async def test_predeadline_buzz_queued_before_timeout_wins(
    controller_storage: tuple[Engine, Path],
) -> None:
    clock = MutableMonotonicClock(100.0)
    engine = playing_engine(clock)
    clock.value = 129.9
    controller = gated_controller(controller_storage, engine)
    await controller.start()

    request = asyncio.create_task(
        controller.submit_request(
            BuzzRequest(GAME_ID, ROUND_ID, "player-token", "player-connection")
        )
    )
    await controller.tracking_queue.wait_for_put_count(1)
    clock.value = 130.0
    controller.post_system_event(PlaybackTimeoutEvent(GAME_ID, ROUND_ID))
    await controller.tracking_queue.wait_for_put_count(2)
    controller.release_consumer.set()

    accepted = await request
    await controller.tracking_queue.join()

    assert accepted.reason is RoundTerminationReason.BUZZ
    assert engine.state is GameState.AWAITING_REVEAL
    assert any(
        isinstance(rejection.error, PlaybackTimeoutInapplicableError)
        for rejection in controller.system_event_rejections
    )
    await controller.stop()


@pytest.mark.asyncio
async def test_postdeadline_buzz_queued_before_timeout_is_rejected_then_g_wins(
    controller_storage: tuple[Engine, Path],
) -> None:
    clock = MutableMonotonicClock(100.0)
    engine = playing_engine(clock)
    clock.value = 130.0
    controller = gated_controller(controller_storage, engine)
    await controller.start()

    request = asyncio.create_task(
        controller.submit_request(
            BuzzRequest(GAME_ID, ROUND_ID, "player-token", "player-connection")
        )
    )
    await controller.tracking_queue.wait_for_put_count(1)
    controller.post_system_event(PlaybackTimeoutEvent(GAME_ID, ROUND_ID))
    await controller.tracking_queue.wait_for_put_count(2)
    controller.release_consumer.set()

    with pytest.raises(BuzzNotAllowedError):
        await request
    await controller.tracking_queue.join()

    assert engine.state is GameState.AWAITING_REVEAL
    assert engine.active_game is not None
    assert engine.active_game.round_termination is not None
    assert engine.active_game.round_termination.reason is (
        RoundTerminationReason.PLAYBACK_TIMEOUT
    )
    assert controller.is_running is True
    await controller.stop()


@pytest.mark.asyncio
async def test_timeout_processed_before_later_buzz_wins(
    controller_storage: tuple[Engine, Path],
) -> None:
    clock = MutableMonotonicClock(100.0)
    engine = playing_engine(clock)
    clock.value = 130.0
    controller = gated_controller(controller_storage, engine)
    await controller.start()

    controller.post_system_event(PlaybackTimeoutEvent(GAME_ID, ROUND_ID))
    request = asyncio.create_task(
        controller.submit_request(
            BuzzRequest(GAME_ID, ROUND_ID, "player-token", "player-connection")
        )
    )
    await controller.tracking_queue.wait_for_put_count(2)
    controller.release_consumer.set()

    with pytest.raises(BuzzNotAllowedError):
        await request

    assert engine.active_game is not None
    assert engine.active_game.round_termination is not None
    assert engine.active_game.round_termination.reason is (
        RoundTerminationReason.PLAYBACK_TIMEOUT
    )
    await controller.stop()


@pytest.mark.asyncio
async def test_transition_e_schedules_timer_from_authoritative_deadline(
    controller_storage: tuple[Engine, Path],
) -> None:
    (
        controller,
        engine,
        _,
        preparation_sleep,
        playback_sleep,
    ) = await start_playback_through_controller(controller_storage)

    assert preparation_sleep.cancelled.is_set()
    assert playback_sleep.delays == [30.0]
    assert controller.preparation_timeout_active is False
    assert controller.playback_timeout_active is True
    assert engine.state is GameState.PLAYING
    await controller.stop()


@pytest.mark.asyncio
async def test_delayed_timer_task_uses_remaining_authoritative_deadline(
    controller_storage: tuple[Engine, Path],
) -> None:
    controller, _, _, _, playback_sleep = await start_playback_through_controller(
        controller_storage,
        scheduling_clock_advance=5.0,
    )

    assert playback_sleep.delays == [25.0]
    await controller.stop()


@pytest.mark.asyncio
async def test_timer_expiry_posts_fifo_event_and_takes_transition_g(
    controller_storage: tuple[Engine, Path],
) -> None:
    (
        controller,
        engine,
        clock,
        _,
        playback_sleep,
    ) = await start_playback_through_controller(controller_storage)
    assert controller.latest_projection is not None
    playing_revision = controller.latest_projection.revision
    clock.value = 130.0

    playback_sleep.release.set()
    await controller.playback_timeout_processed.wait()

    assert engine.state is GameState.AWAITING_REVEAL
    assert controller.playback_timeout_active is False
    assert engine.active_game is not None
    assert engine.active_game.round_termination is not None
    assert engine.active_game.round_termination.reason is (
        RoundTerminationReason.PLAYBACK_TIMEOUT
    )
    assert controller.latest_projection is not None
    assert controller.latest_projection.revision > playing_revision
    assert controller.latest_projection.public is not None
    assert controller.latest_projection.public.state is GameState.AWAITING_REVEAL
    await controller.stop()


@pytest.mark.asyncio
async def test_explicit_transition_g_retires_still_sleeping_timer(
    controller_storage: tuple[Engine, Path],
) -> None:
    (
        controller,
        engine,
        clock,
        _,
        playback_sleep,
    ) = await start_playback_through_controller(controller_storage)
    controls = []
    controller.add_audio_control_listener(controls.append)
    clock.value = 130.0

    controller.post_system_event(PlaybackTimeoutEvent(GAME_ID, ROUND_ID))
    await controller.playback_timeout_processed.wait()
    await playback_sleep.cancelled.wait()

    assert engine.state is GameState.AWAITING_REVEAL
    assert controller.playback_timeout_active is False
    assert [
        command.reason for command in controls if isinstance(command, StopAudioCommand)
    ] == [AudioStopReason.PLAYBACK_TIMEOUT]
    await controller.stop()


@pytest.mark.asyncio
async def test_accepted_buzz_retires_playback_timer(
    controller_storage: tuple[Engine, Path],
) -> None:
    controller, _, clock, _, playback_sleep = await start_playback_through_controller(
        controller_storage
    )
    controls = []
    controller.add_audio_control_listener(controls.append)
    clock.value = 102.0

    accepted = await controller.submit_request(
        BuzzRequest(GAME_ID, ROUND_ID, "player-token", "player-connection")
    )
    await playback_sleep.cancelled.wait()

    assert accepted.reason is RoundTerminationReason.BUZZ
    assert controller.playback_timeout_active is False
    assert [
        command.reason for command in controls if isinstance(command, StopAudioCommand)
    ] == [AudioStopReason.ACCEPTED_BUZZ]
    await controller.stop()


@pytest.mark.asyncio
async def test_already_queued_stale_timeout_after_buzz_is_harmless(
    controller_storage: tuple[Engine, Path],
) -> None:
    controller, engine, clock, _, _ = await start_playback_through_controller(
        controller_storage
    )
    clock.value = 102.0
    accepted = await controller.submit_request(
        BuzzRequest(GAME_ID, ROUND_ID, "player-token", "player-connection")
    )
    controller.post_system_event(PlaybackTimeoutEvent(GAME_ID, ROUND_ID))
    await controller.playback_timeout_processed.wait()

    assert engine.active_game is not None
    assert engine.active_game.round_termination is accepted
    assert any(
        isinstance(rejection.error, PlaybackTimeoutInapplicableError)
        for rejection in controller.system_event_rejections
    )
    assert controller.is_running is True
    await controller.stop()


@pytest.mark.asyncio
async def test_shutdown_cleans_up_playback_timer(
    controller_storage: tuple[Engine, Path],
) -> None:
    controller, _, _, _, playback_sleep = await start_playback_through_controller(
        controller_storage
    )

    await controller.stop()

    assert controller.playback_timeout_active is False
    assert playback_sleep.cancelled.is_set()


@pytest.mark.asyncio
async def test_fatal_failure_cleans_up_playback_timer(
    controller_storage: tuple[Engine, Path],
) -> None:
    controller, _, clock, _, playback_sleep = await start_playback_through_controller(
        controller_storage
    )
    clock.value = 102.0

    controller.post_system_event(UnknownSystemEvent())
    with pytest.raises(ControllerFailedError):
        await controller.submit_request(
            BuzzRequest(GAME_ID, ROUND_ID, "player-token", "player-connection")
        )
    await playback_sleep.cancelled.wait()

    assert controller.playback_timeout_active is False
    with pytest.raises(ControllerFailedError):
        await controller.stop()
