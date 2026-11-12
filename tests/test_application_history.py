import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

import who_sings_that.application.controller as controller_module
from who_sings_that.application import (
    AcceptScoreRequest,
    AcknowledgeAudioPreparationFailureRequest,
    AudioPreparationFailedEvent,
    AudioPreparationReadyEvent,
    BuzzRequest,
    CloseGameRequest,
    CorrectScoreRequest,
    CreateGameRequest,
    EndGameRequest,
    GameController,
    MemberDisconnectedEvent,
    PlaybackTimeoutEvent,
    PlaySongRequest,
    RetryCompletedGamePersistenceRequest,
    RevealRequest,
)
from who_sings_that.database import (
    CompletedGameHistoryPersistenceError,
    create_sqlite_engine,
    get_completed_game_history,
    list_completed_games,
    upgrade_database,
)
from who_sings_that.domain import (
    GameEngine,
    GameSettings,
    GameState,
    HistoricalDeckOutcome,
    ResolvedPlaylist,
    RoundVoidReason,
    ScoringChoice,
    SelectedPlaylistSnapshot,
    SongmasterEventReason,
    SongmasterRecoveryInapplicableError,
    TrackReference,
)

GAME_IDS = (UUID(int=7001), UUID(int=7002))
PREPARATION_IDS = tuple(UUID(int=value) for value in range(7101, 7110))
ROUND_IDS = tuple(UUID(int=value) for value in range(7201, 7210))


class AdvancingClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        current = self.value
        self.value += timedelta(seconds=1)
        return current


class MutableMonotonicClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


class Values:
    def __init__(self, values: tuple[UUID, ...]) -> None:
        self._values = iter(values)

    def __call__(self) -> UUID:
        return next(self._values)


async def never_sleep(_: float) -> None:
    await asyncio.Event().wait()


def resolved_playlist(*tracks: TrackReference) -> ResolvedPlaylist:
    return ResolvedPlaylist(
        SelectedPlaylistSnapshot(
            "80000000-0000-4000-8000-000000000001",
            "History Playlist",
            datetime(2026, 1, 1, tzinfo=UTC),
            len(tracks),
        ),
        tracks,
    )


def started_game(
    *tracks: TrackReference,
) -> tuple[GameEngine, MutableMonotonicClock]:
    monotonic = MutableMonotonicClock()
    engine = GameEngine(
        GameSettings(playback_duration_seconds=30),
        clock=AdvancingClock(),
        monotonic_clock=monotonic,
        game_id_factory=Values(GAME_IDS),
        preparation_id_factory=Values(PREPARATION_IDS),
        round_id_factory=Values(ROUND_IDS),
        shuffle=lambda values: None,
    )
    engine.create_game("sm", "Songmaster")
    engine.join_game("p1", "Player One")
    engine.set_audio_preference("sm", False)
    engine.set_audio_preference("p1", True)
    engine.set_browser_audio_capability("p1", True)
    engine.select_playlist("sm", "80000000-0000-4000-8000-000000000001")
    engine.start_game("sm", [resolved_playlist(*tracks)])
    return engine, monotonic


def controller_for(engine: GameEngine, database_path: Path, music_root: Path):
    database_engine = create_sqlite_engine(database_path)
    controller = GameController(
        database_engine,
        music_root,
        game_engine=engine,
        preparation_timeout_sleep=never_sleep,
        playback_timeout_sleep=never_sleep,
    )
    game = engine.active_game
    assert game is not None
    controller.connection_registry.register(game.game_session_id, "sm", "sm-conn")
    controller.connection_registry.register(game.game_session_id, "p1", "p1-conn")
    return controller, database_engine


async def ready_current_round(controller: GameController) -> UUID:
    game = controller.game_engine.active_game
    assert game is not None
    pending = await controller.submit_request(PlaySongRequest(GAME_IDS[0], "sm"))
    controller.post_system_event(
        AudioPreparationReadyEvent(
            GAME_IDS[0],
            pending.preparation_id,
            "p1",
            "p1-conn",
        )
    )
    await controller._queue.join()
    assert game.active_round is not None
    return game.active_round.round_id


@pytest.mark.asyncio
async def test_complete_game_history_captures_all_outcomes_and_survives_close(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "complete.sqlite3"
    upgrade_database(database_path)
    music_root = tmp_path / "music"
    music_root.mkdir()
    tracks = (
        TrackReference(1, Decimal("180.5"), "Buzz Artist", "Buzz Title"),
        TrackReference(2, 180, "Timeout Artist", "Timeout Title"),
        TrackReference(3, 180, "Failed Artist", "Failed Title"),
        TrackReference(4, 180, "Voided Artist", "Voided Title"),
        TrackReference(5, 5, "Short Artist", "Short Title"),
    )
    engine, monotonic = started_game(*tracks)
    controller, database_engine = controller_for(
        engine,
        database_path,
        music_root,
    )
    await controller.start()

    first_round = await ready_current_round(controller)
    monotonic.value = 105.0
    await controller.submit_request(
        BuzzRequest(GAME_IDS[0], first_round, "p1", "p1-conn")
    )
    await controller.submit_request(RevealRequest(GAME_IDS[0], first_round, "sm"))
    initial = await controller.submit_request(
        AcceptScoreRequest(
            GAME_IDS[0],
            first_round,
            "sm",
            ScoringChoice.BOTH,
        )
    )
    corrected = await controller.submit_request(
        CorrectScoreRequest(
            GAME_IDS[0],
            first_round,
            "sm",
            ScoringChoice.ARTIST,
        )
    )
    assert corrected.awarded_points < initial.awarded_points

    monotonic.value = 200.0
    second_round = await ready_current_round(controller)
    monotonic.value = 230.0
    controller.post_system_event(PlaybackTimeoutEvent(GAME_IDS[0], second_round))
    await controller._queue.join()
    await controller.submit_request(RevealRequest(GAME_IDS[0], second_round, "sm"))

    pending = await controller.submit_request(PlaySongRequest(GAME_IDS[0], "sm"))
    controller.post_system_event(
        AudioPreparationFailedEvent(
            GAME_IDS[0],
            pending.preparation_id,
            "p1",
            "p1-conn",
        )
    )
    await controller._queue.join()
    await controller.submit_request(
        AcknowledgeAudioPreparationFailureRequest(
            GAME_IDS[0],
            pending.preparation_id,
            "sm",
        )
    )

    monotonic.value = 300.0
    await ready_current_round(controller)
    controller.post_system_event(
        MemberDisconnectedEvent(GAME_IDS[0], "p1", "p1-conn")
    )
    await controller._queue.join()
    finalization = await controller.submit_request(PlaySongRequest(GAME_IDS[0], "sm"))

    game = engine.active_game
    assert game is not None
    assert controller.is_completed_history_persisted(GAME_IDS[0]) is True
    assert finalization.final_standings[0].score == corrected.awarded_points
    history = get_completed_game_history(database_engine, GAME_IDS[0])
    assert history is not None
    assert [entry.outcome for entry in history.deck_entries] == [
        HistoricalDeckOutcome.COMPLETED,
        HistoricalDeckOutcome.COMPLETED,
        HistoricalDeckOutcome.SKIPPED,
        HistoricalDeckOutcome.VOIDED,
        HistoricalDeckOutcome.SKIPPED,
    ]
    assert history.deck_entries[0].awarded_points == corrected.awarded_points
    assert history.deck_entries[0].scoring_choice is ScoringChoice.ARTIST
    assert history.deck_entries[1].awarded_points == 0
    assert history.deck_entries[1].buzzing_participant_id is None
    assert history.deck_entries[2].effective_playback_offset_seconds == Decimal(30)
    assert history.deck_entries[3].void_reason is RoundVoidReason.PLAYBACK_CLIENT_LOST
    assert history.deck_entries[4].effective_playback_offset_seconds is None
    assert len({entry.round_id for entry in history.deck_entries if entry.round_id}) == 3
    assert history.selected_playlists[0].playlist_name == "History Playlist"
    assert history.songmaster_events[0].reason is SongmasterEventReason.INITIAL_ASSIGNMENT

    await controller.submit_request(CloseGameRequest(GAME_IDS[0], "sm"))
    assert engine.state is GameState.NO_GAME
    await controller.submit_request(CreateGameRequest("sm", "Fresh", "fresh-conn"))
    assert get_completed_game_history(database_engine, GAME_IDS[0]) == history
    assert list_completed_games(database_engine)[0].game_session_id == GAME_IDS[0]
    await controller.stop()
    database_engine.dispose()

    reopened = create_sqlite_engine(database_path)
    assert get_completed_game_history(reopened, GAME_IDS[0]) == history
    reopened.dispose()


@pytest.mark.asyncio
async def test_failed_finalization_write_retains_finished_game_for_retry_and_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "retry.sqlite3"
    upgrade_database(database_path)
    music_root = tmp_path / "music"
    music_root.mkdir()
    engine, _ = started_game(TrackReference(1, 5, "Artist", "Title"))
    controller, database_engine = controller_for(engine, database_path, music_root)
    real_save = controller_module.save_completed_game_history

    def fail_save(*args, **kwargs):
        raise CompletedGameHistoryPersistenceError("injected write failure")

    monkeypatch.setattr(controller_module, "save_completed_game_history", fail_save)
    await controller.start()

    with pytest.raises(CompletedGameHistoryPersistenceError):
        await controller.submit_request(PlaySongRequest(GAME_IDS[0], "sm"))
    assert engine.state is GameState.FINISHED
    assert engine.active_game is not None
    assert controller.is_completed_history_persisted(GAME_IDS[0]) is False
    assert get_completed_game_history(database_engine, GAME_IDS[0]) is None

    with pytest.raises(CompletedGameHistoryPersistenceError):
        await controller.submit_request(CloseGameRequest(GAME_IDS[0], "sm"))
    assert engine.state is GameState.FINISHED
    assert engine.active_game is not None
    assert controller.is_running is True

    monkeypatch.setattr(controller_module, "save_completed_game_history", real_save)
    await controller.submit_request(
        RetryCompletedGamePersistenceRequest(GAME_IDS[0], "sm")
    )
    assert controller.is_completed_history_persisted(GAME_IDS[0]) is True
    await controller.submit_request(CloseGameRequest(GAME_IDS[0], "sm"))
    assert engine.state is GameState.NO_GAME
    assert get_completed_game_history(database_engine, GAME_IDS[0]) is not None
    await controller.stop()
    database_engine.dispose()


@pytest.mark.asyncio
async def test_end_game_during_playing_persists_unfinished_round_as_void(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "early-end.sqlite3"
    upgrade_database(database_path)
    music_root = tmp_path / "music"
    music_root.mkdir()
    engine, _ = started_game(
        TrackReference(1, 180, "Started Artist", "Started Title"),
        TrackReference(2, 180, "Future Artist", "Future Title"),
    )
    controller, database_engine = controller_for(engine, database_path, music_root)
    await controller.start()
    await ready_current_round(controller)

    await controller.submit_request(EndGameRequest(GAME_IDS[0], "sm"))

    history = get_completed_game_history(database_engine, GAME_IDS[0])
    assert history is not None
    assert history.deck_entries[0].outcome is HistoricalDeckOutcome.VOIDED
    assert history.deck_entries[0].void_reason is RoundVoidReason.GAME_ENDED
    assert history.deck_entries[0].round_termination_reason is None
    assert history.deck_entries[0].awarded_points is None
    assert history.deck_entries[1].outcome is HistoricalDeckOutcome.UNPLAYED
    await controller.stop()
    database_engine.dispose()


def test_songmaster_history_distinguishes_reconnect_and_role_succession() -> None:
    reconnect_engine, _ = started_game(
        TrackReference(1, 180, "Artist", "Title")
    )
    reconnect_game = reconnect_engine.active_game
    assert reconnect_game is not None
    recovery = reconnect_engine.begin_songmaster_recovery("sm", 100.0, 110.0)
    reconnect_engine.recover_original_songmaster("sm", 105.0)

    with pytest.raises(SongmasterRecoveryInapplicableError):
        reconnect_engine.recover_original_songmaster("sm", 106.0)
    assert [event.reason for event in reconnect_game.songmaster_events] == [
        SongmasterEventReason.INITIAL_ASSIGNMENT,
        SongmasterEventReason.ORIGINAL_RECONNECTED,
    ]
    assert reconnect_game.songmaster_events[-1].previous_songmaster_browser_token == (
        reconnect_game.songmaster_events[-1].resulting_songmaster_browser_token
    )
    assert recovery.recovery_id is not None

    reconnect_engine.end_game(GAME_IDS[0], "sm")
    frozen_history = reconnect_game.completed_game_history
    finished_recovery = reconnect_engine.begin_songmaster_recovery(
        "sm",
        120.0,
        130.0,
    )
    reconnect_engine.expire_songmaster_recovery(
        GAME_IDS[0],
        finished_recovery.recovery_id,
        130.0,
        "p1",
    )
    assert reconnect_game.completed_game_history is frozen_history
    assert [event.reason for event in reconnect_game.songmaster_events] == [
        SongmasterEventReason.INITIAL_ASSIGNMENT,
        SongmasterEventReason.ORIGINAL_RECONNECTED,
    ]

    failover_engine, _ = started_game(
        TrackReference(1, 180, "Artist", "Title")
    )
    failover_game = failover_engine.active_game
    assert failover_game is not None
    failover_engine.transfer_songmaster("sm", "p1")
    active = failover_engine.begin_songmaster_recovery("p1", 200.0, 210.0)
    failover_engine.expire_songmaster_recovery(
        GAME_IDS[0],
        active.recovery_id,
        210.0,
        "sm",
    )
    failover_engine.end_game(GAME_IDS[0], "sm")

    history = failover_game.completed_game_history
    assert history is not None
    assert [event.reason for event in history.songmaster_events] == [
        SongmasterEventReason.INITIAL_ASSIGNMENT,
        SongmasterEventReason.MANUAL_TRANSFER,
        SongmasterEventReason.AUTOMATIC_FAILOVER,
    ]
    assert history.songmaster_events[-1].previous_participant_id == 2
    assert history.songmaster_events[-1].resulting_participant_id == 1
