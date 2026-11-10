"""Minimal in-memory domain models for a Who Sings That? game."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Final
from unicodedata import category, normalize
from uuid import UUID

from .deck import Deck

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


@dataclass(frozen=True, slots=True)
class SelectedPlaylistSnapshot:
    """Immutable playlist metadata captured when a game starts."""

    playlist_id: str
    playlist_name: str
    playlist_created_at: datetime
    playlist_song_count: int


@dataclass(slots=True)
class GameSession:
    """The minimal live game aggregate needed while in the lobby."""

    game_session_id: UUID
    created_at: datetime
    settings: GameSettings
    members: dict[str, GameMember]
    songmaster_browser_token: str
    selected_playlist_ids: set[str] = field(default_factory=set)
    playlist_selection_editable: bool = True
    selected_playlist_snapshots: tuple[SelectedPlaylistSnapshot, ...] = ()
    deck: Deck | None = None
    deck_pointer: int = 0
    original_deck_size: int = 0
    deck_exhausted: bool = False
    started_at: datetime | None = None
    active_round: object | None = None

    @property
    def songmaster(self) -> GameMember:
        """Return the member currently assigned as Songmaster."""

        return self.members[self.songmaster_browser_token]
