"""Authoritative in-memory state transitions for the game domain."""

from collections.abc import Callable
from copy import deepcopy
from datetime import UTC, datetime
from uuid import UUID, uuid4

from .models import (
    GameMember,
    GameSession,
    GameSettings,
    GameState,
    normalize_player_name,
    player_name_uniqueness_key,
)


class GameEngineError(RuntimeError):
    """Base class for game-engine request failures."""


class ActiveGameExistsError(GameEngineError):
    """Raised when Create Game is requested while a game is active."""


class InvalidCreateGameRequestError(GameEngineError, ValueError):
    """Raised when a Create Game request lacks a usable browser identity."""


class DomainInvariantError(GameEngineError):
    """Raised when established domain facts do not satisfy the current state."""


class GameEngine:
    """Own the single authoritative live game and its normal-flow state."""

    def __init__(
        self,
        installation_defaults: GameSettings | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        game_id_factory: Callable[[], UUID] | None = None,
    ) -> None:
        self._installation_defaults = deepcopy(
            installation_defaults
            if installation_defaults is not None
            else GameSettings()
        )
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._game_id_factory = (
            game_id_factory if game_id_factory is not None else uuid4
        )
        self._state = GameState.NO_GAME
        self._active_game: GameSession | None = None
        self._enter_state(GameState.NO_GAME)
        self._validate_state_invariants(GameState.NO_GAME)

    @property
    def state(self) -> GameState:
        """Return the current authoritative state."""

        return self._state

    @property
    def active_game(self) -> GameSession | None:
        """Return the active game, if one exists."""

        return self._active_game

    def create_game(self, browser_token: str, display_name: str) -> GameSession:
        """Apply transition A (``NO_GAME`` to ``LOBBY``) atomically."""

        # The transition condition only inspects established engine facts.
        if self._state is not GameState.NO_GAME or self._active_game is not None:
            raise ActiveGameExistsError("an active game already exists")

        token = self._validate_browser_token(browser_token)
        normalized_name = normalize_player_name(display_name)

        # Transition-A-specific work is completed on a detached aggregate. It is
        # published only once every request value and generated fact is available.
        creator = GameMember(
            browser_token=token,
            display_name=normalized_name,
            name_uniqueness_key=player_name_uniqueness_key(normalized_name),
        )
        game = GameSession(
            game_session_id=self._game_id_factory(),
            created_at=self._clock(),
            settings=deepcopy(self._installation_defaults),
            members={token: creator},
            songmaster_browser_token=token,
        )

        previous_state = self._state
        self._active_game = game
        try:
            self._transition_to(GameState.LOBBY)
        except Exception:
            # Keep request processing atomic even if generated data violates an
            # entry invariant or a supplied dependency fails unexpectedly.
            self._active_game = None
            self._state = previous_state
            self._enter_state(previous_state)
            self._validate_state_invariants(previous_state)
            raise

        return game

    def validate_invariants(self) -> None:
        """Validate the invariants for the engine's current executable state."""

        self._validate_state_invariants(self._state)

    @staticmethod
    def _validate_browser_token(browser_token: str) -> str:
        if not isinstance(browser_token, str):
            raise InvalidCreateGameRequestError("browser_token must be a string")
        if not browser_token:
            raise InvalidCreateGameRequestError("browser_token must not be empty")
        return browser_token

    def _transition_to(self, target: GameState) -> None:
        """Enter a target state and always execute that state's entry actions."""

        self._state = target
        self._enter_state(target)
        self._validate_state_invariants(target)

    def _enter_state(self, state: GameState) -> None:
        match state:
            case GameState.NO_GAME | GameState.LOBBY:
                # These states have invariants but no entry actions in this phase.
                return
            case _:
                raise DomainInvariantError(
                    f"entry behavior for {state.name} is outside the current phase"
                )

    def _validate_state_invariants(self, state: GameState) -> None:
        match state:
            case GameState.NO_GAME:
                self._validate_no_game_invariants()
            case GameState.LOBBY:
                self._validate_lobby_invariants()
            case _:
                raise DomainInvariantError(
                    f"invariants for {state.name} are outside the current phase"
                )

    def _validate_no_game_invariants(self) -> None:
        if self._active_game is not None:
            raise DomainInvariantError("NO_GAME cannot have an active game")

    def _validate_lobby_invariants(self) -> None:
        game = self._active_game
        if game is None:
            raise DomainInvariantError("LOBBY requires an active game")
        if any(token != member.browser_token for token, member in game.members.items()):
            raise DomainInvariantError("game membership keys must be browser tokens")
        songmaster_count = sum(
            member.browser_token == game.songmaster_browser_token
            for member in game.members.values()
        )
        if songmaster_count != 1:
            raise DomainInvariantError(
                "LOBBY requires exactly one member as Songmaster"
            )
        if game.deck is not None:
            raise DomainInvariantError("LOBBY cannot have an immutable deck")
        if game.active_round is not None:
            raise DomainInvariantError("LOBBY cannot have an active round")
        if not game.playlist_selection_editable:
            raise DomainInvariantError("playlist selection must be editable in LOBBY")
