import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import Engine

from who_sings_that.application import (
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
    RegisterConnectionRequest,
    StaleConnectionError,
)
from who_sings_that.database import create_sqlite_engine, upgrade_database
from who_sings_that.domain import (
    BuzzNotAllowedError,
    GameEndReason,
    GameEngine,
    GameSettings,
    GameState,
    MembershipOperationNotAllowedError,
    PreparationEventInapplicableError,
    ResolvedPlaylist,
    RoundTerminationReason,
    RoundVoidReason,
    SelectedPlaylistSnapshot,
    SongmasterRecoveryResolution,
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


def test_new_member_joining_round_complete_is_eligible_at_next_transition_c() -> None:
    engine, _ = started_engine()

    member = engine.join_game("late-token", "Late Player")
    pending = engine.play_song("songmaster-token")

    assert member.connected is True
    assert "late-token" in pending.buzz_eligible_browser_tokens


def test_join_during_preparation_stays_ineligible_until_subsequent_transition_c(
) -> None:
    engine, monotonic_clock = started_engine()
    pending = engine.play_song("songmaster-token")
    late_member = engine.join_game("late-token", "Late Player")

    assert "late-token" not in pending.buzz_eligible_browser_tokens
    engine.record_audio_preparation_ready(
        GAME_IDS[0], pending.preparation_id, "songmaster-token"
    )
    game = engine.active_game
    assert game is not None and game.active_round is not None
    first_round = game.active_round
    monotonic_clock.value = first_round.playback_started_monotonic + 1

    with pytest.raises(BuzzNotAllowedError, match="not eligible"):
        engine.accept_buzz(
            GAME_IDS[0],
            first_round.round_id,
            late_member.browser_token,
            monotonic_clock.value,
        )

    monotonic_clock.value = first_round.playback_deadline_monotonic
    engine.playback_timed_out(
        GAME_IDS[0], first_round.round_id, monotonic_clock.value
    )
    engine.reveal_answer(
        GAME_IDS[0], first_round.round_id, "songmaster-token"
    )
    next_pending = engine.play_song("songmaster-token")

    assert "late-token" in next_pending.buzz_eligible_browser_tokens


def test_join_is_rejected_after_game_is_finished() -> None:
    engine, _ = started_engine()
    engine.end_game(GAME_IDS[0], "songmaster-token")

    with pytest.raises(MembershipOperationNotAllowedError):
        engine.join_game("late-token", "Late Player")


@pytest.mark.asyncio
async def test_fifo_disconnect_reconnect_stale_events_then_current_connection_buzz_wins(
    controller_storage: tuple[Engine, Path],
) -> None:
    """Exercise reconnect-then-buzz through the one authoritative FIFO."""

    engine, monotonic_clock = started_engine()
    engine.set_audio_preference("player-token", True)
    pending = engine.play_song("songmaster-token")
    engine.record_audio_preparation_ready(
        GAME_IDS[0], pending.preparation_id, "songmaster-token"
    )
    engine.set_browser_audio_capability("player-token", True)
    game = engine.active_game
    assert game is not None and game.active_round is not None
    active_round = game.active_round
    original_eligibility = active_round.buzz_eligible_browser_tokens
    player = game.members["player-token"]
    player.committed_score = 77
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
    assert player.connected is False
    assert engine.state is GameState.PLAYING
    with pytest.raises(StaleConnectionError):
        await controller.submit_request(
            BuzzRequest(
                GAME_IDS[0],
                active_round.round_id,
                "player-token",
                "player-connection-a",
            )
        )

    reconnection = await controller.submit_request(
        RegisterConnectionRequest(
            GAME_IDS[0], "player-token", "player-connection-b"
        )
    )
    assert reconnection.status is ConnectionRegistrationStatus.REPLACED
    assert player.connected is True
    assert player.display_name == "Player"
    assert player.committed_score == 77
    assert game.songmaster_browser_token == "songmaster-token"
    assert player.audio_enabled is True
    assert player.browser_audio_usable is False
    assert game.active_round is active_round
    assert active_round.buzz_eligible_browser_tokens is original_eligibility

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
    assert active_round.buzz_eligible_browser_tokens is original_eligibility

    with pytest.raises(StaleConnectionError):
        await controller.submit_request(
            BuzzRequest(
                GAME_IDS[0],
                active_round.round_id,
                "player-token",
                "player-connection-a",
            )
        )

    monotonic_clock.value = active_round.playback_started_monotonic + 1

    termination = await controller.submit_request(
        BuzzRequest(
            GAME_IDS[0],
            active_round.round_id,
            "player-token",
            "player-connection-b",
        )
    )
    assert termination.reason is RoundTerminationReason.BUZZ
    assert termination.winning_browser_token == "player-token"
    assert engine.state is GameState.AWAITING_REVEAL
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
async def test_buzz_only_contestant_disconnect_does_not_void_playing_round(
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

    assert len(controller.system_event_rejections) == 2
    assert isinstance(controller.system_event_rejections[0].error, StaleConnectionError)
    assert isinstance(
        controller.system_event_rejections[1].error,
        PreparationEventInapplicableError,
    )
    assert engine.state is GameState.ROUND_COMPLETE
    await controller.stop()


@pytest.mark.asyncio
async def test_songmaster_disconnect_and_timely_reconnect_restore_authority(
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
    assert game.songmaster_recovery is None
    assert game.last_songmaster_recovery_outcome is not None
    assert (
        game.last_songmaster_recovery_outcome.resolution
        is SongmasterRecoveryResolution.ORIGINAL_RECONNECTED
    )
    assert controller.connection_registry.is_current(
        GAME_IDS[0], "songmaster-token", "songmaster-connection"
    )
    assert controller.system_event_rejections == ()
    assert controller.songmaster_reconnect_grace_active is False
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


def contestant_playback_engine() -> tuple[GameEngine, MutableMonotonicClock]:
    engine, monotonic_clock = started_engine()
    engine.set_audio_preference("songmaster-token", False)
    engine.set_audio_preference("player-token", True)
    engine.set_browser_audio_capability("player-token", True)
    pending = engine.play_song("songmaster-token")
    engine.record_audio_preparation_ready(
        GAME_IDS[0], pending.preparation_id, "player-token"
    )
    return engine, monotonic_clock


def test_domain_playback_client_disconnect_voids_and_consumes_once() -> None:
    engine, _ = contestant_playback_engine()
    game = engine.active_game
    assert game is not None and game.active_round is not None
    active_round = game.active_round
    prior_scores = {
        token: member.committed_score for token, member in game.members.items()
    }

    member = engine.disconnect_member("player-token")

    assert member.connected is False
    assert engine.state is GameState.ROUND_COMPLETE
    assert game.deck_pointer == 1
    assert game.active_round is None
    assert game.buzzing_enabled is False
    assert game.last_voided_round is not None
    assert game.last_voided_round.active_round is active_round
    assert game.last_voided_round.reason is RoundVoidReason.PLAYBACK_CLIENT_LOST
    assert (
        game.last_voided_round.lost_playback_client_browser_token
        == "player-token"
    )
    assert game.last_voided_round.termination is None
    assert game.last_voided_round.answer_revealed is False
    assert {
        token: member.committed_score for token, member in game.members.items()
    } == prior_scores

    engine.record_connection_established("player-token", connection_changed=True)
    assert engine.state is GameState.ROUND_COMPLETE
    assert game.deck_pointer == 1
    assert game.active_round is None


@pytest.mark.asyncio
async def test_playback_client_replacement_voids_and_rejects_predecessor(
    controller_storage: tuple[Engine, Path],
) -> None:
    engine, _ = contestant_playback_engine()
    game = engine.active_game
    assert game is not None and game.active_round is not None
    active_round = game.active_round
    player = game.members["player-token"]
    player.committed_score = 41
    controller = controller_for_engine(controller_storage, engine)
    controller._schedule_playback_timeout(
        GAME_IDS[0], active_round.round_id, active_round.playback_deadline_monotonic
    )
    timer = controller._playback_timeout_task
    await controller.start()

    replacement = await controller.submit_request(
        RegisterConnectionRequest(
            GAME_IDS[0], "player-token", "player-connection-b"
        )
    )
    await asyncio.sleep(0)

    assert replacement.status is ConnectionRegistrationStatus.REPLACED
    assert controller.connection_registry.is_current(
        GAME_IDS[0], "player-token", "player-connection-b"
    )
    assert engine.state is GameState.ROUND_COMPLETE
    assert game.deck_pointer == 1
    assert player.connected is True
    assert player.audio_enabled is True
    assert player.browser_audio_usable is False
    assert player.committed_score == 41
    assert timer is not None and timer.done()
    assert controller._playback_timeout_task is None

    controller.post_system_event(
        MemberDisconnectedEvent(
            GAME_IDS[0], "player-token", "player-connection-a"
        )
    )
    duplicate = await controller.submit_request(
        RegisterConnectionRequest(
            GAME_IDS[0], "player-token", "player-connection-b"
        )
    )
    assert duplicate.status is ConnectionRegistrationStatus.UNCHANGED
    assert game.deck_pointer == 1
    assert len(controller.system_event_rejections) == 1
    assert isinstance(
        controller.system_event_rejections[0].error, StaleConnectionError
    )
    await controller.stop()


@pytest.mark.asyncio
async def test_playback_client_duplicate_disconnect_consumes_only_once(
    controller_storage: tuple[Engine, Path],
) -> None:
    engine, _ = contestant_playback_engine()
    game = engine.active_game
    assert game is not None and game.active_round is not None
    controller = controller_for_engine(controller_storage, engine)
    controller._schedule_playback_timeout(
        GAME_IDS[0],
        game.active_round.round_id,
        game.active_round.playback_deadline_monotonic,
    )
    timer = controller._playback_timeout_task
    await controller.start()

    loss = MemberDisconnectedEvent(
        GAME_IDS[0], "player-token", "player-connection-a"
    )
    controller.post_system_event(loss)
    controller.post_system_event(loss)
    await controller.submit_request(
        RegisterConnectionRequest(
            GAME_IDS[0], "songmaster-token", "songmaster-connection"
        )
    )
    await asyncio.sleep(0)

    assert engine.state is GameState.ROUND_COMPLETE
    assert game.deck_pointer == 1
    assert timer is not None and timer.done()
    assert controller._playback_timeout_task is None
    assert len(controller.system_event_rejections) == 1
    assert isinstance(
        controller.system_event_rejections[0].error, StaleConnectionError
    )
    await controller.stop()


def test_playback_loss_restores_previous_completed_song_display() -> None:
    engine, monotonic_clock = contestant_playback_engine()
    game = engine.active_game
    assert game is not None and game.active_round is not None
    first_round = game.active_round
    monotonic_clock.value = first_round.playback_deadline_monotonic
    engine.playback_timed_out(
        GAME_IDS[0], first_round.round_id, monotonic_clock.value
    )
    engine.reveal_answer(GAME_IDS[0], first_round.round_id, "songmaster-token")
    expected_display = (1, "Artist One", "Title One")
    assert (
        game.displayed_deck_position,
        game.displayed_artist,
        game.displayed_title,
    ) == expected_display

    pending = engine.play_song("songmaster-token")
    engine.record_audio_preparation_ready(
        GAME_IDS[0], pending.preparation_id, "player-token"
    )
    assert game.displayed_deck_position == 2
    assert game.displayed_artist is None
    assert game.displayed_title is None

    engine.disconnect_member("player-token")

    assert engine.state is GameState.ROUND_COMPLETE
    assert (
        game.displayed_deck_position,
        game.displayed_artist,
        game.displayed_title,
    ) == expected_display
    assert game.last_voided_round is not None
    assert game.last_voided_round.answer_revealed is False


def test_final_song_playback_loss_waits_for_explicit_transition_k() -> None:
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
    engine.set_audio_preference("songmaster-token", False)
    engine.set_audio_preference("player-token", True)
    engine.set_browser_audio_capability("player-token", True)
    engine.select_playlist("songmaster-token", "playlist")
    playlist = resolved_playlist()
    engine.start_game(
        "songmaster-token",
        [
            ResolvedPlaylist(
                snapshot=SelectedPlaylistSnapshot(
                    playlist_id=playlist.snapshot.playlist_id,
                    playlist_name=playlist.snapshot.playlist_name,
                    playlist_created_at=playlist.snapshot.playlist_created_at,
                    playlist_song_count=1,
                ),
                tracks=playlist.tracks[:1],
            )
        ],
    )
    pending = engine.play_song("songmaster-token")
    engine.record_audio_preparation_ready(
        GAME_IDS[0], pending.preparation_id, "player-token"
    )

    engine.disconnect_member("player-token")
    game = engine.active_game
    assert game is not None
    assert engine.state is GameState.ROUND_COMPLETE
    assert game.deck_pointer == 1
    assert game.deck_exhausted is True
    assert game.end_reason is None
    assert game.final_standings == ()

    result = engine.play_song("songmaster-token")
    assert result.end_reason is GameEndReason.DECK_EXHAUSTED
    assert engine.state is GameState.FINISHED


@pytest.mark.asyncio
async def test_fifo_playback_loss_before_buzz_rejects_later_buzz(
    controller_storage: tuple[Engine, Path],
) -> None:
    engine, monotonic_clock = contestant_playback_engine()
    game = engine.active_game
    assert game is not None and game.active_round is not None
    active_round = game.active_round
    monotonic_clock.value = active_round.playback_started_monotonic + 1
    controller = controller_for_engine(controller_storage, engine)
    await controller.start()

    controller.post_system_event(
        MemberDisconnectedEvent(
            GAME_IDS[0], "player-token", "player-connection-a"
        )
    )
    with pytest.raises(StaleConnectionError):
        await controller.submit_request(
            BuzzRequest(
                GAME_IDS[0],
                active_round.round_id,
                "player-token",
                "player-connection-a",
            )
        )

    assert engine.state is GameState.ROUND_COMPLETE
    assert game.deck_pointer == 1
    await controller.stop()


@pytest.mark.asyncio
async def test_fifo_buzz_before_playback_loss_prevents_retroactive_void(
    controller_storage: tuple[Engine, Path],
) -> None:
    engine, monotonic_clock = contestant_playback_engine()
    game = engine.active_game
    assert game is not None and game.active_round is not None
    active_round = game.active_round
    monotonic_clock.value = active_round.playback_started_monotonic + 1
    controller = controller_for_engine(controller_storage, engine)
    await controller.start()

    termination = await controller.submit_request(
        BuzzRequest(
            GAME_IDS[0],
            active_round.round_id,
            "player-token",
            "player-connection-a",
        )
    )
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

    assert termination.reason is RoundTerminationReason.BUZZ
    assert engine.state is GameState.AWAITING_REVEAL
    assert game.deck_pointer == 0
    assert game.last_voided_round is None
    assert game.members["player-token"].connected is False
    await controller.stop()


@pytest.mark.asyncio
async def test_fifo_playback_loss_before_timeout_makes_timeout_stale(
    controller_storage: tuple[Engine, Path],
) -> None:
    engine, monotonic_clock = contestant_playback_engine()
    game = engine.active_game
    assert game is not None and game.active_round is not None
    active_round = game.active_round
    monotonic_clock.value = active_round.playback_deadline_monotonic
    controller = controller_for_engine(controller_storage, engine)
    await controller.start()

    controller.post_system_event(
        MemberDisconnectedEvent(
            GAME_IDS[0], "player-token", "player-connection-a"
        )
    )
    controller.post_system_event(
        PlaybackTimeoutEvent(GAME_IDS[0], active_round.round_id)
    )
    await controller.submit_request(
        RegisterConnectionRequest(
            GAME_IDS[0], "songmaster-token", "songmaster-connection"
        )
    )

    assert engine.state is GameState.ROUND_COMPLETE
    assert game.deck_pointer == 1
    assert len(controller.system_event_rejections) == 1
    await controller.stop()


@pytest.mark.asyncio
async def test_fifo_timeout_before_playback_loss_prevents_retroactive_void(
    controller_storage: tuple[Engine, Path],
) -> None:
    engine, monotonic_clock = contestant_playback_engine()
    game = engine.active_game
    assert game is not None and game.active_round is not None
    active_round = game.active_round
    monotonic_clock.value = active_round.playback_deadline_monotonic
    controller = controller_for_engine(controller_storage, engine)
    await controller.start()

    controller.post_system_event(
        PlaybackTimeoutEvent(GAME_IDS[0], active_round.round_id)
    )
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

    assert engine.state is GameState.AWAITING_REVEAL
    assert game.deck_pointer == 0
    assert game.last_voided_round is None
    assert game.members["player-token"].connected is False
    await controller.stop()

