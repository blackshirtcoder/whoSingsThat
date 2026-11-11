"""Minimal in-memory domain models for a Who Sings That? game."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Final
from unicodedata import category, normalize
from uuid import UUID

from .deck import Deck, DeckEntry, TrackReference
from .scoring import ScoringChoice

MAX_PLAYER_NAME_LENGTH: Final = 24
_ALLOWED_PLAYER_NAME_PUNCTUATION: Final = frozenset(" -_'.")


class GameState(Enum):
    """Authoritative normal-flow states for V1."""

    NO_GAME = "no_game"
    LOBBY = "lobby"
    ROUND_COMPLETE = "round_complete"
    AUDIO_PREPARING = "audio_preparing"
    PLAYING = "playing"
    AWAITING_REVEAL = "awaiting_reveal"
    SCORING = "scoring"
    FINISHED = "finished"


class DeckSkipReason(Enum):
    """Permanent reasons that a structural deck entry was not played."""

    DURATION_INELIGIBLE = "duration_ineligible"
    AUDIO_PREPARATION_FAILED = "audio_preparation_failed"


class GameEndReason(Enum):
    """Authoritative reasons a game entered ``FINISHED``."""

    DECK_EXHAUSTED = "deck_exhausted"


class RoundTerminationReason(Enum):
    """Mutually exclusive authoritative outcomes of the playback interval."""

    BUZZ = "buzz"
    PLAYBACK_TIMEOUT = "playback_timeout"


class InvalidPlayerNameError(ValueError):
    """Raised when a display name cannot identify a game member."""


def normalize_player_name(display_name: str) -> str:
    """Return a validated display name while preserving its capitalization.

    Compatibility normalization is used so canonically or compatibly equivalent
    input has one representation. All runs of Unicode whitespace become a single
    ordinary space before the character and length checks are applied.
    """

    if not isinstance(display_name, str):
        raise TypeError("display_name must be a string")

    normalized = normalize("NFKC", display_name)
    normalized = " ".join(normalized.split())

    if not normalized:
        raise InvalidPlayerNameError("display_name must not be empty")
    if len(normalized) > MAX_PLAYER_NAME_LENGTH:
        raise InvalidPlayerNameError(
            f"display_name must be at most {MAX_PLAYER_NAME_LENGTH} characters"
        )

    invalid_character = next(
        (
            character
            for character in normalized
            if category(character)[0] not in {"L", "N"}
            and character not in _ALLOWED_PLAYER_NAME_PUNCTUATION
        ),
        None,
    )
    if invalid_character is not None:
        raise InvalidPlayerNameError(
            f"display_name contains an invalid character: {invalid_character!r}"
        )

    return normalized


def player_name_uniqueness_key(display_name: str) -> str:
    """Return the Unicode-normalized, case-folded key used for name uniqueness."""

    validated_name = normalize_player_name(display_name)
    return normalize("NFKC", validated_name.casefold())


@dataclass(slots=True)
class GameSettings:
    """Settings copied from installation defaults when a game is created."""

    playback_offset_seconds: int = 30
    playback_duration_seconds: int = 30
    speed_bonus_plateau_percent: Decimal = Decimal(10)


@dataclass(slots=True)
class GameMember:
    """A player's membership and committed score within one game."""

    browser_token: str
    display_name: str
    name_uniqueness_key: str
    committed_score: int = 0
    connected: bool = True
    audio_enabled: bool = False
    browser_audio_usable: bool = False

    @property
    def is_valid_audio_target(self) -> bool:
        """Return whether this connection can receive audio preparation work."""

        return self.connected and self.audio_enabled and self.browser_audio_usable


@dataclass(frozen=True, slots=True)
class PendingAudioPreparation:
    """Internal snapshot for one prospective round's preparation attempt.

    This object is authoritative domain data, not a client-facing payload. In
    particular, transports must not serialize the bound deck entry to clients.
    """

    preparation_id: UUID
    deck_entry: DeckEntry
    configured_playback_offset_seconds: int
    effective_playback_offset_seconds: Decimal
    playback_duration_seconds: int
    speed_bonus_plateau_percent: Decimal
    buzz_eligible_browser_tokens: frozenset[str]
    audio_target_browser_tokens: frozenset[str]


class AudioPreparationStatus(Enum):
    """One target's authoritative response to a preparation attempt."""

    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"


@dataclass(slots=True)
class AudioPreparationResponseState:
    """Mutable response ledger bound to one immutable preparation snapshot."""

    preparation_id: UUID
    target_statuses: dict[str, AudioPreparationStatus]
    outcome_determined: bool = False
    failure_acknowledgement_pending: bool = False

    @property
    def ready_browser_tokens(self) -> frozenset[str]:
        """Return targets whose first accepted terminal response was READY."""

        return frozenset(
            token
            for token, status in self.target_statuses.items()
            if status is AudioPreparationStatus.READY
        )

    @property
    def failed_browser_tokens(self) -> frozenset[str]:
        """Return targets whose first accepted terminal response was FAILED."""

        return frozenset(
            token
            for token, status in self.target_statuses.items()
            if status is AudioPreparationStatus.FAILED
        )

    @property
    def pending_browser_tokens(self) -> frozenset[str]:
        """Return targets that had not responded when inspected or finalized."""

        return frozenset(
            token
            for token, status in self.target_statuses.items()
            if status is AudioPreparationStatus.PENDING
        )


@dataclass(frozen=True, slots=True)
class ActiveRound:
    """Authoritative immutable start facts for one actual played round."""

    round_id: UUID
    preparation_id: UUID
    deck_entry: DeckEntry
    original_deck_position: int
    track_id: int
    configured_playback_offset_seconds: int
    effective_playback_offset_seconds: Decimal
    playback_duration_seconds: int
    speed_bonus_plateau_percent: Decimal
    buzz_eligible_browser_tokens: frozenset[str]
    playback_clients: frozenset[str]
    playback_started_at: datetime
    playback_started_monotonic: float
    playback_deadline_monotonic: float


@dataclass(frozen=True, slots=True)
class RoundTermination:
    """Immutable outcome facts established by transition F or G."""

    reason: RoundTerminationReason
    winning_browser_token: str | None = None
    buzz_elapsed_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class CompletedRound:
    """The most recently completed played round retained in live memory.

    This is the bounded lifecycle snapshot needed after transitions I/J and for
    replacement scoring, not the durable history schema planned for later.
    """

    active_round: ActiveRound
    termination: RoundTermination
    scoring_choice: ScoringChoice | None
    awarded_points: int


@dataclass(frozen=True, slots=True)
class SelectedPlaylistSnapshot:
    """Immutable playlist metadata captured when a game starts."""

    playlist_id: str
    playlist_name: str
    playlist_created_at: datetime
    playlist_song_count: int


@dataclass(frozen=True, slots=True)
class ResolvedPlaylist:
    """A selected playlist's immutable metadata and resolved valid tracks.

    Catalog lookup, filesystem checks, and structural media validation happen
    outside the domain engine. Start Game accepts their already-resolved result
    through this value object.
    """

    snapshot: SelectedPlaylistSnapshot
    tracks: tuple[TrackReference, ...]


@dataclass(frozen=True, slots=True)
class SkippedDeckEntry:
    """A permanent record of one skipped entry at its original deck position."""

    original_position: int
    track_id: int
    reason: DeckSkipReason


@dataclass(frozen=True, slots=True)
class FinalStanding:
    """One immutable competitive result frozen when a game finishes."""

    rank: int
    browser_token: str
    display_name: str
    score: int


@dataclass(slots=True)
class GameSession:
    """The live in-memory aggregate for the currently implemented states."""

    game_session_id: UUID
    created_at: datetime
    settings: GameSettings
    members: dict[str, GameMember]
    songmaster_browser_token: str
    selected_playlist_ids: set[str] | frozenset[str] = field(default_factory=set)
    playlist_selection_editable: bool = True
    selected_playlist_snapshots: tuple[SelectedPlaylistSnapshot, ...] = ()
    deck: Deck | None = None
    deck_pointer: int = 0
    original_deck_size: int = 0
    deck_exhausted: bool = False
    skipped_deck_entries: tuple[SkippedDeckEntry, ...] = ()
    started_at: datetime | None = None
    ended_at: datetime | None = None
    end_reason: GameEndReason | None = None
    final_standings: tuple[FinalStanding, ...] = ()
    active_round: ActiveRound | None = None
    round_termination: RoundTermination | None = None
    last_completed_round: CompletedRound | None = None
    scoring_correction_round_id: UUID | None = None
    pending_audio_preparation: PendingAudioPreparation | None = None
    audio_preparation_responses: AudioPreparationResponseState | None = None
    buzzing_enabled: bool = False
    displayed_deck_position: int | None = None
    displayed_artist: str | None = None
    displayed_title: str | None = None

    @property
    def songmaster(self) -> GameMember:
        """Return the member currently assigned as Songmaster."""

        return self.members[self.songmaster_browser_token]
