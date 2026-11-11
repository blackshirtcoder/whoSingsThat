from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from who_sings_that.domain import (
    BuzzNotAllowedError,
    DomainInvariantError,
    GameEngine,
    GameMember,
    GameSettings,
    GameState,
    PlaybackTimeoutInapplicableError,
    ResolvedPlaylist,
    RoundTermination,
    RoundTerminationReason,
    SelectedPlaylistSnapshot,
    TrackReference,
    player_name_uniqueness_key,
)

GAME_ID = UUID("11111111-aaaa-4111-8111-111111111111")
PREPARATION_ID = UUID("22222222-bbbb-4222-8222-222222222222")
ROUND_ID = UUID("33333333-cccc-4333-8333-333333333333")
CREATED_AT = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
STARTED_AT = datetime(2026, 11, 10, 9, 1, tzinfo=UTC)
PLAYBACK_STARTED_AT = datetime(2026, 11, 10, 9, 2, tzinfo=UTC)
PLAYLIST_CREATED_AT = datetime(2025, 1, 2, 3, 4, tzinfo=UTC)


class MutableMonotonicClock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class MutableWallClock:
    def __init__(self) -> None:
        self.values = [CREATED_AT, STARTED_AT, PLAYBACK_STARTED_AT]
        self.index = 0

    def __call__(self) -> datetime:
        value = self.values[self.index]
        self.index += 1
        return value


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


def playing_game(
    *,
    include_player: bool = True,
    duration: int = 30,
    monotonic_start: float = 100.0,
):
    monotonic_clock = MutableMonotonicClock(monotonic_start)
    wall_clock = MutableWallClock()
    engine = GameEngine(
        GameSettings(playback_duration_seconds=duration),
        clock=wall_clock,
        monotonic_clock=monotonic_clock,
        game_id_factory=lambda: GAME_ID,
        preparation_id_factory=lambda: PREPARATION_ID,
        round_id_factory=lambda: ROUND_ID,
        shuffle=lambda tracks: None,
    )
    game = engine.create_game("songmaster-token", "Songmaster")
    if include_player:
        engine.join_game("player-token", "Player")
    engine.set_browser_audio_capability("songmaster-token", True)
    engine.select_playlist("songmaster-token", "playlist")
    engine.start_game("songmaster-token", [resolved_playlist()])
    pending = engine.play_song("songmaster-token")
    engine.record_audio_preparation_ready(
        GAME_ID,
        pending.preparation_id,
        "songmaster-token",
    )
    assert game.active_round is not None
    return engine, game, monotonic_clock, wall_clock


def buzz(
    engine: GameEngine,
    token: str = "player-token",
    received_at: float = 102.3,
    *,
    game_id: UUID = GAME_ID,
    round_id: UUID = ROUND_ID,
):
    return engine.accept_buzz(game_id, round_id, token, received_at)


def timeout(
    engine: GameEngine,
    observed_at: float = 130.0,
    *,
    game_id: UUID = GAME_ID,
    round_id: UUID = ROUND_ID,
):
    return engine.playback_timed_out(game_id, round_id, observed_at)


def test_transition_f_accepts_first_valid_buzz_and_preserves_round_facts() -> None:
    engine, game, _, _ = playing_game()
    assert game.active_round is not None
    active_round = game.active_round
    pointer_before = game.deck_pointer
    scores_before = {token: member.committed_score for token, member in game.members.items()}

    termination = buzz(engine)

    assert engine.state is GameState.AWAITING_REVEAL
    assert termination is game.round_termination
    assert termination.reason is RoundTerminationReason.BUZZ
    assert termination.winning_browser_token == "player-token"
    assert termination.buzz_elapsed_seconds == pytest.approx(2.3)
    assert game.active_round is active_round
    assert game.deck_pointer == pointer_before
    assert game.buzzing_enabled is False
    assert game.displayed_artist is None
    assert game.displayed_title is None
    assert {
        token: member.committed_score for token, member in game.members.items()
    } == scores_before
    engine.validate_invariants()


@pytest.mark.parametrize("token", ["songmaster-token", "player-token"])
def test_songmaster_and_non_playback_contestant_can_buzz(token: str) -> None:
    engine, game, _, _ = playing_game()
    assert game.active_round is not None
    assert game.active_round.playback_clients == frozenset({"songmaster-token"})
    assert game.members["player-token"].audio_enabled is False

    termination = buzz(engine, token)

    assert termination.winning_browser_token == token


def test_ineligible_and_disconnected_members_are_rejected() -> None:
    engine, game, _, _ = playing_game()
    late_member = GameMember(
        browser_token="late-token",
        display_name="Late Player",
        name_uniqueness_key=player_name_uniqueness_key("Late Player"),
    )
    game.members[late_member.browser_token] = late_member

    with pytest.raises(BuzzNotAllowedError, match="not eligible"):
        buzz(engine, "late-token")

    game.members["player-token"].connected = False
    with pytest.raises(BuzzNotAllowedError, match="disconnected"):
        buzz(engine, "player-token")

    assert engine.state is GameState.PLAYING
    assert game.round_termination is None


@pytest.mark.parametrize(
    ("game_id", "round_id"),
    [(uuid4(), ROUND_ID), (GAME_ID, uuid4())],
)
def test_wrong_game_or_round_buzz_is_rejected_without_mutation(
    game_id: UUID, round_id: UUID
) -> None:
    engine, game, _, _ = playing_game()
    before = deepcopy(game)

    with pytest.raises(BuzzNotAllowedError):
        buzz(engine, game_id=game_id, round_id=round_id)

    assert game == before


def test_invalid_buzz_does_not_prevent_later_valid_buzz() -> None:
    engine, game, _, _ = playing_game()

    with pytest.raises(BuzzNotAllowedError):
        buzz(engine, "unknown-token")

    termination = buzz(engine, "player-token", 103.0)

    assert termination.winning_browser_token == "player-token"
    assert game.round_termination is termination


def test_first_accepted_buzz_cannot_be_replaced() -> None:
    engine, game, _, _ = playing_game()
    first = buzz(engine, "player-token", 102.0)
    scores_before = {token: member.committed_score for token, member in game.members.items()}

    with pytest.raises(BuzzNotAllowedError):
        buzz(engine, "songmaster-token", 101.0)

    assert game.round_termination is first
    assert game.round_termination.winning_browser_token == "player-token"
    assert game.round_termination.buzz_elapsed_seconds == 2.0
    assert {
        token: member.committed_score for token, member in game.members.items()
    } == scores_before


@pytest.mark.parametrize(
    ("receipt", "expected_message"),
    [(99.999, "before playback"), (130.0, "at or after"), (130.001, "at or after")],
)
def test_strict_playback_interval_rejects_early_and_late_buzzes(
    receipt: float, expected_message: str
) -> None:
    engine, game, _, _ = playing_game()

    with pytest.raises(BuzzNotAllowedError, match=expected_message):
        buzz(engine, received_at=receipt)

    assert engine.state is GameState.PLAYING
    assert game.round_termination is None


def test_receipt_time_controls_elapsed_even_when_processing_clock_is_later() -> None:
    engine, _, monotonic_clock, _ = playing_game()
    monotonic_clock.value = 129.9

    termination = buzz(engine, received_at=102.3)

    assert termination.buzz_elapsed_seconds == pytest.approx(2.3)


def test_monotonic_timing_is_independent_of_wall_clock_adjustment() -> None:
    engine, game, _, wall_clock = playing_game()
    assert game.active_round is not None
    assert wall_clock.index == 3
    wall_clock.values[-1] = PLAYBACK_STARTED_AT - timedelta(days=1000)

    termination = buzz(engine, received_at=104.0)

    assert termination.buzz_elapsed_seconds == 4.0
    assert wall_clock.index == 3
    assert game.active_round.playback_started_at == PLAYBACK_STARTED_AT
    assert game.active_round.playback_started_monotonic == 100.0
    assert game.active_round.playback_deadline_monotonic == 130.0


def test_transition_g_enters_awaiting_reveal_without_fabricating_winner() -> None:
    engine, game, _, _ = playing_game()
    assert game.active_round is not None
    active_round = game.active_round
    pointer_before = game.deck_pointer
    scores_before = {token: member.committed_score for token, member in game.members.items()}

    termination = timeout(engine)

    assert engine.state is GameState.AWAITING_REVEAL
    assert termination.reason is RoundTerminationReason.PLAYBACK_TIMEOUT
    assert termination.winning_browser_token is None
    assert termination.buzz_elapsed_seconds is None
    assert game.active_round is active_round
    assert game.deck_pointer == pointer_before
    assert game.buzzing_enabled is False
    assert game.displayed_artist is None
    assert game.displayed_title is None
    assert {
        token: member.committed_score for token, member in game.members.items()
    } == scores_before
    engine.validate_invariants()


def test_early_and_wrong_timeout_events_are_harmless() -> None:
    engine, game, _, _ = playing_game()
    before = deepcopy(game)

    with pytest.raises(PlaybackTimeoutInapplicableError, match="before"):
        timeout(engine, 129.999)
    with pytest.raises(PlaybackTimeoutInapplicableError):
        timeout(engine, game_id=uuid4())
    with pytest.raises(PlaybackTimeoutInapplicableError):
        timeout(engine, round_id=uuid4())

    assert game == before
    assert engine.state is GameState.PLAYING


def test_timeout_and_buzz_are_mutually_exclusive_terminal_outcomes() -> None:
    engine, game, _, _ = playing_game()
    accepted = buzz(engine)

    with pytest.raises(PlaybackTimeoutInapplicableError):
        timeout(engine)

    assert game.round_termination is accepted

    engine, game, _, _ = playing_game()
    timed_out = timeout(engine)

    with pytest.raises(BuzzNotAllowedError):
        buzz(engine, received_at=102.0)
    with pytest.raises(PlaybackTimeoutInapplicableError):
        timeout(engine, 131.0)

    assert game.round_termination is timed_out


def test_buzz_outside_playing_is_rejected() -> None:
    engine = GameEngine(game_id_factory=lambda: GAME_ID)
    game = engine.create_game("songmaster-token", "Songmaster")

    with pytest.raises(BuzzNotAllowedError):
        buzz(engine, "songmaster-token", 0.0)

    assert engine.state is GameState.LOBBY
    assert game.round_termination is None


def test_playing_and_awaiting_reveal_timing_outcome_invariants() -> None:
    engine, game, _, _ = playing_game()
    assert game.active_round is not None
    original_deadline = game.active_round.playback_deadline_monotonic
    object.__setattr__(
        game.active_round,
        "playback_deadline_monotonic",
        original_deadline + 1,
    )
    with pytest.raises(DomainInvariantError, match="deadline"):
        engine.validate_invariants()
    object.__setattr__(
        game.active_round,
        "playback_deadline_monotonic",
        original_deadline,
    )

    buzz(engine)
    accepted_termination = game.round_termination
    game.round_termination = None
    with pytest.raises(DomainInvariantError, match="termination outcome"):
        engine.validate_invariants()
    game.round_termination = accepted_termination
    game.round_termination = RoundTermination(
        reason=RoundTerminationReason.PLAYBACK_TIMEOUT,
        winning_browser_token="player-token",
        buzz_elapsed_seconds=2.0,
    )
    with pytest.raises(DomainInvariantError, match="cannot contain"):
        engine.validate_invariants()
