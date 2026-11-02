"""Pure domain logic for Who Sings That?"""

from .engine import (
    ActiveGameExistsError,
    DomainInvariantError,
    DuplicatePlayerNameError,
    GameEngine,
    GameEngineError,
    InvalidCreateGameRequestError,
    InvalidDisconnectRequestError,
    InvalidJoinGameRequestError,
    MembershipOperationNotAllowedError,
    NoActiveGameError,
    SongmasterDisconnectRequiresFailoverError,
    UnknownGameMemberError,
)
from .models import (
    GameMember,
    GameSession,
    GameSettings,
    GameState,
    InvalidPlayerNameError,
    normalize_player_name,
    player_name_uniqueness_key,
)
from .scoring import ScoringChoice, calculate_points, speed_multiplier

__all__ = [
    "ActiveGameExistsError",
    "DomainInvariantError",
    "DuplicatePlayerNameError",
    "GameEngine",
    "GameEngineError",
    "GameMember",
    "GameSession",
    "GameSettings",
    "GameState",
    "InvalidCreateGameRequestError",
    "InvalidDisconnectRequestError",
    "InvalidJoinGameRequestError",
    "InvalidPlayerNameError",
    "MembershipOperationNotAllowedError",
    "NoActiveGameError",
    "ScoringChoice",
    "SongmasterDisconnectRequiresFailoverError",
    "UnknownGameMemberError",
    "calculate_points",
    "normalize_player_name",
    "player_name_uniqueness_key",
    "speed_multiplier",
]
