from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from who_sings_that.domain import (
    GameEndReason,
    GameEngine,
    GameFinalizationResult,
    GameSettings,
    GameState,
    InvalidScoringDecisionError,
    ResolvedPlaylist,
    RoundTerminationReason,
    ScoreCorrectionNotAllowedError,
    ScoringChoice,
    ScoringNotAllowedError,
    SelectedPlaylistSnapshot,
    TrackReference,
    UnauthorizedScoreCorrectionError,
    UnauthorizedScoringError,
    calculate_points,
)
from who_sings_that.domain.engine import NoValidAudioTargetError

GAME_ID = UUID("11111111-1111-4111-8111-111111111111")
PLAYLIST_CREATED_AT = datetime(2025, 1, 2, 3, 4, tzinfo=UTC)
TRACKS = (
    TrackReference(10, 240, "First Artist", "First Title"),
    TrackReference(20, 240, "Second Artist", "Second Title"),
    TrackReference(30, 240, "Third Artist", "Third Title"),
)


class AdvancingClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)

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
        GameSettings(
            playback_offset_seconds=17,
            playback_duration_seconds=40,
            speed_bonus_plateau_percent=Decimal(25),
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
    engine.join_game("other-token", "Other")
    engine.set_browser_audio_capability("songmaster-token", True)
    engine.select_playlist("songmaster-token", "playlist")
    engine.start_game("songmaster-token", [resolved_playlist(*tracks)])
    return engine, game, monotonic_clock


def begin_round(engine: GameEngine, monotonic_clock: MutableMonotonicClock):
    pending = engine.play_song("songmaster-token")
    engine.record_audio_preparation_ready(
        GAME_ID,
        pending.preparation_id,
        "songmaster-token",
    )
    game = engine.active_game
    assert game is not None
    assert game.active_round is not None
    assert game.active_round.playback_started_monotonic == monotonic_clock.value
    return game.active_round


def enter_scoring(
    engine: GameEngine,
    monotonic_clock: MutableMonotonicClock,
    *,
    winner: str = "winner-token",
    elapsed: float = 12.5,
):
    active_round = begin_round(engine, monotonic_clock)
    engine.accept_buzz(
        GAME_ID,
        active_round.round_id,
        winner,
        active_round.playback_started_monotonic + elapsed,
    )
    engine.reveal_answer(GAME_ID, active_round.round_id, "songmaster-token")
    assert engine.state is GameState.SCORING
    return active_round


def complete_score(
    engine: GameEngine,
    active_round,
    choice: ScoringChoice,
):
    return engine.accept_score(
        GAME_ID,
        active_round.round_id,
        "songmaster-token",
        choice,
    )


@pytest.mark.parametrize(
    "choice",
    [
        ScoringChoice.ARTIST,
        ScoringChoice.TITLE,
        ScoringChoice.BOTH,
        ScoringChoice.NEITHER,
    ],
)
def test_transition_j_uses_pure_scoring_and_completes_buzz_round(
    choice: ScoringChoice,
) -> None:
    engine, game, monotonic_clock = started_engine(*TRACKS)
    active_round = enter_scoring(engine, monotonic_clock, elapsed=12.5)
    scores_before = {
        token: member.committed_score for token, member in game.members.items()
    }
    expected = calculate_points(choice, 40, Decimal("0.25"), 12.5)

    completed = complete_score(engine, active_round, choice)

    assert engine.state is GameState.ROUND_COMPLETE
    assert completed is game.last_completed_round
    assert completed.active_round is active_round
    assert completed.termination is not None
    assert completed.termination.reason is RoundTerminationReason.BUZZ
    assert completed.termination.winning_browser_token == "winner-token"
    assert completed.termination.buzz_elapsed_seconds == 12.5
    assert completed.scoring_choice is choice
    assert completed.awarded_points == expected
    assert game.members["winner-token"].committed_score == expected
    assert game.members["songmaster-token"].committed_score == scores_before[
        "songmaster-token"
    ]
    assert game.members["other-token"].committed_score == scores_before[
        "other-token"
    ]
    assert game.deck_pointer == 1
    assert game.active_round is None
    assert game.round_termination is None
    assert game.displayed_deck_position == 1
    assert game.displayed_artist == "First Artist"
    assert game.displayed_title == "First Title"
    assert game.scoring_correction_round_id == active_round.round_id
    engine.validate_invariants()


def test_winning_songmaster_scores_and_non_playback_winner_can_score() -> None:
    engine, game, monotonic_clock = started_engine(*TRACKS)
    songmaster_round = enter_scoring(
        engine,
        monotonic_clock,
        winner="songmaster-token",
        elapsed=2,
    )
    complete_score(engine, songmaster_round, ScoringChoice.BOTH)
    songmaster_award = calculate_points(ScoringChoice.BOTH, 40, Decimal(".25"), 2)
    assert game.members["songmaster-token"].committed_score == songmaster_award

    monotonic_clock.value = 200
    second_round = enter_scoring(
        engine,
        monotonic_clock,
        winner="winner-token",
        elapsed=20,
    )
    assert "winner-token" not in second_round.playback_clients
    complete_score(engine, second_round, ScoringChoice.ARTIST)
    assert game.members["winner-token"].committed_score == calculate_points(
        ScoringChoice.ARTIST,
        40,
        Decimal(".25"),
        20,
    )


def test_trusted_elapsed_time_and_round_snapshot_control_award() -> None:
    engine, game, monotonic_clock = started_engine(*TRACKS)
    active_round = enter_scoring(engine, monotonic_clock, elapsed=31.25)

    completed = complete_score(engine, active_round, ScoringChoice.BOTH)

    assert completed.awarded_points == calculate_points(
        ScoringChoice.BOTH,
        active_round.playback_duration_seconds,
        active_round.speed_bonus_plateau_percent / 100,
        completed.termination.buzz_elapsed_seconds,
    )
    assert completed.awarded_points < 180
    assert game.members["winner-token"].committed_score == completed.awarded_points


def test_final_transition_j_keeps_corrections_open_until_explicit_k() -> None:
    short_track = TrackReference(15, 10, "Short Artist", "Short Title")
    engine, game, monotonic_clock = started_engine(TRACKS[0], short_track, TRACKS[1])
    first_round = enter_scoring(engine, monotonic_clock)
    complete_score(engine, first_round, ScoringChoice.ARTIST)
    assert engine.state is GameState.ROUND_COMPLETE
    assert game.deck_pointer == 2
    assert game.deck is not None
    assert game.deck[game.deck_pointer].track is TRACKS[1]

    monotonic_clock.value = 200
    final_round = enter_scoring(engine, monotonic_clock, elapsed=4)
    completed = complete_score(engine, final_round, ScoringChoice.BOTH)

    assert engine.state is GameState.ROUND_COMPLETE
    assert game.deck_pointer == 3
    assert game.deck_exhausted is True
    assert game.end_reason is None
    assert game.scoring_correction_round_id == final_round.round_id
    assert game.final_standings == ()
    assert game.displayed_artist == "Second Artist"
    assert game.displayed_title == "Second Title"

    pointer = game.deck_pointer
    for choice in (
        ScoringChoice.NEITHER,
        ScoringChoice.ARTIST,
        ScoringChoice.BOTH,
    ):
        completed = engine.correct_score(
            GAME_ID,
            final_round.round_id,
            "songmaster-token",
            choice,
        )
        assert engine.state is GameState.ROUND_COMPLETE
        assert game.deck_pointer == pointer
        assert game.deck_exhausted is True

    finalization = engine.play_song("songmaster-token")

    assert isinstance(finalization, GameFinalizationResult)
    assert engine.state is GameState.FINISHED
    assert game.end_reason is GameEndReason.DECK_EXHAUSTED
    assert game.scoring_correction_round_id is None
    assert game.final_standings[0].browser_token == "winner-token"
    assert game.final_standings[0].score == (
        calculate_points(ScoringChoice.ARTIST, 40, Decimal(".25"), 12.5)
        + completed.awarded_points
    )

    with pytest.raises(ScoreCorrectionNotAllowedError):
        engine.correct_score(
            GAME_ID,
            final_round.round_id,
            "songmaster-token",
            ScoringChoice.NEITHER,
        )


def test_transition_j_consumes_trailing_short_entries_without_changing_display(
) -> None:
    trailing_short = (
        TrackReference(40, 10, "Short One", "Short One"),
        TrackReference(50, 20, "Short Two", "Short Two"),
    )
    engine, game, monotonic_clock = started_engine(TRACKS[0], *trailing_short)
    active_round = enter_scoring(engine, monotonic_clock)

    completed = complete_score(engine, active_round, ScoringChoice.ARTIST)

    assert engine.state is GameState.ROUND_COMPLETE
    assert game.deck is not None
    assert game.original_deck_size == 3
    assert game.deck_pointer == len(game.deck)
    assert game.deck_exhausted is True
    assert [skip.original_position for skip in game.skipped_deck_entries] == [2, 3]
    assert game.displayed_deck_position == 1
    assert game.displayed_artist == "First Artist"
    assert game.displayed_title == "First Title"
    assert game.scoring_correction_round_id == active_round.round_id
    assert game.final_standings == ()
    assert completed is game.last_completed_round


@pytest.mark.parametrize(
    ("game_id", "round_id", "token", "choice", "error"),
    [
        (UUID(int=999), UUID(int=200), "songmaster-token", ScoringChoice.BOTH, ScoringNotAllowedError),
        (GAME_ID, UUID(int=999), "songmaster-token", ScoringChoice.BOTH, ScoringNotAllowedError),
        (GAME_ID, UUID(int=200), "winner-token", ScoringChoice.BOTH, UnauthorizedScoringError),
        (GAME_ID, UUID(int=200), "songmaster-token", "both", InvalidScoringDecisionError),
    ],
)
def test_initial_scoring_rejections_preserve_all_authoritative_facts(
    game_id: UUID,
    round_id: UUID,
    token: str,
    choice: object,
    error: type[Exception],
) -> None:
    engine, game, monotonic_clock = started_engine(*TRACKS)
    active_round = enter_scoring(engine, monotonic_clock)
    before = deepcopy(game)

    with pytest.raises(error):
        engine.accept_score(game_id, round_id, token, choice)  # type: ignore[arg-type]

    assert game == before
    assert game.active_round is active_round


def test_disconnected_songmaster_and_premature_or_duplicate_scoring_are_rejected() -> None:
    engine, game, monotonic_clock = started_engine(*TRACKS)
    active_round = begin_round(engine, monotonic_clock)
    before = deepcopy(game)
    with pytest.raises(ScoringNotAllowedError):
        complete_score(engine, active_round, ScoringChoice.BOTH)
    assert game == before

    engine.accept_buzz(GAME_ID, active_round.round_id, "winner-token", 102)
    engine.reveal_answer(GAME_ID, active_round.round_id, "songmaster-token")
    game.songmaster.connected = False
    before = deepcopy(game)
    with pytest.raises(UnauthorizedScoringError):
        complete_score(engine, active_round, ScoringChoice.BOTH)
    assert game == before
    game.songmaster.connected = True

    completed = complete_score(engine, active_round, ScoringChoice.BOTH)
    score_after = game.members["winner-token"].committed_score
    pointer_after = game.deck_pointer
    with pytest.raises(ScoringNotAllowedError):
        complete_score(engine, active_round, ScoringChoice.BOTH)
    assert game.last_completed_round is completed
    assert game.members["winner-token"].committed_score == score_after
    assert game.deck_pointer == pointer_after


def test_replacement_corrections_apply_only_net_delta_and_preserve_round_facts() -> None:
    engine, game, monotonic_clock = started_engine(*TRACKS)
    active_round = enter_scoring(engine, monotonic_clock, elapsed=18)
    original = complete_score(engine, active_round, ScoringChoice.ARTIST)
    pointer = game.deck_pointer
    state = engine.state
    immutable_facts = (
        original.active_round,
        original.termination,
        game.displayed_deck_position,
        game.displayed_artist,
        game.displayed_title,
    )

    choices = (
        ScoringChoice.BOTH,
        ScoringChoice.NEITHER,
        ScoringChoice.TITLE,
        ScoringChoice.TITLE,
        ScoringChoice.BOTH,
    )
    for choice in choices:
        completed = engine.correct_score(
            GAME_ID,
            active_round.round_id,
            "songmaster-token",
            choice,
        )
        expected = calculate_points(choice, 40, Decimal(".25"), 18)
        assert completed.awarded_points == expected
        assert game.members["winner-token"].committed_score == expected
        assert game.deck_pointer == pointer
        assert engine.state is state
        assert (
            completed.active_round,
            completed.termination,
            game.displayed_deck_position,
            game.displayed_artist,
            game.displayed_title,
        ) == immutable_facts


def test_correction_uses_original_settings_and_preserves_earlier_round_points() -> None:
    engine, game, monotonic_clock = started_engine(*TRACKS)
    first_round = enter_scoring(engine, monotonic_clock, elapsed=8)
    first = complete_score(engine, first_round, ScoringChoice.BOTH)
    first_points = first.awarded_points

    monotonic_clock.value = 200
    second_round = enter_scoring(engine, monotonic_clock, elapsed=24)
    second = complete_score(engine, second_round, ScoringChoice.ARTIST)
    game.settings.playback_duration_seconds = 120
    game.settings.speed_bonus_plateau_percent = Decimal(100)

    corrected = engine.correct_score(
        GAME_ID,
        second_round.round_id,
        "songmaster-token",
        ScoringChoice.TITLE,
    )

    expected_second = calculate_points(ScoringChoice.TITLE, 40, Decimal(".25"), 24)
    assert corrected.awarded_points == expected_second
    assert game.members["winner-token"].committed_score == first_points + expected_second
    assert second.active_round.playback_duration_seconds == 40
    assert second.active_round.speed_bonus_plateau_percent == Decimal(25)


def test_correction_authorization_round_and_timeout_rejections_are_non_mutating() -> None:
    engine, game, monotonic_clock = started_engine(*TRACKS)
    active_round = enter_scoring(engine, monotonic_clock)
    complete_score(engine, active_round, ScoringChoice.ARTIST)

    for game_id, round_id, token, error in (
        (UUID(int=999), active_round.round_id, "songmaster-token", ScoreCorrectionNotAllowedError),
        (GAME_ID, UUID(int=999), "songmaster-token", ScoreCorrectionNotAllowedError),
        (GAME_ID, active_round.round_id, "winner-token", UnauthorizedScoreCorrectionError),
    ):
        before = deepcopy(game)
        with pytest.raises(error):
            engine.correct_score(game_id, round_id, token, ScoringChoice.BOTH)
        assert game == before

    monotonic_clock.value = 200
    timeout_round = begin_round(engine, monotonic_clock)
    engine.playback_timed_out(
        GAME_ID,
        timeout_round.round_id,
        timeout_round.playback_deadline_monotonic,
    )
    engine.reveal_answer(GAME_ID, timeout_round.round_id, "songmaster-token")
    before = deepcopy(game)
    with pytest.raises(ScoreCorrectionNotAllowedError):
        engine.correct_score(
            GAME_ID,
            timeout_round.round_id,
            "songmaster-token",
            ScoringChoice.BOTH,
        )
    assert game == before


def test_correction_window_survives_rejected_play_but_closes_on_successful_c() -> None:
    engine, game, monotonic_clock = started_engine(*TRACKS)
    active_round = enter_scoring(engine, monotonic_clock)
    complete_score(engine, active_round, ScoringChoice.ARTIST)
    engine.set_browser_audio_capability("songmaster-token", False)

    with pytest.raises(NoValidAudioTargetError):
        engine.play_song("songmaster-token")
    assert game.scoring_correction_round_id == active_round.round_id
    corrected = engine.correct_score(
        GAME_ID,
        active_round.round_id,
        "songmaster-token",
        ScoringChoice.BOTH,
    )
    assert corrected.scoring_choice is ScoringChoice.BOTH

    engine.set_browser_audio_capability("songmaster-token", True)
    pending = engine.play_song("songmaster-token")
    assert engine.state is GameState.AUDIO_PREPARING
    assert game.scoring_correction_round_id is None
    before = deepcopy(game)
    with pytest.raises(ScoreCorrectionNotAllowedError):
        engine.correct_score(
            GAME_ID,
            active_round.round_id,
            "songmaster-token",
            ScoringChoice.NEITHER,
        )
    assert game == before

    engine.record_audio_preparation_failed(
        GAME_ID,
        pending.preparation_id,
        "songmaster-token",
    )
    engine.acknowledge_audio_preparation_failure(
        GAME_ID,
        pending.preparation_id,
        "songmaster-token",
    )
    assert engine.state is GameState.ROUND_COMPLETE
    assert game.scoring_correction_round_id is None
    with pytest.raises(ScoreCorrectionNotAllowedError):
        engine.correct_score(
            GAME_ID,
            active_round.round_id,
            "songmaster-token",
            ScoringChoice.NEITHER,
        )


def test_each_subsequent_scored_round_gets_its_own_correction_window() -> None:
    engine, game, monotonic_clock = started_engine(*TRACKS)
    first_round = enter_scoring(engine, monotonic_clock)
    first = complete_score(engine, first_round, ScoringChoice.ARTIST)
    first_points = first.awarded_points

    monotonic_clock.value = 200
    second_round = enter_scoring(engine, monotonic_clock, elapsed=6)
    with pytest.raises(ScoreCorrectionNotAllowedError):
        engine.correct_score(
            GAME_ID,
            first_round.round_id,
            "songmaster-token",
            ScoringChoice.BOTH,
        )
    second = complete_score(engine, second_round, ScoringChoice.TITLE)
    assert game.scoring_correction_round_id == second_round.round_id

    corrected = engine.correct_score(
        GAME_ID,
        second_round.round_id,
        "songmaster-token",
        ScoringChoice.BOTH,
    )
    assert game.members["winner-token"].committed_score == (
        first_points + corrected.awarded_points
    )
    assert second.active_round.round_id == second_round.round_id
