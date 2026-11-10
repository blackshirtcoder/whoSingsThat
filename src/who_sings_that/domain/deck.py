"""Pure construction of immutable game decks."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from random import shuffle as random_shuffle


class EmptyDeckError(ValueError):
    """Raised when selected playlists contain no unique tracks."""


@dataclass(frozen=True, slots=True)
class TrackReference:
    """A resolved catalog track identified by its stable catalog identity."""

    track_id: int
    duration_seconds: float | Decimal


@dataclass(frozen=True, slots=True)
class DeckEntry:
    """A track at its permanent, one-based position in a game deck."""

    original_position: int
    track: TrackReference


Deck = tuple[DeckEntry, ...]
Shuffle = Callable[[list[TrackReference]], None]


def build_deck(
    selected_playlist_tracks: Iterable[Iterable[TrackReference]],
    *,
    shuffle: Shuffle = random_shuffle,
) -> Deck:
    """Deduplicate and shuffle resolved playlist tracks into an immutable deck.

    Playlist resolution and track validation are deliberately outside this
    function. When the same stable track identity occurs more than once, the
    first resolved reference is retained. The unique references are shuffled
    exactly once before permanent positions are assigned.
    """

    unique_tracks: dict[int, TrackReference] = {}
    for playlist_tracks in selected_playlist_tracks:
        for track in playlist_tracks:
            unique_tracks.setdefault(track.track_id, track)

    if not unique_tracks:
        raise EmptyDeckError("cannot build a deck without tracks")

    shuffled_tracks = list(unique_tracks.values())
    shuffle(shuffled_tracks)

    return tuple(
        DeckEntry(original_position=position, track=track)
        for position, track in enumerate(shuffled_tracks, start=1)
    )
