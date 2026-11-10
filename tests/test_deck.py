from dataclasses import FrozenInstanceError

import pytest

from who_sings_that.domain.deck import (
    DeckEntry,
    EmptyDeckError,
    TrackReference,
    build_deck,
)


def unchanged(tracks: list[TrackReference]) -> None:
    """Provide a deterministic no-op shuffle for order-focused tests."""


def test_tracks_are_deduplicated_by_stable_identity_within_and_across_playlists(
) -> None:
    first_a = TrackReference(1, 180)
    repeated_a = TrackReference(1, 180)
    track_b = TrackReference(2, 180)
    track_c = TrackReference(3, 180)

    deck = build_deck(
        [
            [first_a, repeated_a, track_b],
            [track_b, repeated_a, track_c],
        ],
        shuffle=unchanged,
    )

    assert [entry.track.track_id for entry in deck] == [
        1,
        2,
        3,
    ]
    assert deck[0].track is first_a


def test_shuffle_is_called_exactly_once_with_only_unique_tracks() -> None:
    shuffle_calls: list[list[int]] = []

    def recording_shuffle(tracks: list[TrackReference]) -> None:
        shuffle_calls.append([track.track_id for track in tracks])
        tracks.reverse()

    deck = build_deck(
        [
            [TrackReference(1, 180), TrackReference(1, 180)],
            [TrackReference(2, 180), TrackReference(3, 180)],
        ],
        shuffle=recording_shuffle,
    )

    assert shuffle_calls == [[1, 2, 3]]
    assert [entry.track.track_id for entry in deck] == [
        3,
        2,
        1,
    ]


def test_original_positions_are_one_based_and_immutable() -> None:
    deck = build_deck(
        [[TrackReference(2, 180), TrackReference(1, 180)]],
        shuffle=unchanged,
    )

    assert isinstance(deck, tuple)
    assert [entry.original_position for entry in deck] == [1, 2]
    with pytest.raises(FrozenInstanceError):
        deck[0].original_position = 2  # type: ignore[misc]


@pytest.mark.parametrize("selected_playlist_tracks", [[], [[], []]])
def test_empty_resulting_deck_is_rejected(
    selected_playlist_tracks: list[list[TrackReference]],
) -> None:
    shuffle_called = False

    def recording_shuffle(tracks: list[TrackReference]) -> None:
        nonlocal shuffle_called
        shuffle_called = True

    with pytest.raises(EmptyDeckError, match="without tracks"):
        build_deck(selected_playlist_tracks, shuffle=recording_shuffle)

    assert shuffle_called is False


def test_injected_shuffle_produces_deterministic_order_and_positions() -> None:
    desired_order = {3: 0, 1: 1, 2: 2}

    def deterministic_shuffle(tracks: list[TrackReference]) -> None:
        tracks.sort(key=lambda track: desired_order[track.track_id])

    deck = build_deck(
        [
            [TrackReference(1, 180), TrackReference(2, 180)],
            [TrackReference(3, 180)],
        ],
        shuffle=deterministic_shuffle,
    )

    assert deck == (
        DeckEntry(1, TrackReference(3, 180)),
        DeckEntry(2, TrackReference(1, 180)),
        DeckEntry(3, TrackReference(2, 180)),
    )
