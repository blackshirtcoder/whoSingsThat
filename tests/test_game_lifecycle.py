from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from who_sings_that.domain import (
    CancelGameNotAllowedError,
    CloseGameNotAllowedError,
    DomainInvariantError,
    EndGameNotAllowedError,
    GameEndReason,
    GameEngine,
    GameFinalizationResult,
    GameSettings,
    GameState,
    PendingAudioPreparation,
    PreparationEventInapplicableError,
    ResolvedPlaylist,
    RevealNotAllowedError,
    RoundTerminationReason,
    RoundVoidReason,
    ScoreCorrectionNotAllowedError,
    ScoringChoice,
    ScoringNotAllowedError,
    SelectedPlaylistSnapshot,
    TrackReference,
    UnauthorizedCancelGameError,
    UnauthorizedCloseGameError,
    UnauthorizedEndGameError,
    VoidedRound,
)

GAME_IDS = (UUID(int=1), UUID(int=2), UUID(int=3))
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
    def __init__(self, values: tuple[UUID, ...]) -> None:
        self._values = iter(values)

    def __call__(self) -> UUID:
        return next(self._values)


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


def make_engine() -> tuple[GameEngine, MutableMonotonicClock]:
    monotonic_clock = MutableMonotonicClock()
    engine = GameEngine(
        GameSettings(
            playback_duration_seconds=30,
            speed_bonus_plateau_percent=Decimal(10),
        ),
        clock=AdvancingClock(),
        monotonic_clock=monotonic_clock,
        game_id_factory=UUIDFactory(GAME_IDS),
        preparation_id_factory=UUIDFactory(
            (UUID(int=100), UUID(int=101), UUID(int=102))
        ),
        round_id_factory=UUIDFactory((UUID(int=200), UUID(int=201), UUID(int=202))),
        shuffle=lambda values: None,
    )
    return engine, monotonic_clock


def create_lobby(engine: GameEngine):
    game = engine.create_game("songmaster-token", "Songmaster")
    engine.join_game("player-token", "Player")
    return game


def start_game(engine: GameEngine, *tracks: TrackReference):
    game = create_lobby(engine)
    engine.set_browser_audio_capability("songmaster-token", True)
    engine.select_playlist("songmaster-token", "playlist")
    engine.start_game("songmaster-token", [resolved_playlist(*tracks)])
    return game


def begin_playing(engine: GameEngine):
    pending = engine.play_song("songmaster-token")
    assert isinstance(pending, PendingAudioPreparation)
    engine.record_audio_preparation_ready(
        GAME_IDS[0],
        pending.preparation_id,
        "songmaster-token",
    )
    game = engine.active_game
    assert game is not None
    assert game.active_round is not None
    return game.active_round


def enter_awaiting_reveal(
    engine: GameEngine,
    monotonic_clock: MutableMonotonicClock,
    *,
    buzz: bool,
):
    active_round = begin_playing(engine)
    if buzz:
        engine.accept_buzz(
            GAME_IDS[0],
            active_round.round_id,
            "player-token",
            active_round.playback_started_monotonic + 5,
        )
    else:
        monotonic_clock.value = active_round.playback_deadline_monotonic
        engine.playback_timed_out(
            GAME_IDS[0],
            active_round.round_id,
            monotonic_clock.value,
        )
    return active_round


def enter_scoring(engine: GameEngine, monotonic_clock: MutableMonotonicClock):
    active_round = enter_awaiting_reveal(engine, monotonic_clock, buzz=True)
    engine.reveal_answer(
        GAME_IDS[0],
        active_round.round_id,
        "songmaster-token",
    )
    return active_round


def test_cancel_game_discards_lobby_and_allows_fresh_token_membership() -> None:
    engine, _ = make_engine()
    canceled = create_lobby(engine)
    canceled.songmaster.committed_score = 75
    engine.select_playlist("songmaster-token", "playlist")

    result = engine.cancel_game(GAME_IDS[0], "songmaster-token")

    assert result is None
    assert engine.state is GameState.NO_GAME
    assert engine.active_game is None
    assert canceled.started_at is None
    assert canceled.ended_at is None
    assert canceled.final_standings == ()
    engine.validate_invariants()
    with pytest.raises(CancelGameNotAllowedError):
        engine.cancel_game(GAME_IDS[0], "songmaster-token")

    fresh = engine.create_game("songmaster-token", "Fresh Name")

    assert fresh.game_session_id == GAME_IDS[1]
    assert fresh.songmaster.display_name == "Fresh Name"
    assert fresh.songmaster.committed_score == 0
    assert set(fresh.members) == {"songmaster-token"}


def test_cancel_game_rejections_are_non_mutating() -> None:
    engine, _ = make_engine()
    game = create_lobby(engine)

    for game_id, token, error in (
        (UUID(int=999), "songmaster-token", CancelGameNotAllowedError),
        (GAME_IDS[0], "player-token", UnauthorizedCancelGameError),
    ):
        before = deepcopy(game)
        with pytest.raises(error):
            engine.cancel_game(game_id, token)
        assert game == before

    game.songmaster.connected = False
    game.songmaster.browser_audio_usable = False
    disconnected = deepcopy(game)
    with pytest.raises(UnauthorizedCancelGameError):
        engine.cancel_game(GAME_IDS[0], "songmaster-token")
    assert game == disconnected


def test_cancel_game_is_rejected_after_start() -> None:
    engine, _ = make_engine()
    game = start_game(engine, *TRACKS)
    before = deepcopy(game)

    with pytest.raises(CancelGameNotAllowedError):
        engine.cancel_game(GAME_IDS[0], "songmaster-token")

    assert game == before


def test_end_game_from_round_complete_freezes_scores_and_preserves_candidate() -> None:
    engine, monotonic_clock = make_engine()
    game = start_game(engine, *TRACKS)
    active_round = enter_scoring(engine, monotonic_clock)
    completed = engine.accept_score(
        GAME_IDS[0],
        active_round.round_id,
        "songmaster-token",
        ScoringChoice.BOTH,
    )
    game.songmaster.committed_score = completed.awarded_points
    pointer = game.deck_pointer
    display = (
        game.displayed_deck_position,
        game.displayed_artist,
        game.displayed_title,
    )
    assert game.scoring_correction_round_id == active_round.round_id

    result = engine.end_game(GAME_IDS[0], "songmaster-token")

    assert isinstance(result, GameFinalizationResult)
    assert result.end_reason is GameEndReason.SONGMASTER_ENDED_GAME
    assert engine.state is GameState.FINISHED
    assert game.deck_pointer == pointer == 1
    assert game.deck_exhausted is False
    assert game.last_completed_round is completed
    assert game.last_voided_round is None
    assert game.scoring_correction_round_id is None
    assert (
        game.displayed_deck_position,
        game.displayed_artist,
        game.displayed_title,
    ) == display
    assert [standing.rank for standing in game.final_standings] == [1, 1]
    assert [standing.score for standing in game.final_standings] == [
        completed.awarded_points,
        completed.awarded_points,
    ]
    with pytest.raises(ScoreCorrectionNotAllowedError):
        engine.correct_score(
            GAME_IDS[0],
            active_round.round_id,
            "songmaster-token",
            ScoringChoice.NEITHER,
        )
    engine.validate_invariants()


def test_end_game_from_exhausted_round_complete_is_voluntary_not_k() -> None:
    engine, _ = make_engine()
    game = start_game(
        engine,
        TrackReference(10, 10, "Short Artist", "Short Title"),
    )
    assert game.deck_exhausted is True

    result = engine.end_game(GAME_IDS[0], "songmaster-token")

    assert result.end_reason is GameEndReason.SONGMASTER_ENDED_GAME
    assert game.end_reason is GameEndReason.SONGMASTER_ENDED_GAME
    assert game.deck_exhausted is True
    assert game.deck is not None
    assert game.deck_pointer == len(game.deck)
    assert game.last_voided_round is None


def test_end_game_from_audio_preparing_discards_only_prospective_round() -> None:
    engine, monotonic_clock = make_engine()
    game = start_game(engine, TRACKS[0], TRACKS[1])
    first_round = enter_awaiting_reveal(engine, monotonic_clock, buzz=False)
    engine.reveal_answer(GAME_IDS[0], first_round.round_id, "songmaster-token")
    previous_display = (
        game.displayed_deck_position,
        game.displayed_artist,
        game.displayed_title,
    )
    pending = engine.play_song("songmaster-token")
    assert isinstance(pending, PendingAudioPreparation)
    pointer = game.deck_pointer

    engine.end_game(GAME_IDS[0], "songmaster-token")

    assert engine.state is GameState.FINISHED
    assert game.pending_audio_preparation is None
    assert game.audio_preparation_responses is None
    assert game.active_round is None
    assert game.last_voided_round is None
    assert game.deck_pointer == pointer == 1
    assert (
        game.displayed_deck_position,
        game.displayed_artist,
        game.displayed_title,
    ) == previous_display
    assert game.last_completed_round is not None
    assert game.last_completed_round.awarded_points == 0
    with pytest.raises(PreparationEventInapplicableError):
        engine.record_audio_preparation_ready(
            GAME_IDS[0],
            pending.preparation_id,
            "songmaster-token",
        )


def test_end_game_from_playing_records_unterminated_unrevealed_void() -> None:
    engine, _ = make_engine()
    game = start_game(engine, *TRACKS)
    game.members["player-token"].committed_score = 40
    active_round = begin_playing(engine)
    pointer = game.deck_pointer

    engine.end_game(GAME_IDS[0], "songmaster-token")

    voided = game.last_voided_round
    assert isinstance(voided, VoidedRound)
    assert voided.active_round is active_round
    assert voided.reason is RoundVoidReason.GAME_ENDED
    assert voided.termination is None
    assert voided.answer_revealed is False
    assert game.deck_pointer == pointer == 0
    assert game.displayed_deck_position == 1
    assert game.displayed_artist is None
    assert game.displayed_title is None
    assert game.members["player-token"].committed_score == 40
    assert game.final_standings[0].score == 40
    assert game.active_round is None
    assert game.round_termination is None
    assert game.buzzing_enabled is False
    engine.validate_invariants()


@pytest.mark.parametrize("buzz", [True, False])
def test_end_game_from_awaiting_reveal_retains_termination_without_revealing(
    buzz: bool,
) -> None:
    engine, monotonic_clock = make_engine()
    game = start_game(engine, *TRACKS)
    active_round = enter_awaiting_reveal(
        engine,
        monotonic_clock,
        buzz=buzz,
    )
    termination = game.round_termination

    engine.end_game(GAME_IDS[0], "songmaster-token")

    voided = game.last_voided_round
    assert isinstance(voided, VoidedRound)
    assert voided.active_round is active_round
    assert voided.termination is termination
    assert voided.answer_revealed is False
    assert game.displayed_deck_position == 1
    assert game.displayed_artist is None
    assert game.displayed_title is None
    assert game.members["player-token"].committed_score == 0
    if buzz:
        assert termination is not None
        assert termination.reason is RoundTerminationReason.BUZZ
        assert termination.winning_browser_token == "player-token"
    else:
        assert termination is not None
        assert termination.reason is RoundTerminationReason.PLAYBACK_TIMEOUT
    with pytest.raises(RevealNotAllowedError):
        engine.reveal_answer(
            GAME_IDS[0],
            active_round.round_id,
            "songmaster-token",
        )


def test_end_game_from_scoring_retains_revealed_unscored_void() -> None:
    engine, monotonic_clock = make_engine()
    game = start_game(engine, *TRACKS)
    active_round = enter_scoring(engine, monotonic_clock)
    termination = game.round_termination

    engine.end_game(GAME_IDS[0], "songmaster-token")

    voided = game.last_voided_round
    assert isinstance(voided, VoidedRound)
    assert voided.active_round is active_round
    assert voided.termination is termination
    assert voided.answer_revealed is True
    assert game.displayed_deck_position == 1
    assert game.displayed_artist == "First Artist"
    assert game.displayed_title == "First Title"
    assert game.members["player-token"].committed_score == 0
    assert game.last_completed_round is None
    with pytest.raises(ScoringNotAllowedError):
        engine.accept_score(
            GAME_IDS[0],
            active_round.round_id,
            "songmaster-token",
            ScoringChoice.BOTH,
        )
    engine.validate_invariants()


def test_end_game_authorization_state_and_duplicate_rejections_are_non_mutating(
) -> None:
    lobby_engine, _ = make_engine()
    lobby = create_lobby(lobby_engine)
    with pytest.raises(EndGameNotAllowedError):
        lobby_engine.end_game(GAME_IDS[0], "songmaster-token")
    assert lobby_engine.active_game is lobby

    engine, _ = make_engine()
    game = start_game(engine, *TRACKS)
    for game_id, token, error in (
        (UUID(int=999), "songmaster-token", EndGameNotAllowedError),
        (GAME_IDS[0], "player-token", UnauthorizedEndGameError),
    ):
        before = deepcopy(game)
        with pytest.raises(error):
            engine.end_game(game_id, token)
        assert game == before

    game.songmaster.connected = False
    game.songmaster.browser_audio_usable = False
    disconnected = deepcopy(game)
    with pytest.raises(UnauthorizedEndGameError):
        engine.end_game(GAME_IDS[0], "songmaster-token")
    assert game == disconnected
    game.songmaster.connected = True
    engine.end_game(GAME_IDS[0], "songmaster-token")
    with pytest.raises(EndGameNotAllowedError):
        engine.end_game(GAME_IDS[0], "songmaster-token")


def test_natural_and_voluntary_finished_reasons_are_distinct() -> None:
    natural_engine, _ = make_engine()
    natural = start_game(
        natural_engine,
        TrackReference(10, 10, "Short Artist", "Short Title"),
    )
    natural_result = natural_engine.play_song("songmaster-token")

    voluntary_engine, _ = make_engine()
    voluntary = start_game(voluntary_engine, *TRACKS)
    voluntary_result = voluntary_engine.end_game(
        GAME_IDS[0],
        "songmaster-token",
    )

    assert isinstance(natural_result, GameFinalizationResult)
    assert natural.end_reason is GameEndReason.DECK_EXHAUSTED
    assert natural_result.end_reason is GameEndReason.DECK_EXHAUSTED
    assert voluntary.end_reason is GameEndReason.SONGMASTER_ENDED_GAME
    assert voluntary_result.end_reason is GameEndReason.SONGMASTER_ENDED_GAME
    assert voluntary.deck_exhausted is False


def test_close_game_discards_finished_session_and_allows_fresh_game() -> None:
    engine, _ = make_engine()
    finished = start_game(engine, *TRACKS)
    finished.songmaster.committed_score = 60
    engine.end_game(GAME_IDS[0], "songmaster-token")

    result = engine.close_game(GAME_IDS[0], "songmaster-token")

    assert result is None
    assert engine.state is GameState.NO_GAME
    assert engine.active_game is None
    engine.validate_invariants()

    fresh = engine.create_game("songmaster-token", "New Songmaster")

    assert fresh.game_session_id == GAME_IDS[1]
    assert fresh.songmaster.display_name == "New Songmaster"
    assert fresh.songmaster.committed_score == 0
    assert fresh.selected_playlist_ids == set()
    assert fresh.deck is None


def test_close_game_rejections_are_non_mutating() -> None:
    wrong_state_engine, _ = make_engine()
    lobby = create_lobby(wrong_state_engine)
    with pytest.raises(CloseGameNotAllowedError):
        wrong_state_engine.close_game(GAME_IDS[0], "songmaster-token")
    assert wrong_state_engine.active_game is lobby

    engine, _ = make_engine()
    game = start_game(engine, *TRACKS)
    engine.end_game(GAME_IDS[0], "songmaster-token")
    for game_id, token, error in (
        (UUID(int=999), "songmaster-token", CloseGameNotAllowedError),
        (GAME_IDS[0], "player-token", UnauthorizedCloseGameError),
    ):
        before = deepcopy(game)
        with pytest.raises(error):
            engine.close_game(game_id, token)
        assert game == before

    game.songmaster.connected = False
    game.songmaster.browser_audio_usable = False
    disconnected = deepcopy(game)
    with pytest.raises(UnauthorizedCloseGameError):
        engine.close_game(GAME_IDS[0], "songmaster-token")
    assert game == disconnected
    game.songmaster.connected = True
    engine.close_game(GAME_IDS[0], "songmaster-token")
    with pytest.raises(CloseGameNotAllowedError):
        engine.close_game(GAME_IDS[0], "songmaster-token")


def test_finished_invariants_reject_corrupted_voided_round_facts() -> None:
    engine, _ = make_engine()
    game = start_game(engine, *TRACKS)
    begin_playing(engine)
    engine.end_game(GAME_IDS[0], "songmaster-token")
    assert game.last_voided_round is not None
    game.last_voided_round = replace(
        game.last_voided_round,
        active_round=replace(
            game.last_voided_round.active_round,
            round_id="not-a-uuid",  # type: ignore[arg-type]
        ),
    )

    with pytest.raises(DomainInvariantError, match="identities must be UUIDs"):
        engine.validate_invariants()
