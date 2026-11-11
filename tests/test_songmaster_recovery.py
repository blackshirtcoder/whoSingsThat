import asyncio
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import Engine

from who_sings_that.application import (
    AudioPreparationReadyEvent,
    BrowserAudioCapabilityChangedEvent,
    CloseGameRequest,
    CreateGameRequest,
    EndGameRequest,
    GameController,
    JoinGameRequest,
    MemberDisconnectedEvent,
    PlaybackTimeoutEvent,
    PlaySongRequest,
    RegisterConnectionRequest,
    SongmasterReconnectGraceExpiredEvent,
    StaleConnectionError,
    TransferSongmasterRequest,
)
from who_sings_that.database import create_sqlite_engine
from who_sings_that.domain import (
    GameEngine,
    GameSettings,
    GameState,
    GameSuspendedForSongmasterRecoveryError,
    ResolvedPlaylist,
    RoundVoidReason,
    SelectedPlaylistSnapshot,
    SongmasterRecoveryInapplicableError,
    SongmasterRecoveryResolution,
    SongmasterRecoveryStatus,
    SongmasterTransferNotAllowedError,
    TrackReference,
)

GAME_ID = UUID(int=4101)
PREPARATION_IDS = tuple(UUID(int=value) for value in range(4201, 4210))
ROUND_IDS = tuple(UUID(int=value) for value in range(4301, 4310))
RECOVERY_IDS = tuple(UUID(int=value) for value in range(4401, 4410))


class MutableMonotonicClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class ValueFactory:
    def __init__(self, values: tuple[UUID, ...]) -> None:
        self.values = iter(values)

    def __call__(self) -> UUID:
        return next(self.values)


class NeverSleep:
    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def __call__(self, _: float) -> None:
        self.started.set()
        await asyncio.Event().wait()


@pytest.fixture
def storage(tmp_path: Path) -> tuple[Engine, Path]:
    engine = create_sqlite_engine(tmp_path / "recovery.sqlite3")
    music_root = tmp_path / "music"
    music_root.mkdir()
    return engine, music_root


def playlist() -> ResolvedPlaylist:
    tracks = tuple(
        TrackReference(index, 180, f"Artist {index}", f"Title {index}")
        for index in range(1, 5)
    )
    return ResolvedPlaylist(
        snapshot=SelectedPlaylistSnapshot(
            playlist_id="playlist",
            playlist_name="Recovery",
            playlist_created_at=datetime(2026, 1, 1, tzinfo=UTC),
            playlist_song_count=len(tracks),
        ),
        tracks=tracks,
    )


def game_engine(
    *,
    started: bool = True,
    contestants: tuple[str, ...] = ("p1", "p2"),
    game_ids: tuple[UUID, ...] = (GAME_ID,),
) -> tuple[GameEngine, MutableMonotonicClock]:
    monotonic_clock = MutableMonotonicClock()
    engine = GameEngine(
        GameSettings(playback_duration_seconds=30),
        clock=lambda: datetime(2026, 11, 11, 12, 0, tzinfo=UTC),
        monotonic_clock=monotonic_clock,
        game_id_factory=ValueFactory(game_ids),
        preparation_id_factory=ValueFactory(PREPARATION_IDS),
        round_id_factory=ValueFactory(ROUND_IDS),
        recovery_id_factory=ValueFactory(RECOVERY_IDS),
        shuffle=lambda values: None,
    )
    engine.create_game("sm", "Songmaster")
    for index, token in enumerate(contestants, start=1):
        engine.join_game(token, f"Player {index}")
    if started:
        engine.select_playlist("sm", "playlist")
        engine.start_game("sm", (playlist(),))
    return engine, monotonic_clock


def enter_state(engine: GameEngine, state: GameState) -> None:
    if state is GameState.LOBBY:
        return
    if state is GameState.ROUND_COMPLETE:
        return
    if state is GameState.FINISHED:
        game = engine.active_game
        assert game is not None
        engine.end_game(game.game_session_id, "sm")
        return
    engine.set_browser_audio_capability("sm", True)
    pending = engine.play_song("sm")
    if state is GameState.AUDIO_PREPARING:
        return
    engine.record_audio_preparation_ready(
        GAME_ID,
        pending.preparation_id,
        "sm",
    )
    if state is GameState.PLAYING:
        return
    game = engine.active_game
    assert game is not None and game.active_round is not None
    engine.accept_buzz(
        GAME_ID,
        game.active_round.round_id,
        "p1",
        game.active_round.playback_started_monotonic + 1,
    )
    if state is GameState.AWAITING_REVEAL:
        return
    assert game.active_round is not None
    engine.reveal_answer(GAME_ID, game.active_round.round_id, "sm")


@pytest.mark.parametrize(
    ("source_state", "started"),
    [
        (GameState.LOBBY, False),
        (GameState.ROUND_COMPLETE, True),
        (GameState.AUDIO_PREPARING, True),
        (GameState.PLAYING, True),
        (GameState.AWAITING_REVEAL, True),
        (GameState.SCORING, True),
        (GameState.FINISHED, True),
    ],
)
def test_songmaster_loss_normalizes_every_source_state(
    source_state: GameState,
    started: bool,
) -> None:
    engine, _ = game_engine(started=started)
    game = engine.active_game
    assert game is not None
    game.members["p1"].committed_score = 37
    enter_state(engine, source_state)
    pointer_before = game.deck_pointer
    final_standings_before = game.final_standings
    pending_before = game.pending_audio_preparation
    active_round_before = game.active_round

    recovery = engine.begin_songmaster_recovery("sm", 100.0, 110.0)

    expected_state = (
        source_state
        if source_state in {GameState.LOBBY, GameState.ROUND_COMPLETE, GameState.FINISHED}
        else GameState.ROUND_COMPLETE
    )
    assert engine.state is expected_state
    assert recovery.interrupted_state is source_state
    assert recovery.normalized_state is expected_state
    assert recovery.status is SongmasterRecoveryStatus.RECONNECT_GRACE
    assert game.songmaster_browser_token == "sm"
    assert game.songmaster.connected is False
    assert game.members["p1"].committed_score == 37
    assert game.active_round is None
    assert game.pending_audio_preparation is None
    assert game.audio_preparation_responses is None
    assert game.buzzing_enabled is False

    if source_state is GameState.AUDIO_PREPARING:
        assert pending_before is not None
        assert game.deck_pointer == pointer_before
        assert game.last_voided_round is None
        assert recovery.preparation_discarded is True
    elif source_state in {
        GameState.PLAYING,
        GameState.AWAITING_REVEAL,
        GameState.SCORING,
    }:
        assert active_round_before is not None
        assert game.deck_pointer == pointer_before + 1
        assert game.last_voided_round is not None
        assert game.last_voided_round.active_round is active_round_before
        assert (
            game.last_voided_round.reason
            is RoundVoidReason.SONGMASTER_DISCONNECTED
        )
        assert game.last_voided_round.answer_revealed is (
            source_state is GameState.SCORING
        )
    else:
        assert game.deck_pointer == pointer_before

    if source_state is GameState.FINISHED:
        assert game.final_standings == final_standings_before
    engine.validate_invariants()


def test_songmaster_loss_when_not_a_playback_client_voids_once() -> None:
    engine, _ = game_engine()
    engine.set_audio_preference("sm", False)
    engine.set_audio_preference("p1", True)
    engine.set_browser_audio_capability("p1", True)
    pending = engine.play_song("sm")
    engine.record_audio_preparation_ready(GAME_ID, pending.preparation_id, "p1")
    game = engine.active_game
    assert game is not None and game.active_round is not None
    assert game.active_round.playback_clients == frozenset({"p1"})

    engine.begin_songmaster_recovery("sm", 100.0, 110.0)

    assert game.deck_pointer == 1
    assert game.last_voided_round is not None
    assert game.last_voided_round.reason is RoundVoidReason.SONGMASTER_DISCONNECTED


def test_playing_loss_restores_previous_completed_answer_without_leaking_void() -> None:
    engine, clock = game_engine()
    engine.set_browser_audio_capability("sm", True)
    first = engine.play_song("sm")
    engine.record_audio_preparation_ready(GAME_ID, first.preparation_id, "sm")
    game = engine.active_game
    assert game is not None and game.active_round is not None
    clock.value = game.active_round.playback_deadline_monotonic
    engine.playback_timed_out(GAME_ID, game.active_round.round_id, clock.value)
    assert game.active_round is not None
    engine.reveal_answer(GAME_ID, game.active_round.round_id, "sm")
    previous_display = (
        game.displayed_deck_position,
        game.displayed_artist,
        game.displayed_title,
    )
    second = engine.play_song("sm")
    engine.record_audio_preparation_ready(GAME_ID, second.preparation_id, "sm")

    engine.begin_songmaster_recovery("sm", 200.0, 210.0)

    assert (
        game.displayed_deck_position,
        game.displayed_artist,
        game.displayed_title,
    ) == previous_display
    assert game.last_voided_round is not None
    voided_track = game.last_voided_round.active_round.deck_entry.track
    assert game.displayed_artist != voided_track.artist
    assert game.displayed_title != voided_track.title


def test_grace_suspends_gameplay_and_timely_reconnect_preserves_role() -> None:
    engine, _ = game_engine()
    game = engine.active_game
    assert game is not None
    recovery = engine.begin_songmaster_recovery("sm", 100.0, 110.0)

    with pytest.raises(GameSuspendedForSongmasterRecoveryError):
        engine.play_song("sm")
    outcome = engine.recover_original_songmaster("sm", 109.999)

    assert outcome.recovery_id == recovery.recovery_id
    assert outcome.resolution is SongmasterRecoveryResolution.ORIGINAL_RECONNECTED
    assert game.songmaster_browser_token == "sm"
    assert game.songmaster.connected is True
    assert game.songmaster.browser_audio_usable is False
    assert game.songmaster_recovery is None
    engine.validate_invariants()


def test_manual_songmaster_transfer_remains_limited_to_stable_states() -> None:
    lobby_engine, _ = game_engine(started=False)
    lobby = lobby_engine.active_game
    assert lobby is not None
    lobby_engine.transfer_songmaster("sm", "p1")
    assert lobby.songmaster_browser_token == "p1"

    round_complete_engine, _ = game_engine()
    game = round_complete_engine.active_game
    assert game is not None
    round_complete_engine.transfer_songmaster("sm", "p1")
    assert game.songmaster_browser_token == "p1"

    playing_engine, _ = game_engine()
    playing_engine.set_browser_audio_capability("sm", True)
    pending = playing_engine.play_song("sm")
    playing_engine.record_audio_preparation_ready(
        GAME_ID,
        pending.preparation_id,
        "sm",
    )
    with pytest.raises(SongmasterTransferNotAllowedError):
        playing_engine.transfer_songmaster("sm", "p1")

    suspended_engine, _ = game_engine()
    suspended_engine.begin_songmaster_recovery("sm", 100.0, 110.0)
    with pytest.raises(GameSuspendedForSongmasterRecoveryError):
        suspended_engine.transfer_songmaster("sm", "p1")


def controller_for(
    storage: tuple[Engine, Path],
    engine: GameEngine,
    sleep: NeverSleep,
) -> GameController:
    database_engine, music_root = storage
    controller = GameController(
        database_engine,
        music_root,
        game_engine=engine,
        songmaster_reconnect_grace_sleep=sleep,
    )
    controller.connection_registry.register(GAME_ID, "sm", "sm-a")
    game = engine.active_game
    assert game is not None
    for token in game.members:
        if token != "sm":
            controller.connection_registry.register(GAME_ID, token, f"{token}-a")
    return controller


async def disconnect_songmaster(controller: GameController) -> UUID:
    controller.post_system_event(MemberDisconnectedEvent(GAME_ID, "sm", "sm-a"))
    await controller._queue.join()
    game = controller.game_engine.active_game
    assert game is not None and game.songmaster_recovery is not None
    return game.songmaster_recovery.recovery_id


@pytest.mark.asyncio
async def test_controller_grace_reconnect_retires_timer_and_rejects_old_expiry(
    storage: tuple[Engine, Path],
) -> None:
    engine, clock = game_engine()
    sleep = NeverSleep()
    controller = controller_for(storage, engine, sleep)
    await controller.start()
    recovery_id = await disconnect_songmaster(controller)
    assert controller.songmaster_reconnect_grace_active

    clock.value = 109.0
    await controller.submit_request(
        RegisterConnectionRequest(GAME_ID, "sm", "sm-b")
    )
    game = engine.active_game
    assert game is not None
    assert game.songmaster_browser_token == "sm"
    assert game.songmaster_recovery is None
    assert controller.songmaster_reconnect_grace_active is False

    clock.value = 111.0
    controller.post_system_event(
        SongmasterReconnectGraceExpiredEvent(GAME_ID, recovery_id)
    )
    await controller._queue.join()
    assert isinstance(
        controller.system_event_rejections[-1].error,
        SongmasterRecoveryInapplicableError,
    )
    assert game.songmaster_browser_token == "sm"
    await controller.stop()


@pytest.mark.asyncio
async def test_controller_routes_manual_transfer_only_outside_recovery(
    storage: tuple[Engine, Path],
) -> None:
    engine, _ = game_engine()
    controller = controller_for(storage, engine, NeverSleep())
    await controller.start()
    recipient = await controller.submit_request(
        TransferSongmasterRequest(GAME_ID, "sm", "p1")
    )
    game = engine.active_game
    assert game is not None
    assert recipient.browser_token == "p1"
    assert game.songmaster_browser_token == "p1"
    await controller.stop()


@pytest.mark.asyncio
async def test_longest_continuously_connected_contestant_is_promoted(
    storage: tuple[Engine, Path],
) -> None:
    engine, clock = game_engine()
    sleep = NeverSleep()
    controller = controller_for(storage, engine, sleep)
    await controller.start()
    # Replacing p1 makes p2's unchanged connection the longest continuous one.
    await controller.submit_request(
        RegisterConnectionRequest(GAME_ID, "p1", "p1-b")
    )
    recovery_id = await disconnect_songmaster(controller)

    clock.value = 110.0
    controller.post_system_event(
        SongmasterReconnectGraceExpiredEvent(GAME_ID, recovery_id)
    )
    await controller._queue.join()
    game = engine.active_game
    assert game is not None
    assert game.songmaster_browser_token == "p2"
    assert game.songmaster_recovery is None
    assert game.last_songmaster_recovery_outcome is not None
    assert (
        game.last_songmaster_recovery_outcome.resolution
        is SongmasterRecoveryResolution.AUTOMATIC_FAILOVER
    )
    assert controller.songmaster_reconnect_grace_active is False

    await controller.submit_request(
        RegisterConnectionRequest(GAME_ID, "sm", "sm-late")
    )
    assert game.songmaster_browser_token == "p2"
    assert game.members["sm"].connected is True
    controller.post_system_event(
        SongmasterReconnectGraceExpiredEvent(GAME_ID, recovery_id)
    )
    await controller._queue.join()
    assert isinstance(
        controller.system_event_rejections[-1].error,
        SongmasterRecoveryInapplicableError,
    )
    assert game.songmaster_browser_token == "p2"
    await controller.stop()


@pytest.mark.asyncio
async def test_exact_deadline_reconnect_loses_privileged_claim(
    storage: tuple[Engine, Path],
) -> None:
    engine, clock = game_engine()
    controller = controller_for(storage, engine, NeverSleep())
    await controller.start()
    await disconnect_songmaster(controller)

    clock.value = 110.0
    await controller.submit_request(
        RegisterConnectionRequest(GAME_ID, "sm", "sm-at-deadline")
    )

    game = engine.active_game
    assert game is not None
    assert game.songmaster_browser_token == "p1"
    assert game.members["sm"].connected is True
    assert game.songmaster_recovery is None
    await controller.stop()


@pytest.mark.asyncio
async def test_zero_connected_expiry_suspends_then_first_new_join_resumes(
    storage: tuple[Engine, Path],
) -> None:
    engine, clock = game_engine(contestants=())
    controller = controller_for(storage, engine, NeverSleep())
    await controller.start()
    recovery_id = await disconnect_songmaster(controller)

    clock.value = 110.0
    controller.post_system_event(
        SongmasterReconnectGraceExpiredEvent(GAME_ID, recovery_id)
    )
    await controller._queue.join()
    game = engine.active_game
    assert game is not None and game.songmaster_recovery is not None
    assert (
        game.songmaster_recovery.status
        is SongmasterRecoveryStatus.INDEFINITE_SUSPENSION
    )
    assert engine.state is GameState.ROUND_COMPLETE
    assert controller.songmaster_reconnect_grace_active is False

    member = await controller.submit_request(
        JoinGameRequest(GAME_ID, "new", "new-a", "New Player")
    )
    assert member.browser_token == "new"
    assert game.songmaster_browser_token == "new"
    assert game.songmaster_recovery is None
    assert game.last_songmaster_recovery_outcome is not None
    assert (
        game.last_songmaster_recovery_outcome.resolution
        is SongmasterRecoveryResolution.RESUMED_AFTER_INDEFINITE_SUSPENSION
    )
    await controller.stop()


@pytest.mark.asyncio
async def test_solo_game_can_resume_after_indefinite_suspension(
    storage: tuple[Engine, Path],
) -> None:
    engine, clock = game_engine(contestants=())
    controller = controller_for(storage, engine, NeverSleep())
    await controller.start()
    recovery_id = await disconnect_songmaster(controller)
    clock.value = 110.0
    controller.post_system_event(
        SongmasterReconnectGraceExpiredEvent(GAME_ID, recovery_id)
    )
    await controller._queue.join()

    await controller.submit_request(
        JoinGameRequest(GAME_ID, "sm", "sm-returned")
    )
    game = engine.active_game
    assert game is not None
    assert game.songmaster_browser_token == "sm"
    assert game.songmaster.connected
    assert game.songmaster_recovery is None
    await controller.stop()


@pytest.mark.asyncio
async def test_songmaster_replacement_normalizes_without_wait_and_stale_loss_is_ignored(
    storage: tuple[Engine, Path],
) -> None:
    engine, _ = game_engine()
    engine.set_browser_audio_capability("sm", True)
    pending = engine.play_song("sm")
    engine.record_audio_preparation_ready(GAME_ID, pending.preparation_id, "sm")
    controller = controller_for(storage, engine, NeverSleep())
    await controller.start()

    await controller.submit_request(
        RegisterConnectionRequest(GAME_ID, "sm", "sm-b")
    )
    game = engine.active_game
    assert game is not None
    assert engine.state is GameState.ROUND_COMPLETE
    assert game.deck_pointer == 1
    assert game.last_voided_round is not None
    assert game.last_voided_round.reason is RoundVoidReason.SONGMASTER_DISCONNECTED
    assert game.songmaster_browser_token == "sm"
    assert game.songmaster_recovery is None
    assert controller.songmaster_reconnect_grace_active is False

    controller.post_system_event(MemberDisconnectedEvent(GAME_ID, "sm", "sm-a"))
    await controller._queue.join()
    assert isinstance(
        controller.system_event_rejections[-1].error,
        StaleConnectionError,
    )
    assert game.deck_pointer == 1
    await controller.stop()


@pytest.mark.asyncio
async def test_disconnect_retires_preparation_timer_and_old_ready_is_stale(
    storage: tuple[Engine, Path],
) -> None:
    engine, _ = game_engine()
    controller = controller_for(storage, engine, NeverSleep())
    await controller.start()
    controller.post_system_event(
        BrowserAudioCapabilityChangedEvent(GAME_ID, "sm", "sm-a", True)
    )
    await controller._queue.join()
    pending = await controller.submit_request(PlaySongRequest(GAME_ID, "sm"))
    assert controller.preparation_timeout_active

    recovery_id = await disconnect_songmaster(controller)
    assert controller.preparation_timeout_active is False
    assert controller.songmaster_reconnect_grace_active
    controller.post_system_event(
        AudioPreparationReadyEvent(
            GAME_ID,
            pending.preparation_id,
            "sm",
            "sm-a",
        )
    )
    await controller._queue.join()
    game = engine.active_game
    assert game is not None and game.songmaster_recovery is not None
    assert game.songmaster_recovery.recovery_id == recovery_id
    assert engine.state is GameState.ROUND_COMPLETE
    await controller.stop()


@pytest.mark.asyncio
async def test_disconnect_retires_playback_timer_and_old_timeout_is_stale(
    storage: tuple[Engine, Path],
) -> None:
    engine, _ = game_engine()
    controller = controller_for(storage, engine, NeverSleep())
    await controller.start()
    controller.post_system_event(
        BrowserAudioCapabilityChangedEvent(GAME_ID, "sm", "sm-a", True)
    )
    await controller._queue.join()
    pending = await controller.submit_request(PlaySongRequest(GAME_ID, "sm"))
    controller.post_system_event(
        AudioPreparationReadyEvent(
            GAME_ID,
            pending.preparation_id,
            "sm",
            "sm-a",
        )
    )
    await controller._queue.join()
    game = engine.active_game
    assert game is not None and game.active_round is not None
    round_id = game.active_round.round_id
    assert controller.playback_timeout_active

    await disconnect_songmaster(controller)
    assert controller.playback_timeout_active is False
    assert game.deck_pointer == 1
    controller.post_system_event(PlaybackTimeoutEvent(GAME_ID, round_id))
    await controller._queue.join()
    assert game.deck_pointer == 1
    assert game.last_voided_round is not None
    assert game.last_voided_round.reason is RoundVoidReason.SONGMASTER_DISCONNECTED
    await controller.stop()


@pytest.mark.asyncio
async def test_old_expiry_cannot_affect_closed_or_replacement_game(
    storage: tuple[Engine, Path],
) -> None:
    second_game_id = UUID(int=4102)
    engine, clock = game_engine(game_ids=(GAME_ID, second_game_id))
    controller = controller_for(storage, engine, NeverSleep())
    await controller.start()
    recovery_id = await disconnect_songmaster(controller)
    clock.value = 109.0
    await controller.submit_request(
        RegisterConnectionRequest(GAME_ID, "sm", "sm-b")
    )
    await controller.submit_request(EndGameRequest(GAME_ID, "sm"))
    await controller.submit_request(CloseGameRequest(GAME_ID, "sm"))
    second = await controller.submit_request(
        CreateGameRequest("new-sm", "New Songmaster", "new-sm-a")
    )
    assert second.game_session_id == second_game_id

    clock.value = 120.0
    controller.post_system_event(
        SongmasterReconnectGraceExpiredEvent(GAME_ID, recovery_id)
    )
    await controller._queue.join()

    assert engine.active_game is second
    assert second.songmaster_browser_token == "new-sm"
    assert isinstance(
        controller.system_event_rejections[-1].error,
        SongmasterRecoveryInapplicableError,
    )
    await controller.stop()
