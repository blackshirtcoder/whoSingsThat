from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from who_sings_that.domain import (
    ActiveRound,
    AudioPreparationStatus,
    DeckSkipReason,
    DomainInvariantError,
    GameEndReason,
    GameEngine,
    GameFinalizationResult,
    GameSettings,
    GameState,
    PreparationEventInapplicableError,
    PreparationFailureAcknowledgementNotAllowedError,
    PreparationOutcome,
    ResolvedPlaylist,
    SelectedPlaylistSnapshot,
    TrackReference,
    UnauthorizedPreparationFailureAcknowledgementError,
)

GAME_ID = UUID("11111111-1111-4111-8111-111111111111")
PREPARATION_ID = UUID("22222222-2222-4222-8222-222222222222")
ROUND_ID = UUID("33333333-3333-4333-8333-333333333333")
CREATED_AT = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
STARTED_AT = datetime(2026, 11, 10, 9, 1, tzinfo=UTC)
PLAYBACK_STARTED_AT = datetime(2026, 11, 10, 9, 2, tzinfo=UTC)
ENDED_AT = datetime(2026, 11, 10, 9, 3, tzinfo=UTC)
PLAYLIST_CREATED_AT = datetime(2025, 1, 2, 3, 4, tzinfo=UTC)


class SequenceClock:
    def __init__(self, *values: datetime) -> None:
        self._values = iter(values)

    def __call__(self) -> datetime:
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


def make_engine(settings: GameSettings | None = None) -> GameEngine:
    return GameEngine(
        settings,
        clock=SequenceClock(CREATED_AT, STARTED_AT, PLAYBACK_STARTED_AT, ENDED_AT),
        game_id_factory=lambda: GAME_ID,
        preparation_id_factory=lambda: PREPARATION_ID,
        round_id_factory=lambda: ROUND_ID,
        shuffle=lambda tracks: None,
    )


def prepare_game(
    *tracks: TrackReference,
    audio_targets: tuple[str, ...] = ("songmaster-token",),
    settings: GameSettings | None = None,
):
    engine = make_engine(settings)
    game = engine.create_game("songmaster-token", "Songmaster")
    for index, token in enumerate(audio_targets):
        if token == "songmaster-token":
            continue
        engine.join_game(token, f"Player {index}")
        engine.set_audio_preference(token, True)
    for token in audio_targets:
        engine.set_browser_audio_capability(token, True)
    engine.select_playlist("songmaster-token", "playlist")
    engine.start_game("songmaster-token", [resolved_playlist(*tracks)])
    pending = engine.play_song("songmaster-token")
    return engine, game, pending


def ready(engine: GameEngine, token: str):
    return engine.record_audio_preparation_ready(
        GAME_ID,
        PREPARATION_ID,
        token,
    )


def failed(engine: GameEngine, token: str):
    return engine.record_audio_preparation_failed(
        GAME_ID,
        PREPARATION_ID,
        token,
    )


def acknowledge(engine: GameEngine):
    return engine.acknowledge_audio_preparation_failure(
        GAME_ID,
        PREPARATION_ID,
        "songmaster-token",
    )


def test_one_ready_target_immediately_takes_transition_e() -> None:
    engine, game, _ = prepare_game(TrackReference(10, 180))

    result = ready(engine, "songmaster-token")

    assert result.outcome is PreparationOutcome.PLAYING
    assert result.ready_browser_tokens == frozenset({"songmaster-token"})
    assert result.active_round is game.active_round
    assert engine.state is GameState.PLAYING


def test_transition_c_initializes_every_target_as_pending() -> None:
    _, game, pending = prepare_game(
        TrackReference(10, 180),
        audio_targets=("songmaster-token", "player-token"),
    )

    assert game.audio_preparation_responses is not None
    assert game.audio_preparation_responses.preparation_id == pending.preparation_id
    assert game.audio_preparation_responses.target_statuses == {
        "songmaster-token": AudioPreparationStatus.PENDING,
        "player-token": AudioPreparationStatus.PENDING,
    }
    assert game.audio_preparation_responses.outcome_determined is False
    assert (
        game.audio_preparation_responses.failure_acknowledgement_pending is False
    )


def test_one_failed_target_finalizes_without_consuming_before_acknowledgement() -> None:
    engine, game, pending = prepare_game(TrackReference(10, 180))
    pointer_before = game.deck_pointer

    result = failed(engine, "songmaster-token")

    assert result.outcome is PreparationOutcome.FAILURE_PENDING_ACKNOWLEDGEMENT
    assert engine.state is GameState.AUDIO_PREPARING
    assert game.deck_pointer == pointer_before
    assert game.pending_audio_preparation is pending
    assert game.active_round is None
    assert game.audio_preparation_responses is not None
    assert game.audio_preparation_responses.outcome_determined is True
    assert game.audio_preparation_responses.failure_acknowledgement_pending is True


def test_first_ready_waits_for_other_pending_targets() -> None:
    engine, game, _ = prepare_game(
        TrackReference(10, 180),
        audio_targets=("songmaster-token", "player-token"),
    )

    result = ready(engine, "songmaster-token")

    assert result.outcome is PreparationOutcome.COLLECTING
    assert result.pending_browser_tokens == frozenset({"player-token"})
    assert engine.state is GameState.AUDIO_PREPARING
    assert game.active_round is None


def test_all_ready_targets_enter_playing_with_every_ready_client() -> None:
    engine, game, _ = prepare_game(
        TrackReference(10, 180),
        audio_targets=("songmaster-token", "player-token"),
    )
    ready(engine, "songmaster-token")

    result = ready(engine, "player-token")

    expected = frozenset({"songmaster-token", "player-token"})
    assert result.outcome is PreparationOutcome.PLAYING
    assert result.ready_browser_tokens == expected
    assert game.active_round is not None
    assert game.active_round.playback_clients == expected


def test_mixed_ready_and_failed_enters_playing_with_only_ready_targets() -> None:
    engine, game, _ = prepare_game(
        TrackReference(10, 180),
        audio_targets=("songmaster-token", "player-token"),
    )
    ready(engine, "songmaster-token")

    result = failed(engine, "player-token")

    assert result.outcome is PreparationOutcome.PLAYING
    assert result.ready_browser_tokens == frozenset({"songmaster-token"})
    assert result.failed_browser_tokens == frozenset({"player-token"})
    assert game.active_round is not None
    assert game.active_round.playback_clients == frozenset({"songmaster-token"})


def test_all_failed_targets_require_acknowledgement() -> None:
    engine, game, _ = prepare_game(
        TrackReference(10, 180),
        audio_targets=("songmaster-token", "player-token"),
    )
    failed(engine, "songmaster-token")

    result = failed(engine, "player-token")

    assert result.outcome is PreparationOutcome.FAILURE_PENDING_ACKNOWLEDGEMENT
    assert result.failed_browser_tokens == frozenset(
        {"songmaster-token", "player-token"}
    )
    assert game.active_round is None
    assert engine.state is GameState.AUDIO_PREPARING


def test_timeout_with_ready_target_enters_playing_without_pending_target() -> None:
    engine, game, _ = prepare_game(
        TrackReference(10, 180),
        audio_targets=("songmaster-token", "player-token"),
    )
    ready(engine, "songmaster-token")

    result = engine.audio_preparation_timed_out(GAME_ID, PREPARATION_ID)

    assert result.outcome is PreparationOutcome.PLAYING
    assert result.ready_browser_tokens == frozenset({"songmaster-token"})
    assert result.pending_browser_tokens == frozenset({"player-token"})
    assert game.active_round is not None
    assert game.active_round.playback_clients == frozenset({"songmaster-token"})


def test_timeout_with_zero_ready_targets_requires_acknowledgement() -> None:
    engine, game, _ = prepare_game(
        TrackReference(10, 180),
        audio_targets=("songmaster-token", "player-token"),
    )
    failed(engine, "player-token")

    result = engine.audio_preparation_timed_out(GAME_ID, PREPARATION_ID)

    assert result.outcome is PreparationOutcome.FAILURE_PENDING_ACKNOWLEDGEMENT
    assert result.pending_browser_tokens == frozenset({"songmaster-token"})
    assert game.audio_preparation_responses is not None
    assert game.audio_preparation_responses.failure_acknowledgement_pending is True
    assert game.deck_pointer == 0


@pytest.mark.parametrize("late_status", ["ready", "failed", "timeout"])
def test_late_events_cannot_change_finalized_failure(late_status: str) -> None:
    engine, game, _ = prepare_game(TrackReference(10, 180))
    failed(engine, "songmaster-token")
    before = deepcopy(game)

    with pytest.raises(PreparationEventInapplicableError):
        if late_status == "ready":
            ready(engine, "songmaster-token")
        elif late_status == "failed":
            failed(engine, "songmaster-token")
        else:
            engine.audio_preparation_timed_out(GAME_ID, PREPARATION_ID)

    assert game == before
    assert engine.state is GameState.AUDIO_PREPARING


@pytest.mark.parametrize("second_status", ["ready", "failed"])
def test_duplicate_or_conflicting_response_preserves_first_terminal_outcome(
    second_status: str,
) -> None:
    engine, game, _ = prepare_game(
        TrackReference(10, 180),
        audio_targets=("songmaster-token", "player-token"),
    )
    ready(engine, "songmaster-token")
    assert game.audio_preparation_responses is not None

    with pytest.raises(PreparationEventInapplicableError, match="first accepted"):
        if second_status == "ready":
            ready(engine, "songmaster-token")
        else:
            failed(engine, "songmaster-token")

    assert game.audio_preparation_responses.target_statuses[
        "songmaster-token"
    ] is AudioPreparationStatus.READY
    assert engine.state is GameState.AUDIO_PREPARING


def test_non_target_response_is_inapplicable() -> None:
    engine, game, _ = prepare_game(TrackReference(10, 180))
    before = deepcopy(game)

    with pytest.raises(PreparationEventInapplicableError, match="targeted"):
        ready(engine, "not-targeted")

    assert game == before


@pytest.mark.parametrize(
    ("game_id", "preparation_id"),
    [(uuid4(), PREPARATION_ID), (GAME_ID, uuid4())],
)
def test_wrong_game_or_preparation_identity_is_inapplicable(
    game_id: UUID,
    preparation_id: UUID,
) -> None:
    engine, game, _ = prepare_game(TrackReference(10, 180))
    before = deepcopy(game)

    with pytest.raises(PreparationEventInapplicableError):
        engine.record_audio_preparation_ready(
            game_id,
            preparation_id,
            "songmaster-token",
        )

    assert game == before


def test_broken_current_preparation_bookkeeping_is_an_invariant_error() -> None:
    engine, game, _ = prepare_game(TrackReference(10, 180))
    game.audio_preparation_responses = None

    with pytest.raises(DomainInvariantError, match="response state"):
        ready(engine, "songmaster-token")


def test_premature_and_unauthorized_failure_acknowledgements_are_rejected() -> None:
    engine, game, _ = prepare_game(
        TrackReference(10, 180),
        audio_targets=("songmaster-token", "player-token"),
    )
    before = deepcopy(game)

    with pytest.raises(PreparationFailureAcknowledgementNotAllowedError):
        acknowledge(engine)
    assert game == before

    failed(engine, "songmaster-token")
    failed(engine, "player-token")
    before = deepcopy(game)
    with pytest.raises(UnauthorizedPreparationFailureAcknowledgementError):
        engine.acknowledge_audio_preparation_failure(
            GAME_ID,
            PREPARATION_ID,
            "player-token",
        )
    assert game == before


@pytest.mark.parametrize(
    ("game_id", "preparation_id"),
    [(uuid4(), PREPARATION_ID), (GAME_ID, uuid4())],
)
def test_stale_failure_acknowledgement_is_rejected_without_mutation(
    game_id: UUID,
    preparation_id: UUID,
) -> None:
    engine, game, _ = prepare_game(TrackReference(10, 180))
    failed(engine, "songmaster-token")
    before = deepcopy(game)

    with pytest.raises(PreparationFailureAcknowledgementNotAllowedError):
        engine.acknowledge_audio_preparation_failure(
            game_id,
            preparation_id,
            "songmaster-token",
        )

    assert game == before


def test_transition_d_skips_once_preserves_display_and_score_and_finds_next() -> None:
    engine, game, _ = prepare_game(
        TrackReference(10, 180),
        TrackReference(20, 180),
    )
    game.songmaster.committed_score = 75
    game.displayed_deck_position = 1
    game.displayed_artist = "Previous Artist"
    game.displayed_title = "Previous Title"
    failed(engine, "songmaster-token")

    returned = acknowledge(engine)

    assert returned is game
    assert engine.state is GameState.ROUND_COMPLETE
    assert game.deck_pointer == 1
    assert game.pending_audio_preparation is None
    assert game.audio_preparation_responses is None
    assert game.active_round is None
    assert game.skipped_deck_entries[-1].reason is (
        DeckSkipReason.AUDIO_PREPARATION_FAILED
    )
    assert game.skipped_deck_entries[-1].original_position == 1
    assert game.songmaster.committed_score == 75
    assert game.displayed_deck_position == 1
    assert game.displayed_artist == "Previous Artist"
    assert game.displayed_title == "Previous Title"

    with pytest.raises(PreparationFailureAcknowledgementNotAllowedError):
        acknowledge(engine)
    assert game.deck_pointer == 1


def test_transition_d_round_complete_entry_scans_additional_short_candidates() -> None:
    engine, game, _ = prepare_game(
        TrackReference(10, 180),
        TrackReference(20, 10),
        TrackReference(30, 180),
    )
    failed(engine, "songmaster-token")

    acknowledge(engine)

    assert engine.state is GameState.ROUND_COMPLETE
    assert game.deck_pointer == 2
    assert [skip.reason for skip in game.skipped_deck_entries] == [
        DeckSkipReason.AUDIO_PREPARATION_FAILED,
        DeckSkipReason.DURATION_INELIGIBLE,
    ]
    assert game.deck is not None
    assert game.deck[game.deck_pointer].track.track_id == 30


def test_transition_d_exhaustion_waits_in_round_complete_for_transition_k() -> None:
    engine, game, _ = prepare_game(TrackReference(10, 180))
    game.displayed_deck_position = 1
    game.displayed_artist = "Previous Artist"
    game.displayed_title = "Previous Title"
    failed(engine, "songmaster-token")

    acknowledge(engine)

    assert engine.state is GameState.ROUND_COMPLETE
    assert game.deck_pointer == 1
    assert game.deck_exhausted is True
    assert game.end_reason is None
    assert game.ended_at is None
    assert game.final_standings == ()
    assert game.active_round is None
    assert game.displayed_deck_position == 1
    assert game.displayed_artist == "Previous Artist"
    assert game.displayed_title == "Previous Title"

    finalization = engine.play_song("songmaster-token")

    assert isinstance(finalization, GameFinalizationResult)
    assert engine.state is GameState.FINISHED
    assert game.end_reason is GameEndReason.DECK_EXHAUSTED
    # No transition E occurred, so explicit K uses the next clock value.
    assert game.ended_at == PLAYBACK_STARTED_AT


def test_transition_e_creates_round_from_frozen_preparation_without_consuming() -> None:
    settings = GameSettings(
        playback_offset_seconds=35,
        playback_duration_seconds=30,
        speed_bonus_plateau_percent=Decimal("12.5"),
    )
    engine, game, pending = prepare_game(
        TrackReference(10, 40),
        audio_targets=("songmaster-token", "player-token"),
        settings=settings,
    )
    game.songmaster.committed_score = 50
    pointer_before = game.deck_pointer
    assert game.active_round is None
    ready(engine, "songmaster-token")

    result = ready(engine, "player-token")

    active_round = game.active_round
    assert isinstance(active_round, ActiveRound)
    assert result.active_round is active_round
    assert active_round.round_id == ROUND_ID
    assert active_round.preparation_id == PREPARATION_ID
    assert active_round.deck_entry is pending.deck_entry
    assert active_round.original_deck_position == 1
    assert active_round.track_id == 10
    assert active_round.configured_playback_offset_seconds == 35
    assert active_round.effective_playback_offset_seconds == Decimal(10)
    assert active_round.playback_duration_seconds == 30
    assert active_round.speed_bonus_plateau_percent == Decimal("12.5")
    assert active_round.buzz_eligible_browser_tokens == (
        pending.buzz_eligible_browser_tokens
    )
    assert active_round.playback_clients == frozenset(
        {"songmaster-token", "player-token"}
    )
    assert active_round.playback_started_at == PLAYBACK_STARTED_AT
    assert game.deck_pointer == pointer_before
    assert game.pending_audio_preparation is None
    assert game.audio_preparation_responses is None
    assert game.displayed_deck_position == 1
    assert game.displayed_artist is None
    assert game.displayed_title is None
    assert game.buzzing_enabled is True
    assert game.songmaster.committed_score == 50
    assert engine.state is GameState.PLAYING
    engine.validate_invariants()


def test_default_round_identity_generation_is_unique() -> None:
    def create_round_id() -> UUID:
        engine = GameEngine(
            clock=SequenceClock(CREATED_AT, STARTED_AT, PLAYBACK_STARTED_AT),
            shuffle=lambda tracks: None,
        )
        game = engine.create_game("songmaster-token", "Songmaster")
        engine.set_browser_audio_capability("songmaster-token", True)
        engine.select_playlist("songmaster-token", "playlist")
        engine.start_game(
            "songmaster-token",
            [resolved_playlist(TrackReference(10, 180))],
        )
        pending = engine.play_song("songmaster-token")
        engine.record_audio_preparation_ready(
            game.game_session_id,
            pending.preparation_id,
            "songmaster-token",
        )
        assert game.active_round is not None
        return game.active_round.round_id

    assert create_round_id() != create_round_id()


def test_stale_timeout_after_transition_e_cannot_change_playing_round() -> None:
    engine, game, _ = prepare_game(TrackReference(10, 180))
    ready(engine, "songmaster-token")
    before = deepcopy(game)

    with pytest.raises(PreparationEventInapplicableError):
        engine.audio_preparation_timed_out(GAME_ID, PREPARATION_ID)

    assert game == before
    assert engine.state is GameState.PLAYING
