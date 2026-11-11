"""Pure construction of immutable game decks."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from random import shuffle as random_shuffle


class EmptyDeckError(ValueError):
    """Raised when selected playlists contain no unique tracks."""


@dataclass(frozen=True, slots=True)
class TrackReference:
    """An internal immutable track snapshot used by the game deck.

    Artist and title are retained for authoritative reveal but must never be
    serialized to clients before that transition. ``None`` remains supported
    for lower-level synthetic playback/deck tests that do not exercise reveal.
    """

    track_id: int
    duration_seconds: float | Decimal
    artist: str | None = None
    title: str | None = None


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
