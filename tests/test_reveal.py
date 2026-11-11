from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from who_sings_that.domain import (
    DeckSkipReason,
    DomainInvariantError,
    GameEndReason,
    GameEngine,
    GameFinalizationResult,
    GameSettings,
    GameState,
    ResolvedPlaylist,
    RevealNotAllowedError,
    RoundTerminationReason,
    SelectedPlaylistSnapshot,
    TrackReference,
    UnauthorizedRevealError,
)

GAME_ID = UUID("11111111-1111-4111-8111-111111111111")
PREPARATION_IDS = (
    UUID("22222222-2222-4222-8222-222222222221"),
    UUID("22222222-2222-4222-8222-222222222222"),
)
ROUND_IDS = (
    UUID("33333333-3333-4333-8333-333333333331"),
    UUID("33333333-3333-4333-8333-333333333332"),
)
CREATED_AT = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
STARTED_AT = datetime(2026, 11, 11, 9, 1, tzinfo=UTC)
FIRST_PLAYBACK_AT = datetime(2026, 11, 11, 9, 2, tzinfo=UTC)
SECOND_PLAYBACK_AT = datetime(2026, 11, 11, 9, 3, tzinfo=UTC)
ENDED_AT = datetime(2026, 11, 11, 9, 4, tzinfo=UTC)
PLAYLIST_CREATED_AT = datetime(2025, 1, 2, 3, 4, tzinfo=UTC)

FIRST_TRACK = TrackReference(10, 180, "First Artist", "First Title")
SHORT_TRACK = TrackReference(20, 10, "Short Artist", "Short Title")
SECOND_TRACK = TrackReference(30, 180, "Second Artist", "Second Title")


class SequenceClock:
    def __init__(self) -> None:
        self._values = iter(
            (
                CREATED_AT,
                STARTED_AT,
                FIRST_PLAYBACK_AT,
                SECOND_PLAYBACK_AT,
                ENDED_AT,
            )
        )

    def __call__(self) -> datetime:
        return next(self._values)


class SequenceFactory:
    def __init__(self, values: tuple[UUID, ...]) -> None:
        self._values = iter(values)

    def __call__(self) -> UUID:
        return next(self._values)


class MutableMonotonicClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


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


def started_engine(*tracks: TrackReference):
    monotonic_clock = MutableMonotonicClock()
    engine = GameEngine(
        GameSettings(playback_duration_seconds=30),
        clock=SequenceClock(),
        monotonic_clock=monotonic_clock,
        game_id_factory=lambda: GAME_ID,
        preparation_id_factory=SequenceFactory(PREPARATION_IDS),
        round_id_factory=SequenceFactory(ROUND_IDS),
        shuffle=lambda values: None,
    )
    game = engine.create_game("songmaster-token", "Songmaster")
    engine.join_game("player-token", "Player")
    engine.set_browser_audio_capability("songmaster-token", True)
    engine.select_playlist("songmaster-token", "playlist")
    engine.start_game("songmaster-token", [resolved_playlist(*tracks)])
    return engine, game, monotonic_clock


def begin_round(engine: GameEngine):
    pending = engine.play_song("songmaster-token")
    engine.record_audio_preparation_ready(
        GAME_ID,
        pending.preparation_id,
        "songmaster-token",
    )
    assert engine.active_game is not None
    assert engine.active_game.active_round is not None
    return engine.active_game.active_round


def awaiting_buzz(*tracks: TrackReference):
    engine, game, monotonic_clock = started_engine(*tracks)
    active_round = begin_round(engine)
    engine.accept_buzz(
        GAME_ID,
        active_round.round_id,
        "player-token",
        monotonic_clock.value + 2.5,
    )
    return engine, game, monotonic_clock, active_round


def awaiting_timeout(*tracks: TrackReference):
    engine, game, monotonic_clock = started_engine(*tracks)
    active_round = begin_round(engine)
    monotonic_clock.value = active_round.playback_deadline_monotonic
    engine.playback_timed_out(
        GAME_ID,
        active_round.round_id,
        monotonic_clock.value,
    )
    return engine, game, monotonic_clock, active_round


def reveal(
    engine: GameEngine,
    round_id: UUID = ROUND_IDS[0],
    token: str = "songmaster-token",
    game_id: UUID = GAME_ID,
):
    return engine.reveal_answer(game_id, round_id, token)


def scores(game) -> dict[str, int]:
    return {
        token: member.committed_score for token, member in game.members.items()
    }


def test_transition_h_reveals_answer_and_enters_scoring_without_consuming() -> None:
    engine, game, _, active_round = awaiting_buzz(FIRST_TRACK, SECOND_TRACK)
    assert game.displayed_artist is None
    assert game.displayed_title is None
    termination = game.round_termination
    game.songmaster.committed_score = 40
    game.members["player-token"].committed_score = 25
    scores_before = scores(game)
    pointer_before = game.deck_pointer

    returned = reveal(engine)

    assert returned is game
    assert engine.state is GameState.SCORING
    assert game.displayed_deck_position == 1
    assert game.displayed_artist == "First Artist"
    assert game.displayed_title == "First Title"
    assert game.active_round is active_round
    assert game.round_termination is termination
    assert termination is not None
    assert termination.reason is RoundTerminationReason.BUZZ
    assert termination.winning_browser_token == "player-token"
    assert termination.buzz_elapsed_seconds == 2.5
    assert game.deck_pointer == pointer_before
    assert game.last_completed_round is None
    assert game.buzzing_enabled is False
    assert scores(game) == scores_before
    engine.validate_invariants()


def test_transition_h_preserves_round_identity_and_settings_snapshot() -> None:
    engine, game, _, active_round = awaiting_buzz(FIRST_TRACK)
    settings_snapshot = (
        active_round.configured_playback_offset_seconds,
        active_round.effective_playback_offset_seconds,
        active_round.playback_duration_seconds,
        active_round.speed_bonus_plateau_percent,
    )

    reveal(engine)

    assert game.active_round is active_round
    assert game.active_round.round_id == ROUND_IDS[0]
    assert (
        game.active_round.configured_playback_offset_seconds,
        game.active_round.effective_playback_offset_seconds,
        game.active_round.playback_duration_seconds,
        game.active_round.speed_bonus_plateau_percent,
    ) == settings_snapshot


@pytest.mark.parametrize(
    ("game_id", "round_id"),
    ((uuid4(), ROUND_IDS[0]), (GAME_ID, uuid4())),
)
def test_wrong_game_or_round_reveal_is_rejected_without_mutation(
    game_id: UUID,
    round_id: UUID,
) -> None:
    engine, game, _, _ = awaiting_buzz(FIRST_TRACK)
    before = deepcopy(game)

    with pytest.raises(RevealNotAllowedError):
        reveal(engine, round_id=round_id, game_id=game_id)

    assert game == before


def test_ordinary_contestant_cannot_reveal() -> None:
    engine, game, _, _ = awaiting_buzz(FIRST_TRACK)
    before = deepcopy(game)

    with pytest.raises(UnauthorizedRevealError, match="connected Songmaster"):
        reveal(engine, token="player-token")

    assert game == before
    assert game.displayed_artist is None
    assert game.displayed_title is None


def test_disconnected_songmaster_cannot_reveal() -> None:
    engine, game, _, _ = awaiting_buzz(FIRST_TRACK)
    game.songmaster.connected = False
    before = deepcopy(game)

    with pytest.raises(UnauthorizedRevealError, match="connected Songmaster"):
        reveal(engine)

    assert game == before
    assert game.displayed_artist is None
    assert game.displayed_title is None


def test_premature_and_duplicate_reveals_are_rejected() -> None:
    engine, game, _ = started_engine(FIRST_TRACK)
    active_round = begin_round(engine)
    before = deepcopy(game)

    with pytest.raises(RevealNotAllowedError, match="AWAITING_REVEAL"):
        reveal(engine, active_round.round_id)
    assert game == before

    engine.accept_buzz(GAME_ID, active_round.round_id, "player-token", 102.0)
    reveal(engine, active_round.round_id)
    before = deepcopy(game)

    with pytest.raises(RevealNotAllowedError, match="AWAITING_REVEAL"):
        reveal(engine, active_round.round_id)
    assert game == before


def test_transition_i_reveals_completes_and_scans_next_candidate_once() -> None:
    engine, game, _, active_round = awaiting_timeout(
        FIRST_TRACK,
        SHORT_TRACK,
        SECOND_TRACK,
    )
    game.songmaster.committed_score = 40
    game.members["player-token"].committed_score = 25
    scores_before = scores(game)

    returned = reveal(engine)

    assert returned is game
    assert engine.state is GameState.ROUND_COMPLETE
    assert game.deck_pointer == 2
    assert game.active_round is None
    assert game.round_termination is None
    assert game.buzzing_enabled is False
    assert game.displayed_deck_position == 1
    assert game.displayed_artist == "First Artist"
    assert game.displayed_title == "First Title"
    assert scores(game) == scores_before
    assert game.last_completed_round is not None
    assert game.last_completed_round.active_round is active_round
    assert game.last_completed_round.termination.reason is (
        RoundTerminationReason.PLAYBACK_TIMEOUT
    )
    assert game.last_completed_round.termination.winning_browser_token is None
    assert game.last_completed_round.termination.buzz_elapsed_seconds is None
    assert game.last_completed_round.awarded_points == 0
    assert game.skipped_deck_entries[-1].original_position == 2
    assert game.skipped_deck_entries[-1].reason is (
        DeckSkipReason.DURATION_INELIGIBLE
    )
    engine.validate_invariants()


def test_duplicate_timeout_reveal_cannot_consume_next_song() -> None:
    engine, game, _, active_round = awaiting_timeout(FIRST_TRACK, SECOND_TRACK)
    reveal(engine, active_round.round_id)
    pointer_after = game.deck_pointer
    completed_after = game.last_completed_round
    before = deepcopy(game)

    with pytest.raises(RevealNotAllowedError):
        reveal(engine, active_round.round_id)

    assert game == before
    assert game.deck_pointer == pointer_after == 1
    assert game.last_completed_round is completed_after


def test_transition_i_exhaustion_waits_for_explicit_transition_k() -> None:
    engine, game, _, active_round = awaiting_timeout(FIRST_TRACK)

    reveal(engine, active_round.round_id)

    assert engine.state is GameState.ROUND_COMPLETE
    assert game.deck_pointer == 1
    assert game.deck_exhausted is True
    assert game.end_reason is None
    assert game.ended_at is None
    assert game.final_standings == ()
    assert game.displayed_deck_position == 1
    assert game.displayed_artist == "First Artist"
    assert game.displayed_title == "First Title"
    assert game.last_completed_round is not None
    assert game.last_completed_round.active_round.round_id == active_round.round_id
    assert game.scoring_correction_round_id is None
    engine.validate_invariants()

    finalization = engine.play_song("songmaster-token")

    assert isinstance(finalization, GameFinalizationResult)
    assert engine.state is GameState.FINISHED
    assert game.end_reason is GameEndReason.DECK_EXHAUSTED
    assert game.ended_at == SECOND_PLAYBACK_AT
    engine.validate_invariants()


def test_previous_answer_survives_preparation_then_next_e_hides_it() -> None:
    engine, game, monotonic_clock, first_round = awaiting_timeout(
        FIRST_TRACK,
        SECOND_TRACK,
    )
    reveal(engine, first_round.round_id)
    assert engine.state is GameState.ROUND_COMPLETE

    pending = engine.play_song("songmaster-token")

    assert engine.state is GameState.AUDIO_PREPARING
    assert game.displayed_deck_position == 1
    assert game.displayed_artist == "First Artist"
    assert game.displayed_title == "First Title"

    monotonic_clock.value = 200.0
    engine.record_audio_preparation_ready(
        GAME_ID,
        pending.preparation_id,
        "songmaster-token",
    )

    assert engine.state is GameState.PLAYING
    assert game.active_round is not None
    assert game.active_round.round_id == ROUND_IDS[1]
    assert game.displayed_deck_position == 2
    assert game.displayed_artist is None
    assert game.displayed_title is None
    engine.validate_invariants()


def test_old_round_reveal_cannot_alter_a_newer_awaiting_round() -> None:
    engine, game, monotonic_clock, first_round = awaiting_timeout(
        FIRST_TRACK,
        SECOND_TRACK,
    )
    reveal(engine, first_round.round_id)
    second_round = begin_round(engine)
    monotonic_clock.value = second_round.playback_started_monotonic + 3.0
    engine.accept_buzz(
        GAME_ID,
        second_round.round_id,
        "player-token",
        monotonic_clock.value,
    )
    before = deepcopy(game)

    with pytest.raises(RevealNotAllowedError, match="stale round"):
        reveal(engine, first_round.round_id)

    assert game == before
    assert engine.state is GameState.AWAITING_REVEAL
    assert game.displayed_artist is None
    assert game.displayed_title is None


def test_scoring_invariants_require_revealed_answer_and_buzz_outcome() -> None:
    engine, game, _, _ = awaiting_buzz(FIRST_TRACK)
    reveal(engine)
    termination = game.round_termination

    game.displayed_artist = None
    with pytest.raises(DomainInvariantError, match="display"):
        engine.validate_invariants()
    game.displayed_artist = "First Artist"

    assert termination is not None
    game.round_termination = None
    with pytest.raises(DomainInvariantError, match="buzz termination"):
        engine.validate_invariants()


def test_reveal_requires_an_internal_answer_snapshot() -> None:
    answerless = TrackReference(10, 180)
    engine, game, _, _ = awaiting_buzz(answerless)
    before = deepcopy(game)

    with pytest.raises(DomainInvariantError, match="artist and title"):
        reveal(engine)

    assert game == before
    assert engine.state is GameState.AWAITING_REVEAL
