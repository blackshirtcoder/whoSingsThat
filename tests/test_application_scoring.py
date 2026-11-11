import asyncio
from collections.abc import Iterator
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import Engine

from who_sings_that.application import (
    AcceptScoreRequest,
    AudioPreparationReadyEvent,
    BuzzRequest,
    CorrectScoreRequest,
    GameController,
    PlaySongRequest,
    RevealRequest,
    StaleGameSessionError,
)
from who_sings_that.database import create_sqlite_engine, upgrade_database
from who_sings_that.domain import (
    GameEngine,
    GameFinalizationResult,
    GameSettings,
    GameState,
    ResolvedPlaylist,
    ScoreCorrectionNotAllowedError,
    ScoringChoice,
    ScoringNotAllowedError,
    SelectedPlaylistSnapshot,
    TrackReference,
    UnauthorizedScoringError,
    calculate_points,
)
from who_sings_that.domain.engine import (
    PlaySongNotAllowedError,
    UnauthorizedPlaySongError,
)

GAME_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
PLAYLIST_CREATED_AT = datetime(2025, 1, 2, 3, 4, tzinfo=UTC)
TRACKS = (
    TrackReference(10, 180, "First Artist", "First Title"),
    TrackReference(20, 180, "Second Artist", "Second Title"),
    TrackReference(30, 180, "Third Artist", "Third Title"),
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
    def __init__(self, start: int) -> None:
        self.value = start

    def __call__(self) -> UUID:
        result = UUID(int=self.value)
        self.value += 1
        return result


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
    resolved_tracks = tracks or TRACKS
    return ResolvedPlaylist(
        snapshot=SelectedPlaylistSnapshot(
            playlist_id="playlist",
            playlist_name="Playlist",
            playlist_created_at=PLAYLIST_CREATED_AT,
            playlist_song_count=len(resolved_tracks),
        ),
        tracks=resolved_tracks,
    )


def scoring_engine(*tracks: TrackReference):
    monotonic_clock = MutableMonotonicClock()
    engine = GameEngine(
        GameSettings(
            playback_duration_seconds=30,
            speed_bonus_plateau_percent=Decimal(10),
        ),
        clock=AdvancingClock(),
        monotonic_clock=monotonic_clock,
        game_id_factory=lambda: GAME_ID,
        preparation_id_factory=UUIDFactory(100),
        round_id_factory=UUIDFactory(200),
        shuffle=lambda values: None,
    )
    game = engine.create_game("songmaster-token", "Songmaster")
    engine.join_game("winner-token", "Winner")
    engine.set_browser_audio_capability("songmaster-token", True)
    engine.select_playlist("songmaster-token", "playlist")
    engine.start_game("songmaster-token", [resolved_playlist(*tracks)])
    pending = engine.play_song("songmaster-token")
    engine.record_audio_preparation_ready(
        GAME_ID,
        pending.preparation_id,
        "songmaster-token",
    )
    assert game.active_round is not None
    active_round = game.active_round
    engine.accept_buzz(
        GAME_ID,
        active_round.round_id,
        "winner-token",
        active_round.playback_started_monotonic + 8,
    )
    engine.reveal_answer(GAME_ID, active_round.round_id, "songmaster-token")
    return engine, game, monotonic_clock, active_round


def make_controller(
    storage: tuple[Engine, Path],
    engine: GameEngine,
) -> GameController:
    database_engine, music_root = storage
    controller = GameController(database_engine, music_root, game_engine=engine)
    controller.connection_registry.register(
        GAME_ID, "songmaster-token", "connection-1"
    )
    controller.connection_registry.register(
        GAME_ID, "winner-token", "winner-connection"
    )
    return controller


def test_scoring_requests_are_small_typed_interfaces() -> None:
    expected_fields = [
        "game_session_id",
        "round_id",
        "requesting_browser_token",
        "scoring_choice",
    ]
    assert [field.name for field in fields(AcceptScoreRequest)] == expected_fields
    assert [field.name for field in fields(CorrectScoreRequest)] == expected_fields


@pytest.mark.asyncio
async def test_controller_dispatches_initial_scoring_and_repeated_corrections(
    controller_storage: tuple[Engine, Path],
) -> None:
    engine, game, _, active_round = scoring_engine()
    controller = make_controller(controller_storage, engine)
    await controller.start()

    initial = await controller.submit_request(
        AcceptScoreRequest(
            GAME_ID,
            active_round.round_id,
            "songmaster-token",
            ScoringChoice.ARTIST,
        )
    )
    corrected_both = await controller.submit_request(
        CorrectScoreRequest(
            GAME_ID,
            active_round.round_id,
            "songmaster-token",
            ScoringChoice.BOTH,
        )
    )
    corrected_neither = await controller.submit_request(
        CorrectScoreRequest(
            GAME_ID,
            active_round.round_id,
            "songmaster-token",
            ScoringChoice.NEITHER,
        )
    )

    assert initial.scoring_choice is ScoringChoice.ARTIST
    assert corrected_both.scoring_choice is ScoringChoice.BOTH
    assert corrected_neither.scoring_choice is ScoringChoice.NEITHER
    assert game.members["winner-token"].committed_score == 0
    assert controller.is_running is True
    await controller.stop()


@pytest.mark.asyncio
async def test_controller_returns_expected_scoring_rejections_and_keeps_running(
    controller_storage: tuple[Engine, Path],
) -> None:
    engine, game, _, active_round = scoring_engine()
    controller = make_controller(controller_storage, engine)
    await controller.start()

    with pytest.raises(UnauthorizedScoringError):
        await controller.submit_request(
            AcceptScoreRequest(
                GAME_ID,
                active_round.round_id,
                "winner-token",
                ScoringChoice.BOTH,
            )
        )
    with pytest.raises(ScoringNotAllowedError):
        await controller.submit_request(
            AcceptScoreRequest(
                UUID(int=999),
                active_round.round_id,
                "songmaster-token",
                ScoringChoice.BOTH,
            )
        )

    assert game.members["winner-token"].committed_score == 0
    result = await controller.submit_request(
        AcceptScoreRequest(
            GAME_ID,
            active_round.round_id,
            "songmaster-token",
            ScoringChoice.BOTH,
        )
    )
    assert result.awarded_points > 0
    assert controller.is_running is True
    await controller.stop()


@pytest.mark.asyncio
async def test_fifo_correction_before_play_applies_then_closes_window(
    controller_storage: tuple[Engine, Path],
) -> None:
    engine, game, _, active_round = scoring_engine()
    controller = make_controller(controller_storage, engine)
    await controller.start()
    await controller.submit_request(
        AcceptScoreRequest(
            GAME_ID,
            active_round.round_id,
            "songmaster-token",
            ScoringChoice.ARTIST,
        )
    )

    correction_task = asyncio.create_task(
        controller.submit_request(
            CorrectScoreRequest(
                GAME_ID,
                active_round.round_id,
                "songmaster-token",
                ScoringChoice.BOTH,
            )
        )
    )
    await asyncio.sleep(0)
    play_task = asyncio.create_task(
        controller.submit_request(PlaySongRequest(GAME_ID, "songmaster-token"))
    )
    correction, pending = await asyncio.gather(correction_task, play_task)

    assert correction.scoring_choice is ScoringChoice.BOTH
    assert pending is game.pending_audio_preparation
    assert engine.state is GameState.AUDIO_PREPARING
    assert game.members["winner-token"].committed_score == correction.awarded_points
    assert game.scoring_correction_round_id is None
    await controller.stop()


@pytest.mark.asyncio
async def test_fifo_play_before_correction_makes_correction_stale(
    controller_storage: tuple[Engine, Path],
) -> None:
    engine, game, _, active_round = scoring_engine()
    controller = make_controller(controller_storage, engine)
    await controller.start()
    initial = await controller.submit_request(
        AcceptScoreRequest(
            GAME_ID,
            active_round.round_id,
            "songmaster-token",
            ScoringChoice.ARTIST,
        )
    )

    play_task = asyncio.create_task(
        controller.submit_request(PlaySongRequest(GAME_ID, "songmaster-token"))
    )
    await asyncio.sleep(0)
    correction_task = asyncio.create_task(
        controller.submit_request(
            CorrectScoreRequest(
                GAME_ID,
                active_round.round_id,
                "songmaster-token",
                ScoringChoice.BOTH,
            )
        )
    )
    await play_task
    with pytest.raises(ScoreCorrectionNotAllowedError):
        await correction_task

    assert engine.state is GameState.AUDIO_PREPARING
    assert game.members["winner-token"].committed_score == initial.awarded_points
    assert controller.is_running is True
    await controller.stop()


@pytest.mark.asyncio
async def test_one_song_controller_cycle_corrects_then_explicitly_finalizes(
    controller_storage: tuple[Engine, Path],
) -> None:
    monotonic_clock = MutableMonotonicClock()
    engine = GameEngine(
        GameSettings(
            playback_duration_seconds=30,
            speed_bonus_plateau_percent=Decimal(10),
        ),
        clock=AdvancingClock(),
        monotonic_clock=monotonic_clock,
        game_id_factory=lambda: GAME_ID,
        preparation_id_factory=UUIDFactory(100),
        round_id_factory=UUIDFactory(200),
        shuffle=lambda values: None,
    )
    game = engine.create_game("songmaster-token", "Songmaster")
    engine.join_game("winner-token", "Winner")
    engine.set_browser_audio_capability("songmaster-token", True)
    engine.select_playlist("songmaster-token", "playlist")
    engine.start_game("songmaster-token", [resolved_playlist(TRACKS[0])])
    controller = make_controller(controller_storage, engine)
    await controller.start()

    pending = await controller.submit_request(
        PlaySongRequest(GAME_ID, "songmaster-token")
    )
    assert controller.preparation_timeout_active is True
    controller.post_system_event(
        AudioPreparationReadyEvent(
            GAME_ID,
            pending.preparation_id,
            "songmaster-token",
            "connection-1",
        )
    )
    await controller._queue.join()
    assert game.active_round is not None
    active_round = game.active_round
    assert controller.preparation_timeout_active is False
    assert controller.playback_timeout_active is True

    monotonic_clock.value = active_round.playback_started_monotonic + 8
    await controller.submit_request(
        BuzzRequest(
            GAME_ID,
            active_round.round_id,
            "winner-token",
            "winner-connection",
        )
    )
    await controller.submit_request(
        RevealRequest(GAME_ID, active_round.round_id, "songmaster-token")
    )
    initial = await controller.submit_request(
        AcceptScoreRequest(
            GAME_ID,
            active_round.round_id,
            "songmaster-token",
            ScoringChoice.ARTIST,
        )
    )

    assert engine.state is GameState.ROUND_COMPLETE
    assert game.deck_exhausted is True
    assert game.final_standings == ()
    assert game.scoring_correction_round_id == active_round.round_id
    assert controller.playback_timeout_active is False

    with pytest.raises(StaleGameSessionError):
        await controller.submit_request(
            PlaySongRequest(UUID(int=999), "songmaster-token")
        )
    with pytest.raises(UnauthorizedPlaySongError):
        await controller.submit_request(PlaySongRequest(GAME_ID, "winner-token"))
    game.songmaster.connected = False
    with pytest.raises(UnauthorizedPlaySongError):
        await controller.submit_request(
            PlaySongRequest(GAME_ID, "songmaster-token")
        )
    game.songmaster.connected = True
    assert game.scoring_correction_round_id == active_round.round_id

    correction_task = asyncio.create_task(
        controller.submit_request(
            CorrectScoreRequest(
                GAME_ID,
                active_round.round_id,
                "songmaster-token",
                ScoringChoice.BOTH,
            )
        )
    )
    await asyncio.sleep(0)
    finalization_task = asyncio.create_task(
        controller.submit_request(PlaySongRequest(GAME_ID, "songmaster-token"))
    )
    corrected, finalization = await asyncio.gather(
        correction_task,
        finalization_task,
    )

    assert initial.awarded_points < corrected.awarded_points
    assert isinstance(finalization, GameFinalizationResult)
    assert engine.state is GameState.FINISHED
    assert game.final_standings[0].score == corrected.awarded_points
    assert game.scoring_correction_round_id is None
    assert game.pending_audio_preparation is None
    assert controller.preparation_timeout_active is False
    assert controller.playback_timeout_active is False
    with pytest.raises(PlaySongNotAllowedError):
        await controller.submit_request(
            PlaySongRequest(GAME_ID, "songmaster-token")
        )
    assert controller.is_running is True
    await controller.stop()


@pytest.mark.asyncio
async def test_fifo_finalization_before_correction_rejects_correction(
    controller_storage: tuple[Engine, Path],
) -> None:
    engine, game, _, active_round = scoring_engine(TRACKS[0])
    controller = make_controller(controller_storage, engine)
    await controller.start()
    initial = await controller.submit_request(
        AcceptScoreRequest(
            GAME_ID,
            active_round.round_id,
            "songmaster-token",
            ScoringChoice.ARTIST,
        )
    )

    finalization_task = asyncio.create_task(
        controller.submit_request(PlaySongRequest(GAME_ID, "songmaster-token"))
    )
    await asyncio.sleep(0)
    correction_task = asyncio.create_task(
        controller.submit_request(
            CorrectScoreRequest(
                GAME_ID,
                active_round.round_id,
                "songmaster-token",
                ScoringChoice.BOTH,
            )
        )
    )

    finalization = await finalization_task
    with pytest.raises(ScoreCorrectionNotAllowedError):
        await correction_task

    assert isinstance(finalization, GameFinalizationResult)
    assert engine.state is GameState.FINISHED
    assert game.final_standings[0].score == initial.awarded_points
    assert controller.preparation_timeout_active is False
    assert controller.playback_timeout_active is False
    assert controller.is_running is True
    await controller.stop()


@pytest.mark.asyncio
async def test_two_successive_rounds_keep_cumulative_scoring_isolated(
    controller_storage: tuple[Engine, Path],
) -> None:
    engine, game, monotonic_clock, first_round = scoring_engine()
    controller = make_controller(controller_storage, engine)
    await controller.start()
    first = await controller.submit_request(
        AcceptScoreRequest(
            GAME_ID,
            first_round.round_id,
            "songmaster-token",
            ScoringChoice.ARTIST,
        )
    )
    first = await controller.submit_request(
        CorrectScoreRequest(
            GAME_ID,
            first_round.round_id,
            "songmaster-token",
            ScoringChoice.BOTH,
        )
    )

    monotonic_clock.value = 200
    pending = await controller.submit_request(
        PlaySongRequest(GAME_ID, "songmaster-token")
    )
    controller.post_system_event(
        AudioPreparationReadyEvent(
            GAME_ID,
            pending.preparation_id,
            "songmaster-token",
            "connection-1",
        )
    )
    await controller._queue.join()
    assert game.active_round is not None
    second_round = game.active_round
    monotonic_clock.value = second_round.playback_started_monotonic + 15
    await controller.submit_request(
        BuzzRequest(
            GAME_ID,
            second_round.round_id,
            "winner-token",
            "winner-connection",
        )
    )
    await controller.submit_request(
        RevealRequest(GAME_ID, second_round.round_id, "songmaster-token")
    )
    second = await controller.submit_request(
        AcceptScoreRequest(
            GAME_ID,
            second_round.round_id,
            "songmaster-token",
            ScoringChoice.TITLE,
        )
    )
    second = await controller.submit_request(
        CorrectScoreRequest(
            GAME_ID,
            second_round.round_id,
            "songmaster-token",
            ScoringChoice.NEITHER,
        )
    )

    assert first.awarded_points == calculate_points(
        ScoringChoice.BOTH, 30, Decimal(".1"), 8
    )
    assert second.awarded_points == 0
    assert game.members["winner-token"].committed_score == first.awarded_points
    assert game.last_completed_round is second
    assert game.scoring_correction_round_id == second_round.round_id
    await controller.stop()
