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
    InvalidPlayerNameError,
    normalize_player_name,
    player_name_uniqueness_key,
)


class GameEngineError(RuntimeError):
    """Base class for game-engine request failures."""


class ActiveGameExistsError(GameEngineError):
    """Raised when Create Game is requested while a game is active."""


class InvalidCreateGameRequestError(GameEngineError, ValueError):
    """Raised when a Create Game request lacks a usable browser identity."""


class NoActiveGameError(GameEngineError):
    """Raised when a membership operation requires an active game."""


class MembershipOperationNotAllowedError(GameEngineError):
    """Raised when a membership operation is not implemented for the state."""


class InvalidJoinGameRequestError(GameEngineError, ValueError):
    """Raised when a Join Game request lacks a usable browser identity."""


class InvalidDisconnectRequestError(GameEngineError, ValueError):
    """Raised when a disconnect request lacks a usable browser identity."""


class DuplicatePlayerNameError(GameEngineError, ValueError):
    """Raised when a new member requests a name reserved by another member."""


class UnknownGameMemberError(GameEngineError):
    """Raised when a browser token has no membership in the active game."""


class SongmasterDisconnectRequiresFailoverError(GameEngineError):
    """Raised until the Songmaster reconnect-grace overlay is implemented."""


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

    def join_game(
        self, browser_token: str, display_name: str | None = None
    ) -> GameMember:
        """Join or reconnect to the active game while it is in ``LOBBY``.

        A token already in this game reconnects that exact member without
        requiring a display name. If a name is supplied for an existing token,
        it is ignored because the stored game-local identity is authoritative.
        A new token must supply a valid, unique display name.

        This is an internal LOBBY operation, not a state-machine transition.
        """

        game = self._lobby_game_for_membership_operation()
        token = self._validate_browser_token(
            browser_token, error_type=InvalidJoinGameRequestError
        )

        existing_member = game.members.get(token)
        if existing_member is not None:
            was_connected = existing_member.connected
            existing_member.connected = True
            try:
                self._validate_state_invariants(self._state)
            except Exception:
                existing_member.connected = was_connected
                raise
            return existing_member

        if display_name is None:
            raise InvalidJoinGameRequestError(
                "display_name is required for a new browser_token"
            )
        normalized_name = normalize_player_name(display_name)
        name_key = player_name_uniqueness_key(normalized_name)
        if any(
            member.name_uniqueness_key == name_key for member in game.members.values()
        ):
            raise DuplicatePlayerNameError(
                "display_name is already reserved in the active game"
            )

        new_member = GameMember(
            browser_token=token,
            display_name=normalized_name,
            name_uniqueness_key=name_key,
            committed_score=0,
            connected=True,
        )
        game.members[token] = new_member
        try:
            self._validate_state_invariants(self._state)
        except Exception:
            del game.members[token]
            raise
        return new_member

    def disconnect_member(self, browser_token: str) -> GameMember:
        """Mark a non-Songmaster LOBBY member disconnected without removing it.

        Unexpected Songmaster loss is deliberately rejected here because it
        requires the section-37 reconnect-grace/failover overlay. This lower-
        level operation must not claim to have handled that global behavior.
        Repeated disconnect notifications for another member are idempotent.
        """

        game = self._lobby_game_for_membership_operation()
        token = self._validate_browser_token(
            browser_token, error_type=InvalidDisconnectRequestError
        )
        member = game.members.get(token)
        if member is None:
            raise UnknownGameMemberError(
                "browser_token does not belong to the active game"
            )
        if token == game.songmaster_browser_token:
            raise SongmasterDisconnectRequiresFailoverError(
                "Songmaster disconnect requires the reconnect-grace/failover overlay"
            )

        was_connected = member.connected
        member.connected = False
        try:
            self._validate_state_invariants(self._state)
        except Exception:
            member.connected = was_connected
            raise
        return member

    def validate_invariants(self) -> None:
        """Validate the invariants for the engine's current executable state."""

        self._validate_state_invariants(self._state)

    @staticmethod
    def _validate_browser_token(
        browser_token: str,
        *,
        error_type: type[GameEngineError] = InvalidCreateGameRequestError,
    ) -> str:
        if not isinstance(browser_token, str):
            raise error_type("browser_token must be a string")
        if not browser_token:
            raise error_type("browser_token must not be empty")
        return browser_token

    def _lobby_game_for_membership_operation(self) -> GameSession:
        game = self._active_game
        if game is None:
            raise NoActiveGameError("there is no active game")
        if self._state is not GameState.LOBBY:
            raise MembershipOperationNotAllowedError(
                "membership operation is only implemented in LOBBY"
            )
        self._validate_state_invariants(self._state)
        return game

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
        name_keys: set[str] = set()
        for member in game.members.values():
            try:
                normalized_name = normalize_player_name(member.display_name)
                expected_name_key = player_name_uniqueness_key(member.display_name)
            except (InvalidPlayerNameError, TypeError) as error:
                raise DomainInvariantError(
                    "game members must have valid display names"
                ) from error
            if normalized_name != member.display_name:
                raise DomainInvariantError(
                    "game member display names must be stored normalized"
                )
            if member.name_uniqueness_key != expected_name_key:
                raise DomainInvariantError(
                    "game member name uniqueness key is inconsistent"
                )
            if member.name_uniqueness_key in name_keys:
                raise DomainInvariantError("game member display names must be unique")
            name_keys.add(member.name_uniqueness_key)
            if not isinstance(member.connected, bool):
                raise DomainInvariantError(
                    "game member connection state must be boolean"
                )
            if (
                isinstance(member.committed_score, bool)
                or not isinstance(member.committed_score, int)
                or member.committed_score < 0
            ):
                raise DomainInvariantError(
                    "game member committed score must be a non-negative integer"
                )
        songmaster_count = sum(
            member.browser_token == game.songmaster_browser_token
            for member in game.members.values()
        )
        if songmaster_count != 1:
            raise DomainInvariantError(
                "LOBBY requires exactly one member as Songmaster"
            )
        if not game.songmaster.connected:
            raise DomainInvariantError(
                "LOBBY requires a connected Songmaster until the disconnect overlay exists"
            )
        if game.deck is not None:
            raise DomainInvariantError("LOBBY cannot have an immutable deck")
        if isinstance(game.deck_pointer, bool) or game.deck_pointer != 0:
            raise DomainInvariantError("LOBBY deck pointer must be zero")
        if isinstance(game.original_deck_size, bool) or game.original_deck_size != 0:
            raise DomainInvariantError("LOBBY original deck size must be zero")
        if game.deck_exhausted is not False:
            raise DomainInvariantError("LOBBY deck cannot be exhausted")
        if game.started_at is not None:
            raise DomainInvariantError("LOBBY cannot have a game start timestamp")
        if game.selected_playlist_snapshots != ():
            raise DomainInvariantError("LOBBY cannot have selected-playlist snapshots")
        if game.active_round is not None:
            raise DomainInvariantError("LOBBY cannot have an active round")
        if not game.playlist_selection_editable:
            raise DomainInvariantError("playlist selection must be editable in LOBBY")
