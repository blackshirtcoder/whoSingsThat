"""Pure playback eligibility and deck-candidate selection."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .deck import Deck, DeckEntry, TrackReference


@dataclass(frozen=True, slots=True)
class DeckCandidateSelection:
    """The result of scanning a deck from one authoritative pointer."""

    deck_pointer: int
    skipped_entries: tuple[DeckEntry, ...]
    candidate: DeckEntry | None

    @property
    def exhausted(self) -> bool:
        """Return whether no duration-eligible candidate remains."""

        return self.candidate is None


def _as_decimal(value: float | Decimal, *, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise TypeError(f"{name} must be a number")

    try:
        converted = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f"{name} must be finite") from error

    if not converted.is_finite():
        raise ValueError(f"{name} must be finite")
    return converted


def _track_duration(track: TrackReference) -> Decimal:
    duration = _as_decimal(track.duration_seconds, name="track duration")
    if duration <= 0:
        raise ValueError("track duration must be greater than zero")
    return duration


def _playback_duration(value: float | Decimal) -> Decimal:
    duration = _as_decimal(value, name="playback duration")
    if duration <= 0:
        raise ValueError("playback duration must be greater than zero")
    return duration


def is_duration_eligible(
    track: TrackReference, playback_duration_seconds: float | Decimal
) -> bool:
    """Return whether ``track`` is long enough for the playback duration."""

    return _track_duration(track) >= _playback_duration(playback_duration_seconds)


def calculate_effective_playback_offset(
    track: TrackReference,
    configured_offset_seconds: float | Decimal,
    playback_duration_seconds: float | Decimal,
) -> Decimal:
    """Return the configured offset clamped to the track's latest valid start."""

    track_duration = _track_duration(track)
    playback_duration = _playback_duration(playback_duration_seconds)
    configured_offset = _as_decimal(
        configured_offset_seconds, name="configured offset"
    )
    if configured_offset < 0:
        raise ValueError("configured offset must not be negative")
    if track_duration < playback_duration:
        raise ValueError("track duration must be at least playback duration")

    return min(configured_offset, track_duration - playback_duration)


def select_deck_candidate(
    deck: Deck,
    start_pointer: int,
    playback_duration_seconds: float | Decimal,
) -> DeckCandidateSelection:
    """Find the next duration-eligible entry without mutating authoritative state.

    The returned pointer identifies the candidate when one is found, or equals
    ``len(deck)`` when the remaining deck is exhausted. Entries before
    ``start_pointer`` are never reconsidered.
    """

    if isinstance(start_pointer, bool) or not isinstance(start_pointer, int):
        raise TypeError("start_pointer must be an integer")
    if not 0 <= start_pointer <= len(deck):
        raise ValueError("start_pointer must be between zero and deck size inclusive")

    playback_duration = _playback_duration(playback_duration_seconds)
    skipped_entries: list[DeckEntry] = []
    pointer = start_pointer

    while pointer < len(deck):
        entry = deck[pointer]
        if _track_duration(entry.track) >= playback_duration:
            return DeckCandidateSelection(
                deck_pointer=pointer,
                skipped_entries=tuple(skipped_entries),
                candidate=entry,
            )
        skipped_entries.append(entry)
        pointer += 1

    return DeckCandidateSelection(
        deck_pointer=pointer,
        skipped_entries=tuple(skipped_entries),
        candidate=None,
    )
