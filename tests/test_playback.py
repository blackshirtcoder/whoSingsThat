from copy import deepcopy
from dataclasses import FrozenInstanceError
from decimal import Decimal

import pytest

from who_sings_that.domain.deck import Deck, DeckEntry, TrackReference
from who_sings_that.domain.engine import GameEngine
from who_sings_that.domain.playback import (
    DeckCandidateSelection,
    calculate_effective_playback_offset,
    is_duration_eligible,
    select_deck_candidate,
)


def make_deck(*durations: float | Decimal) -> Deck:
    return tuple(
        DeckEntry(
            original_position=position,
            track=TrackReference(
                track_id=f"track-{position}", duration_seconds=duration
            ),
        )
        for position, duration in enumerate(durations, start=1)
    )


@pytest.mark.parametrize(
    ("track_duration", "expected"),
    [
        (30, True),
        (Decimal("29.999"), False),
        (Decimal("30.001"), True),
    ],
)
def test_duration_eligibility_includes_exact_boundary(
    track_duration: float | Decimal, expected: bool
) -> None:
    track = TrackReference("track", track_duration)

    assert is_duration_eligible(track, 30) is expected


@pytest.mark.parametrize(
    ("track_duration", "configured_offset", "expected_offset"),
    [
        (30, 30, Decimal(0)),
        (35, 30, Decimal(5)),
        (100, 30, Decimal(30)),
    ],
)
def test_effective_offset_is_clamped_to_latest_valid_start(
    track_duration: int, configured_offset: int, expected_offset: Decimal
) -> None:
    track = TrackReference("track", track_duration)

    assert (
        calculate_effective_playback_offset(track, configured_offset, 30)
        == expected_offset
    )


def test_consecutive_ineligible_entries_are_returned_before_candidate() -> None:
    deck = make_deck(10, 29, 60)

    selection = select_deck_candidate(deck, 0, 30)

    assert selection == DeckCandidateSelection(
        deck_pointer=2,
        skipped_entries=(deck[0], deck[1]),
        candidate=deck[2],
    )
    assert selection.exhausted is False


def test_exhaustion_returns_all_remaining_ineligible_entries() -> None:
    deck = make_deck(60, 20, 29)

    selection = select_deck_candidate(deck, 1, 30)

    assert selection.deck_pointer == len(deck)
    assert selection.skipped_entries == (deck[1], deck[2])
    assert selection.candidate is None
    assert selection.exhausted is True


def test_pointer_at_deck_end_is_already_exhausted() -> None:
    deck = make_deck(30, 60)

    selection = select_deck_candidate(deck, len(deck), 30)

    assert selection == DeckCandidateSelection(
        deck_pointer=len(deck), skipped_entries=(), candidate=None
    )
    assert selection.exhausted is True


def test_selection_from_nonzero_pointer_never_reconsiders_earlier_entries() -> None:
    deck = make_deck(10, 20, 45)

    selection = select_deck_candidate(deck, 1, 30)

    assert selection.deck_pointer == 2
    assert selection.skipped_entries == (deck[1],)
    assert selection.candidate is deck[2]


def test_candidate_preserves_immutable_original_deck_position() -> None:
    deck = make_deck(10, 20, 45)

    selection = select_deck_candidate(deck, 0, 30)

    assert selection.candidate is deck[2]
    assert selection.candidate.original_position == 3
    with pytest.raises(FrozenInstanceError):
        selection.candidate.original_position = 1  # type: ignore[misc,union-attr]


def test_selection_does_not_mutate_deck_or_game_session() -> None:
    deck = make_deck(10, 45)
    original_deck = deepcopy(deck)
    engine = GameEngine()
    game = engine.create_game("creator-token", "Creator")
    original_game = deepcopy(game)

    selection = select_deck_candidate(deck, 0, 30)

    assert selection.candidate is deck[1]
    assert deck == original_deck
    assert game == original_game
    assert game.deck is None
    assert game.deck_pointer == 0


@pytest.mark.parametrize(
    ("track_duration", "playback_duration", "error", "message"),
    [
        (0, 30, ValueError, "track duration must be greater than zero"),
        (-1, 30, ValueError, "track duration must be greater than zero"),
        (float("nan"), 30, ValueError, "track duration must be finite"),
        (30, 0, ValueError, "playback duration must be greater than zero"),
        (30, -1, ValueError, "playback duration must be greater than zero"),
        (30, float("inf"), ValueError, "playback duration must be finite"),
        (True, 30, TypeError, "track duration must be a number"),
        (30, False, TypeError, "playback duration must be a number"),
    ],
)
def test_duration_boundaries_are_validated(
    track_duration: object,
    playback_duration: object,
    error: type[Exception],
    message: str,
) -> None:
    track = TrackReference("track", track_duration)  # type: ignore[arg-type]

    with pytest.raises(error, match=message):
        is_duration_eligible(
            track, playback_duration  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    ("configured_offset", "error", "message"),
    [
        (-1, ValueError, "configured offset must not be negative"),
        (float("nan"), ValueError, "configured offset must be finite"),
        (True, TypeError, "configured offset must be a number"),
    ],
)
def test_effective_offset_boundaries_are_validated(
    configured_offset: object, error: type[Exception], message: str
) -> None:
    with pytest.raises(error, match=message):
        calculate_effective_playback_offset(
            TrackReference("track", 60),
            configured_offset,  # type: ignore[arg-type]
            30,
        )


def test_effective_offset_rejects_ineligible_track() -> None:
    with pytest.raises(
        ValueError, match="track duration must be at least playback duration"
    ):
        calculate_effective_playback_offset(TrackReference("track", 29), 0, 30)


@pytest.mark.parametrize(
    ("pointer", "error", "message"),
    [
        (-1, ValueError, "between zero and deck size inclusive"),
        (3, ValueError, "between zero and deck size inclusive"),
        (True, TypeError, "must be an integer"),
        (1.5, TypeError, "must be an integer"),
    ],
)
def test_deck_pointer_boundaries_are_validated(
    pointer: object, error: type[Exception], message: str
) -> None:
    with pytest.raises(error, match=message):
        select_deck_candidate(
            make_deck(30, 30),
            pointer,  # type: ignore[arg-type]
            30,
        )
