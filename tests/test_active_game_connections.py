from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import Engine

from who_sings_that.application import (
    AudioPreparationFailedEvent,
    AudioPreparationReadyEvent,
    AudioPreparationTimeoutEvent,
    BrowserAudioCapabilityChangedEvent,
    BuzzRequest,
    CancelGameRequest,
    ConnectionRegistrationStatus,
    ConnectionRegistry,
    CreateGameRequest,
    GameController,
    JoinGameRequest,
    MemberDisconnectedEvent,
    PlaybackTimeoutEvent,
    PlaySongRequest,
    RegisterConnectionRequest,
    RevealRequest,
    StaleConnectionError,
)
from who_sings_that.database import create_sqlite_engine, upgrade_database
from who_sings_that.domain import (
    BuzzNotAllowedError,
    GameEngine,
    GameSettings,
    GameState,
    MembershipOperationNotAllowedError,
    ResolvedPlaylist,
    RoundTerminationReason,
    SelectedPlaylistSnapshot,
    SongmasterDisconnectRequiresFailoverError,
    TrackReference,
    UnknownGameMemberError,
)

GAME_IDS = (UUID(int=701), UUID(int=702))
PREPARATION_IDS = (UUID(int=801), UUID(int=802))
ROUND_IDS = (UUID(int=901), UUID(int=902))
PLAYLIST_CREATED_AT = datetime(2026, 1, 1, tzinfo=UTC)


class AdvancingClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        result = self.value
        self.value += timedelta(seconds=1)
        return result


class MutableMonotonicClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


class ValueFactory:
    def __init__(self, values: tuple[UUID, ...]) -> None:
        self._values = iter(values)

    def __call__(self) -> UUID:
        return next(self._values)


@pytest.fixture
def controller_storage(tmp_path: Path) -> Iterator[tuple[Engine, Path]]:
    database_path = tmp_path / "connections.sqlite3"
    upgrade_database(database_path)
    database_engine = create_sqlite_engine(database_path)
    music_root = tmp_path / "synthetic-music"
    music_root.mkdir()
    yield database_engine, music_root
    database_engine.dispose()


def resolved_playlist() -> ResolvedPlaylist:
    tracks = (
        TrackReference(1, 180, "Artist One", "Title One"),
        TrackReference(2, 180, "Artist Two", "Title Two"),
    )
    return ResolvedPlaylist(
        snapshot=SelectedPlaylistSnapshot(
            playlist_id="playlist",
            playlist_name="Synthetic",
            playlist_created_at=PLAYLIST_CREATED_AT,
            playlist_song_count=len(tracks),
        ),
        tracks=tracks,
    )


def started_engine() -> tuple[GameEngine, MutableMonotonicClock]:
    monotonic_clock = MutableMonotonicClock()
    engine = GameEngine(
        GameSettings(playback_duration_seconds=30),
        clock=AdvancingClock(),
        monotonic_clock=monotonic_clock,
        game_id_factory=ValueFactory(GAME_IDS),
        preparation_id_factory=ValueFactory(PREPARATION_IDS),
        round_id_factory=ValueFactory(ROUND_IDS),
        shuffle=lambda values: None,
    )
    engine.create_game("songmaster-token", "Songmaster")
    engine.join_game("player-token", "Player")
    engine.set_browser_audio_capability("songmaster-token", True)
    engine.select_playlist("songmaster-token", "playlist")
    engine.start_game("songmaster-token", [resolved_playlist()])
    return engine, monotonic_clock


def playing_engine() -> tuple[GameEngine, MutableMonotonicClock]:
    engine, monotonic_clock = started_engine()
    pending = engine.play_song("songmaster-token")
    engine.record_audio_preparation_ready(
        GAME_IDS[0], pending.preparation_id, "songmaster-token"
    )
    return engine, monotonic_clock


def controller_for_engine(
    storage: tuple[Engine, Path], engine: GameEngine
) -> GameController:
    database_engine, music_root = storage
    controller = GameController(database_engine, music_root, game_engine=engine)
    controller.connection_registry.register(
        GAME_IDS[0], "songmaster-token", "songmaster-connection"
    )
    controller.connection_registry.register(
        GAME_IDS[0], "player-token", "player-connection-a"
    )
    return controller


def test_connection_registry_replacement_disconnect_idempotency_and_cleanup() -> None:
    registry = ConnectionRegistry()

    first = registry.register(GAME_IDS[0], "player-token", "connection-a")
    duplicate = registry.register(GAME_IDS[0], "player-token", "connection-a")
    replacement = registry.register(GAME_IDS[0], "player-token", "connection-b")

    assert first.status is ConnectionRegistrationStatus.ESTABLISHED
    assert duplicate.status is ConnectionRegistrationStatus.UNCHANGED
    assert replacement.status is ConnectionRegistrationStatus.REPLACED
    assert registry.is_current(GAME_IDS[0], "player-token", "connection-b")
    assert not registry.mark_disconnected(
        GAME_IDS[0], "player-token", "connection-a"
    )
    assert registry.is_current(GAME_IDS[0], "player-token", "connection-b")
    assert registry.mark_disconnected(
        GAME_IDS[0], "player-token", "connection-b"
    )
    assert not registry.is_current(GAME_IDS[0], "player-token", "connection-b")
    assert (
        registry.register(GAME_IDS[0], "player-token", "connection-b").status
        is ConnectionRegistrationStatus.RECONNECTED
    )
    registry.clear_game(GAME_IDS[0])
    assert not registry.is_current(GAME_IDS[0], "player-token", "connection-b")


@pytest.mark.parametrize(
    "target_state",
    [
        GameState.ROUND_COMPLETE,
        GameState.AUDIO_PREPARING,
        GameState.PLAYING,
        GameState.AWAITING_REVEAL,
        GameState.SCORING,
    ],
)
def test_new_member_can_join_started_states_without_current_song_eligibility(
    target_state: GameState,
) -> None:
    engine, monotonic_clock = started_engine()
    pending = None
    if target_state is not GameState.ROUND_COMPLETE:
        pending = engine.play_song("songmaster-token")
    if target_state in {
        GameState.PLAYING,
        GameState.AWAITING_REVEAL,
        GameState.SCORING,
    }:
        assert pending is not None
        engine.record_audio_preparation_ready(
            GAME_IDS[0], pending.preparation_id, "songmaster-token"
        )
    game = engine.active_game
    assert game is not None
    if target_state in {GameState.AWAITING_REVEAL, GameState.SCORING}:
        assert game.active_round is not None
        engine.accept_buzz(
            GAME_IDS[0],
            game.active_round.round_id,
            "player-token",
            monotonic_clock.value + 1,
        )
    if target_state is GameState.SCORING:
        assert game.active_round is not None
        engine.reveal_answer(
            GAME_IDS[0], game.active_round.round_id, "songmaster-token"
        )

    member = engine.join_game("late-token", "  Late   Player  ")

    assert engine.state is target_state
    assert member.display_name == "Late Player"
    assert member.committed_score == 0
    assert member.audio_enabled is False
    assert member.browser_audio_usable is False
    snapshot = (
        game.pending_audio_preparation.buzz_eligible_browser_tokens
        if game.pending_audio_preparation is not None
        else (
            game.active_round.buzz_eligible_browser_tokens
            if game.active_round is not None
            else frozenset()
        )
    )
    assert "late-token" not in snapshot


def test_join_is_rejected_after_game_is_finished() -> None:
    engine, _ = started_engine()
    engine.end_game(GAME_IDS[0], "songmaster-token")

    with pytest.raises(MembershipOperationNotAllowedError):
        engine.join_game("late-token", "Late Player")


@pytest.mark.asyncio
async def test_replacement_stale_disconnect_and_current_song_buzz_ineligibility(
    controller_storage: tuple[Engine, Path],
) -> None:
    """Focused manual integration target for connection replacement behavior."""

    engine, monotonic_clock = playing_engine()
    game = engine.active_game
    assert game is not None and game.active_round is not None
    first_round_id = game.active_round.round_id
    player = game.members["player-token"]
    player.audio_enabled = True
    player.browser_audio_usable = True
    player.committed_score = 77
    controller = controller_for_engine(controller_storage, engine)
    await controller.start()

    replacement = await controller.submit_request(
        RegisterConnectionRequest(
            GAME_IDS[0], "player-token", "player-connection-b"
        )
    )
    assert replacement.status is ConnectionRegistrationStatus.REPLACED
    assert player.display_name == "Player"
    assert player.committed_score == 77
    assert player.audio_enabled is True
    assert player.browser_audio_usable is False

    controller.post_system_event(
        MemberDisconnectedEvent(
            GAME_IDS[0], "player-token", "player-connection-a"
        )
    )
    controller.post_system_event(
        BrowserAudioCapabilityChangedEvent(
            GAME_IDS[0], "player-token", "player-connection-a", True
        )
    )
    duplicate = await controller.submit_request(
        RegisterConnectionRequest(
            GAME_IDS[0], "player-token", "player-connection-b"
        )
    )
    assert duplicate.status is ConnectionRegistrationStatus.UNCHANGED
    assert player.connected is True
    assert player.browser_audio_usable is False
    assert len(controller.system_event_rejections) == 2
    assert all(
        isinstance(rejection.error, StaleConnectionError)
        for rejection in controller.system_event_rejections
    )

    controller.post_system_event(
        BrowserAudioCapabilityChangedEvent(
            GAME_IDS[0], "player-token", "player-connection-b", True
        )
    )
    await controller.submit_request(
        RegisterConnectionRequest(
            GAME_IDS[0], "player-token", "player-connection-b"
        )
    )
    assert player.browser_audio_usable is True

    with pytest.raises(BuzzNotAllowedError):
        await controller.submit_request(
            BuzzRequest(
                GAME_IDS[0],
                first_round_id,
                "player-token",
                "player-connection-b",
            )
        )

    monotonic_clock.value = game.active_round.playback_deadline_monotonic
    controller.post_system_event(
        PlaybackTimeoutEvent(GAME_IDS[0], first_round_id)
    )
    await controller.submit_request(
        RevealRequest(GAME_IDS[0], first_round_id, "songmaster-token")
    )
    controller.post_system_event(
        BrowserAudioCapabilityChangedEvent(
            GAME_IDS[0],
            "songmaster-token",
            "songmaster-connection",
            True,
        )
    )
    pending = await controller.submit_request(
        PlaySongRequest(GAME_IDS[0], "songmaster-token")
    )
    controller.post_system_event(
        AudioPreparationReadyEvent(
            GAME_IDS[0],
            pending.preparation_id,
            "songmaster-token",
            "songmaster-connection",
        )
    )
    controller.post_system_event(
        AudioPreparationFailedEvent(
            GAME_IDS[0],
            pending.preparation_id,
            "player-token",
            "player-connection-b",
        )
    )
    await controller.submit_request(
        RegisterConnectionRequest(
            GAME_IDS[0], "player-token", "player-connection-b"
        )
    )
    assert game.active_round is not None
    monotonic_clock.value = game.active_round.playback_started_monotonic + 1

    termination = await controller.submit_request(
        BuzzRequest(
            GAME_IDS[0],
            game.active_round.round_id,
            "player-token",
            "player-connection-b",
        )
    )
    assert termination.reason is RoundTerminationReason.BUZZ
    await controller.stop()


@pytest.mark.asyncio
async def test_duplicate_live_registration_preserves_current_round_eligibility(
    controller_storage: tuple[Engine, Path],
) -> None:
    engine, monotonic_clock = playing_engine()
    game = engine.active_game
    assert game is not None and game.active_round is not None
    controller = controller_for_engine(controller_storage, engine)
    await controller.start()

    registration = await controller.submit_request(
        RegisterConnectionRequest(
            GAME_IDS[0], "player-token", "player-connection-a"
        )
    )
    assert registration.status is ConnectionRegistrationStatus.UNCHANGED
    monotonic_clock.value = game.active_round.playback_started_monotonic + 1
    termination = await controller.submit_request(
        BuzzRequest(
            GAME_IDS[0],
            game.active_round.round_id,
            "player-token",
            "player-connection-a",
        )
    )

    assert termination.reason is RoundTerminationReason.BUZZ
    await controller.stop()


@pytest.mark.asyncio
async def test_fifo_mid_song_join_is_observer_until_later_transition_c(
    controller_storage: tuple[Engine, Path],
) -> None:
    engine, monotonic_clock = playing_engine()
    game = engine.active_game
    assert game is not None and game.active_round is not None
    current_round = game.active_round
    controller = controller_for_engine(controller_storage, engine)
    await controller.start()

    late_member = await controller.submit_request(
        JoinGameRequest(
            GAME_IDS[0],
            "late-token",
            "late-connection",
            "Late Player",
        )
    )
    assert late_member.committed_score == 0
    assert "late-token" not in current_round.buzz_eligible_browser_tokens
    assert controller.connection_registry.is_current(
        GAME_IDS[0], "late-token", "late-connection"
    )
    monotonic_clock.value = current_round.playback_started_monotonic + 1

    with pytest.raises(BuzzNotAllowedError):
        await controller.submit_request(
            BuzzRequest(
                GAME_IDS[0],
                current_round.round_id,
                "late-token",
                "late-connection",
            )
        )

    assert controller.is_running is True
    await controller.stop()


@pytest.mark.asyncio
async def test_disconnect_during_preparation_leaves_timeout_resolution_available(
    controller_storage: tuple[Engine, Path],
) -> None:
    engine, _ = started_engine()
    game = engine.active_game
    assert game is not None
    engine.set_audio_preference("songmaster-token", False)
    engine.set_audio_preference("player-token", True)
    engine.set_browser_audio_capability("player-token", True)
    pending = engine.play_song("songmaster-token")
    controller = controller_for_engine(controller_storage, engine)
    await controller.start()

    controller.post_system_event(
        MemberDisconnectedEvent(
            GAME_IDS[0], "player-token", "player-connection-a"
        )
    )
    controller.post_system_event(
        AudioPreparationTimeoutEvent(GAME_IDS[0], pending.preparation_id)
    )
    await controller.submit_request(
        RegisterConnectionRequest(
            GAME_IDS[0], "songmaster-token", "songmaster-connection"
        )
    )

    responses = game.audio_preparation_responses
    assert responses is not None
    assert responses.failure_acknowledgement_pending is True
    assert game.members["player-token"].connected is False
    assert engine.state is GameState.AUDIO_PREPARING
    await controller.stop()


@pytest.mark.asyncio
async def test_contestant_disconnect_does_not_yet_void_playing_round(
    controller_storage: tuple[Engine, Path],
) -> None:
    engine, _ = playing_engine()
    game = engine.active_game
    assert game is not None and game.active_round is not None
    round_id = game.active_round.round_id
    controller = controller_for_engine(controller_storage, engine)
    await controller.start()

    controller.post_system_event(
        MemberDisconnectedEvent(
            GAME_IDS[0], "player-token", "player-connection-a"
        )
    )
    await controller.submit_request(
        RegisterConnectionRequest(
            GAME_IDS[0], "songmaster-token", "songmaster-connection"
        )
    )

    assert game.members["player-token"].connected is False
    assert engine.state is GameState.PLAYING
    assert game.active_round is not None
    assert game.active_round.round_id == round_id
    assert game.last_voided_round is None
    await controller.stop()


@pytest.mark.asyncio
async def test_current_connection_preparation_event_and_stale_predecessor(
    controller_storage: tuple[Engine, Path],
) -> None:
    engine, _ = started_engine()
    pending = engine.play_song("songmaster-token")
    controller = controller_for_engine(controller_storage, engine)
    await controller.start()
    await controller.submit_request(
        RegisterConnectionRequest(
            GAME_IDS[0], "songmaster-token", "songmaster-connection-b"
        )
    )

    controller.post_system_event(
        AudioPreparationReadyEvent(
            GAME_IDS[0],
            pending.preparation_id,
            "songmaster-token",
            "songmaster-connection",
        )
    )
    controller.post_system_event(
        AudioPreparationReadyEvent(
            GAME_IDS[0],
            pending.preparation_id,
            "songmaster-token",
            "songmaster-connection-b",
        )
    )
    await controller.submit_request(
        RegisterConnectionRequest(
            GAME_IDS[0], "songmaster-token", "songmaster-connection-b"
        )
    )

    assert len(controller.system_event_rejections) == 1
    assert isinstance(controller.system_event_rejections[0].error, StaleConnectionError)
    assert engine.state is GameState.PLAYING
    await controller.stop()


@pytest.mark.asyncio
async def test_songmaster_disconnect_is_deferred_without_retiring_connection(
    controller_storage: tuple[Engine, Path],
) -> None:
    engine, _ = started_engine()
    game = engine.active_game
    assert game is not None
    controller = controller_for_engine(controller_storage, engine)
    await controller.start()

    controller.post_system_event(
        MemberDisconnectedEvent(
            GAME_IDS[0], "songmaster-token", "songmaster-connection"
        )
    )
    await controller.submit_request(
        RegisterConnectionRequest(
            GAME_IDS[0], "songmaster-token", "songmaster-connection"
        )
    )

    assert game.songmaster.connected is True
    assert controller.connection_registry.is_current(
        GAME_IDS[0], "songmaster-token", "songmaster-connection"
    )
    assert len(controller.system_event_rejections) == 1
    assert isinstance(
        controller.system_event_rejections[0].error,
        SongmasterDisconnectRequiresFailoverError,
    )
    await controller.stop()


@pytest.mark.asyncio
async def test_unknown_registration_and_cancel_cleanup_do_not_leak_to_new_game(
    controller_storage: tuple[Engine, Path],
) -> None:
    database_engine, music_root = controller_storage
    engine = GameEngine(
        clock=AdvancingClock(),
        game_id_factory=ValueFactory(GAME_IDS),
    )
    controller = GameController(database_engine, music_root, game_engine=engine)
    await controller.start()
    first = await controller.submit_request(
        CreateGameRequest("reused-token", "First", "old-connection")
    )
    with pytest.raises(UnknownGameMemberError):
        await controller.submit_request(
            RegisterConnectionRequest(
                first.game_session_id, "unknown-token", "intruder-connection"
            )
        )
    await controller.submit_request(
        CancelGameRequest(first.game_session_id, "reused-token")
    )
    assert not controller.connection_registry.is_current(
        first.game_session_id, "reused-token", "old-connection"
    )

    second = await controller.submit_request(
        CreateGameRequest("reused-token", "Second", "new-connection")
    )
    controller.post_system_event(
        BrowserAudioCapabilityChangedEvent(
            first.game_session_id,
            "reused-token",
            "old-connection",
            True,
        )
    )
    await controller.submit_request(
        RegisterConnectionRequest(
            second.game_session_id, "reused-token", "new-connection"
        )
    )

    assert second.songmaster.display_name == "Second"
    assert second.songmaster.committed_score == 0
    assert second.songmaster.browser_audio_usable is False
    assert len(controller.system_event_rejections) == 1
    await controller.stop()

