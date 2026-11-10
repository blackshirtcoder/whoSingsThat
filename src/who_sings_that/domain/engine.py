"""Authoritative in-memory state transitions for the game domain."""

from collections.abc import Callable, Iterable
from copy import deepcopy
from datetime import UTC, datetime
from random import shuffle as random_shuffle
from uuid import UUID, uuid4

from .deck import Deck, DeckEntry, Shuffle, TrackReference, build_deck
from .models import (
    DeckSkipReason,
    FinalStanding,
    GameEndReason,
    GameMember,
    GameSession,
    GameSettings,
    GameState,
    InvalidPlayerNameError,
    ResolvedPlaylist,
    SelectedPlaylistSnapshot,
    SkippedDeckEntry,
    normalize_player_name,
    player_name_uniqueness_key,
)
from .playback import is_duration_eligible, select_deck_candidate


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


class PlaylistSelectionNotAllowedError(GameEngineError):
    """Raised when playlist selection is requested outside ``LOBBY``."""


class UnauthorizedPlaylistSelectionError(GameEngineError):
    """Raised when someone other than the Songmaster changes playlist selection."""


class InvalidPlaylistIdError(GameEngineError, ValueError):
    """Raised when playlist selection is requested without a usable ID."""


class StartGameNotAllowedError(GameEngineError):
    """Raised when Start Game is requested outside ``LOBBY``."""


class UnauthorizedStartGameError(GameEngineError):
    """Raised when someone other than the current Songmaster requests Start Game."""


class NoPlaylistSelectedError(GameEngineError):
    """Raised when Start Game is requested without a selected playlist."""


class InvalidResolvedPlaylistError(GameEngineError, ValueError):
    """Raised when resolved playlist data is inconsistent with lobby selection."""


class GameEngine:
    """Own the single authoritative live game and its normal-flow state."""

    def __init__(
        self,
        installation_defaults: GameSettings | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        game_id_factory: Callable[[], UUID] | None = None,
        shuffle: Shuffle | None = None,
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
        self._shuffle = shuffle if shuffle is not None else random_shuffle
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

    def select_playlist(
        self, requesting_browser_token: str, playlist_id: str
    ) -> GameSession:
        """Select a playlist during ``LOBBY`` without changing state.

        Playlist IDs are opaque to the domain engine. An outer layer is
        responsible for confirming that the ID identifies a persistent playlist.
        Repeated selection is intentionally idempotent.
        """

        game = self._lobby_game_for_playlist_selection(requesting_browser_token)
        validated_playlist_id = self._validate_playlist_id(playlist_id)
        assert isinstance(game.selected_playlist_ids, set)
        game.selected_playlist_ids.add(validated_playlist_id)
        self._validate_state_invariants(self._state)
        return game

    def deselect_playlist(
        self, requesting_browser_token: str, playlist_id: str
    ) -> GameSession:
        """Deselect a playlist during ``LOBBY`` without changing state.

        Playlist IDs are opaque to the domain engine. Deselecting an ID that is
        not currently selected is intentionally idempotent.
        """

        game = self._lobby_game_for_playlist_selection(requesting_browser_token)
        validated_playlist_id = self._validate_playlist_id(playlist_id)
        assert isinstance(game.selected_playlist_ids, set)
        game.selected_playlist_ids.discard(validated_playlist_id)
        self._validate_state_invariants(self._state)
        return game

    def start_game(
        self,
        requesting_browser_token: str,
        resolved_playlists: Iterable[ResolvedPlaylist],
    ) -> GameSession:
        """Apply transition B (``LOBBY`` to ``ROUND_COMPLETE``).

        ``resolved_playlists`` is the catalog/filesystem boundary: every item
        supplies immutable playlist metadata plus tracks that an outer service
        has already resolved and structurally validated. The aggregate is
        prepared without mutation. Once preparation succeeds, transition facts
        are committed to the one authoritative session before normal target-state
        entry runs to completion as one serialized authoritative event.
        """

        if self._state is not GameState.LOBBY:
            raise StartGameNotAllowedError("Start Game is only allowed in LOBBY")
        game = self._active_game
        if game is None:
            raise DomainInvariantError("LOBBY requires an active game")

        token = self._validate_browser_token(
            requesting_browser_token,
            error_type=UnauthorizedStartGameError,
        )
        if token != game.songmaster_browser_token:
            raise UnauthorizedStartGameError(
                "only the current Songmaster may start the game"
            )
        if not game.selected_playlist_ids:
            raise NoPlaylistSelectedError(
                "at least one playlist must be selected before Start Game"
            )

        # Validate and prepare every expected failure point before committing any
        # Start Game fact to the authoritative session.
        self._validate_state_invariants(GameState.LOBBY)
        playlists = self._validate_resolved_playlists(game, resolved_playlists)
        locked_playlist_ids = frozenset(game.selected_playlist_ids)
        playlist_snapshots = tuple(playlist.snapshot for playlist in playlists)
        # Transition-B-specific actions. build_deck owns the one and only
        # shuffle; duration eligibility remains a ROUND_COMPLETE entry action.
        deck = build_deck(
            (playlist.tracks for playlist in playlists),
            shuffle=self._shuffle,
        )
        started_at = self._clock()
        self._validate_prepared_start(
            locked_playlist_ids,
            playlist_snapshots,
            deck,
            started_at,
        )

        # From here onward the serialized authoritative event is committed.
        # Unexpected entry/invariant failures are programming errors and are not
        # hidden by whole-aggregate rollback.
        game.selected_playlist_ids = locked_playlist_ids
        game.selected_playlist_snapshots = playlist_snapshots
        game.deck = deck
        game.deck_pointer = 0
        game.original_deck_size = len(deck)
        game.playlist_selection_editable = False
        game.started_at = started_at

        self._transition_to(GameState.ROUND_COMPLETE)

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

    @staticmethod
    def _validate_playlist_id(playlist_id: str) -> str:
        if not isinstance(playlist_id, str):
            raise InvalidPlaylistIdError("playlist_id must be a string")
        if not playlist_id.strip():
            raise InvalidPlaylistIdError("playlist_id must not be blank")
        return playlist_id

    @staticmethod
    def _validate_resolved_playlists(
        game: GameSession,
        resolved_playlists: Iterable[ResolvedPlaylist],
    ) -> tuple[ResolvedPlaylist, ...]:
        try:
            playlists = tuple(resolved_playlists)
        except TypeError as error:
            raise InvalidResolvedPlaylistError(
                "resolved_playlists must be iterable"
            ) from error

        supplied_ids: list[str] = []
        for playlist in playlists:
            if not isinstance(playlist, ResolvedPlaylist):
                raise InvalidResolvedPlaylistError(
                    "each resolved playlist must be a ResolvedPlaylist"
                )
            snapshot = playlist.snapshot
            if not isinstance(snapshot, SelectedPlaylistSnapshot):
                raise InvalidResolvedPlaylistError(
                    "resolved playlist metadata must be a SelectedPlaylistSnapshot"
                )
            if (
                not isinstance(snapshot.playlist_id, str)
                or not snapshot.playlist_id.strip()
            ):
                raise InvalidResolvedPlaylistError(
                    "playlist snapshot IDs must be non-empty strings"
                )
            if (
                not isinstance(snapshot.playlist_name, str)
                or not snapshot.playlist_name
            ):
                raise InvalidResolvedPlaylistError(
                    "playlist snapshot names must be non-empty strings"
                )
            if not isinstance(snapshot.playlist_created_at, datetime):
                raise InvalidResolvedPlaylistError(
                    "playlist snapshot creation times must be datetimes"
                )
            if not isinstance(playlist.tracks, tuple):
                raise InvalidResolvedPlaylistError(
                    "resolved playlist tracks must be an immutable tuple"
                )
            if (
                isinstance(snapshot.playlist_song_count, bool)
                or not isinstance(snapshot.playlist_song_count, int)
                or snapshot.playlist_song_count < len(playlist.tracks)
            ):
                raise InvalidResolvedPlaylistError(
                    "playlist song count cannot be smaller than resolved track count"
                )
            supplied_ids.append(snapshot.playlist_id)
            for track in playlist.tracks:
                if not isinstance(track, TrackReference):
                    raise InvalidResolvedPlaylistError(
                        "resolved playlist tracks must be TrackReference values"
                    )
                if (
                    isinstance(track.track_id, bool)
                    or not isinstance(track.track_id, int)
                    or track.track_id <= 0
                ):
                    raise InvalidResolvedPlaylistError(
                        "resolved track IDs must be positive integers"
                    )
                try:
                    # This validates the duration without using eligibility as a
                    # Start Game filter; short tracks remain in the deck.
                    is_duration_eligible(
                        track, game.settings.playback_duration_seconds
                    )
                except (TypeError, ValueError) as error:
                    raise InvalidResolvedPlaylistError(
                        "resolved tracks must have valid positive durations"
                    ) from error

        if len(supplied_ids) != len(set(supplied_ids)):
            raise InvalidResolvedPlaylistError(
                "each selected playlist must be resolved exactly once"
            )
        if set(supplied_ids) != set(game.selected_playlist_ids):
            raise InvalidResolvedPlaylistError(
                "resolved playlist IDs must exactly match selected playlist IDs"
            )
        return playlists

    @staticmethod
    def _validate_prepared_start(
        locked_playlist_ids: frozenset[str],
        playlist_snapshots: tuple[SelectedPlaylistSnapshot, ...],
        deck: Deck,
        started_at: datetime,
    ) -> None:
        """Validate prepared Transition-B facts before authoritative mutation."""

        if not locked_playlist_ids:
            raise DomainInvariantError(
                "prepared Start Game requires selected playlist IDs"
            )
        snapshot_ids = tuple(snapshot.playlist_id for snapshot in playlist_snapshots)
        if len(snapshot_ids) != len(set(snapshot_ids)) or set(snapshot_ids) != set(
            locked_playlist_ids
        ):
            raise DomainInvariantError(
                "prepared playlist snapshots must match selected playlist IDs"
            )
        if not isinstance(deck, tuple) or not deck:
            raise DomainInvariantError(
                "prepared Start Game requires a non-empty immutable deck"
            )
        for expected_position, entry in enumerate(deck, start=1):
            if not isinstance(entry, DeckEntry) or (
                entry.original_position != expected_position
            ):
                raise DomainInvariantError(
                    "prepared deck entries must have permanent consecutive positions"
                )
            if not isinstance(entry.track, TrackReference):
                raise DomainInvariantError(
                    "prepared deck entries must contain TrackReference values"
                )
        if not isinstance(started_at, datetime):
            raise DomainInvariantError("the engine clock must return a datetime")

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

    def _lobby_game_for_playlist_selection(
        self, requesting_browser_token: str
    ) -> GameSession:
        if self._state is not GameState.LOBBY:
            raise PlaylistSelectionNotAllowedError(
                "playlist selection is only allowed in LOBBY"
            )
        game = self._active_game
        if game is None:
            raise DomainInvariantError("LOBBY requires an active game")
        self._validate_state_invariants(self._state)
        token = self._validate_browser_token(
            requesting_browser_token,
            error_type=UnauthorizedPlaylistSelectionError,
        )
        if token != game.songmaster_browser_token:
            raise UnauthorizedPlaylistSelectionError(
                "only the current Songmaster may change playlist selection"
            )
        return game

    def _transition_to(self, target: GameState) -> None:
        """Enter a target state and execute its entry actions and invariants."""

        self._state = target
        self._enter_state(target)
        self._validate_state_invariants(target)
        if target is GameState.ROUND_COMPLETE:
            game = self._active_game
            if game is not None and game.deck_exhausted:
                self._take_transition_k()

    def _enter_state(self, state: GameState) -> None:
        match state:
            case GameState.NO_GAME | GameState.LOBBY:
                # These states have invariants but no entry actions in this phase.
                return
            case GameState.ROUND_COMPLETE:
                self._enter_round_complete()
            case GameState.FINISHED:
                self._enter_finished()
            case _:
                raise DomainInvariantError(
                    f"entry behavior for {state.name} is outside the current phase"
                )

    def _enter_round_complete(self) -> None:
        game = self._active_game
        if game is None or game.deck is None:
            raise DomainInvariantError(
                "ROUND_COMPLETE entry requires an active game with a deck"
            )

        selection = select_deck_candidate(
            game.deck,
            game.deck_pointer,
            game.settings.playback_duration_seconds,
        )
        new_skips = tuple(
            SkippedDeckEntry(
                original_position=entry.original_position,
                track_id=entry.track.track_id,
                reason=DeckSkipReason.DURATION_INELIGIBLE,
            )
            for entry in selection.skipped_entries
        )
        game.skipped_deck_entries += new_skips
        game.deck_pointer = selection.deck_pointer
        game.deck_exhausted = selection.exhausted
        # Player-facing display fields are intentionally untouched here.

    def _take_transition_k(self) -> None:
        """Apply transition K after established deck exhaustion."""

        game = self._active_game
        if (
            self._state is not GameState.ROUND_COMPLETE
            or game is None
            or not game.deck_exhausted
        ):
            raise DomainInvariantError(
                "transition K requires established ROUND_COMPLETE deck exhaustion"
            )

        # Transition-K-specific action. General finalization belongs to the
        # FINISHED entry implementation below.
        game.end_reason = GameEndReason.DECK_EXHAUSTED
        self._transition_to(GameState.FINISHED)

    def _enter_finished(self) -> None:
        """Establish pure-domain finalization on every FINISHED entry."""

        game = self._active_game
        if game is None:
            raise DomainInvariantError("FINISHED entry requires an active game")
        ended_at = self._clock()
        if not isinstance(ended_at, datetime):
            raise DomainInvariantError("the engine clock must return a datetime")

        game.ended_at = ended_at
        game.final_standings = self._calculate_final_standings(game)
        game.active_round = None

    @staticmethod
    def _calculate_final_standings(game: GameSession) -> tuple[FinalStanding, ...]:
        """Return score-descending immutable standings with tied ranks."""

        members = sorted(
            game.members.values(),
            key=lambda member: member.committed_score,
            reverse=True,
        )
        standings: list[FinalStanding] = []
        prior_score: int | None = None
        prior_rank = 0
        for ordinal, member in enumerate(members, start=1):
            rank = prior_rank if member.committed_score == prior_score else ordinal
            standings.append(
                FinalStanding(
                    rank=rank,
                    browser_token=member.browser_token,
                    display_name=member.display_name,
                    score=member.committed_score,
                )
            )
            prior_score = member.committed_score
            prior_rank = rank
        return tuple(standings)

    def _validate_state_invariants(self, state: GameState) -> None:
        match state:
            case GameState.NO_GAME:
                self._validate_no_game_invariants()
            case GameState.LOBBY:
                self._validate_lobby_invariants()
            case GameState.ROUND_COMPLETE:
                self._validate_round_complete_invariants()
            case GameState.FINISHED:
                self._validate_finished_invariants()
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
        self._validate_active_game_members(game, state_name="LOBBY")
        if not isinstance(game.selected_playlist_ids, set) or any(
            not isinstance(playlist_id, str) or not playlist_id.strip()
            for playlist_id in game.selected_playlist_ids
        ):
            raise DomainInvariantError(
                "LOBBY selected playlist IDs must be a mutable set of non-empty strings"
            )
        if game.deck is not None:
            raise DomainInvariantError("LOBBY cannot have an immutable deck")
        if isinstance(game.deck_pointer, bool) or game.deck_pointer != 0:
            raise DomainInvariantError("LOBBY deck pointer must be zero")
        if isinstance(game.original_deck_size, bool) or game.original_deck_size != 0:
            raise DomainInvariantError("LOBBY original deck size must be zero")
        if game.deck_exhausted is not False:
            raise DomainInvariantError("LOBBY deck cannot be exhausted")
        if game.skipped_deck_entries != ():
            raise DomainInvariantError("LOBBY cannot have skipped deck entries")
        if game.started_at is not None:
            raise DomainInvariantError("LOBBY cannot have a game start timestamp")
        if game.ended_at is not None or game.end_reason is not None:
            raise DomainInvariantError("LOBBY cannot have game finalization facts")
        if game.final_standings != ():
            raise DomainInvariantError("LOBBY cannot have final standings")
        if game.selected_playlist_snapshots != ():
            raise DomainInvariantError("LOBBY cannot have selected-playlist snapshots")
        if game.active_round is not None:
            raise DomainInvariantError("LOBBY cannot have an active round")
        if game.playlist_selection_editable is not True:
            raise DomainInvariantError("playlist selection must be editable in LOBBY")
        if any(
            displayed_value is not None
            for displayed_value in (
                game.displayed_deck_position,
                game.displayed_artist,
                game.displayed_title,
            )
        ):
            raise DomainInvariantError("LOBBY player-facing song display must be blank")

    @staticmethod
    def _validate_active_game_members(
        game: GameSession, *, state_name: str
    ) -> None:
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
                f"{state_name} requires exactly one member as Songmaster"
            )
        if not game.songmaster.connected:
            raise DomainInvariantError(
                f"{state_name} requires a connected Songmaster until the "
                "disconnect overlay exists"
            )

    def _validate_started_game_facts(
        self, game: GameSession, *, state_name: str
    ) -> Deck:
        """Validate immutable game-start facts shared by started states."""

        self._validate_active_game_members(game, state_name=state_name)
        deck = game.deck
        if not isinstance(deck, tuple) or not deck:
            raise DomainInvariantError(
                f"{state_name} requires a non-empty immutable deck"
            )
        for expected_position, entry in enumerate(deck, start=1):
            if not isinstance(entry, DeckEntry):
                raise DomainInvariantError("deck values must be DeckEntry instances")
            if entry.original_position != expected_position:
                raise DomainInvariantError(
                    "deck entries must retain consecutive original positions"
                )
            if not isinstance(entry.track, TrackReference):
                raise DomainInvariantError(
                    "deck entries must retain TrackReference identities"
                )

        if (
            isinstance(game.original_deck_size, bool)
            or game.original_deck_size != len(deck)
        ):
            raise DomainInvariantError(
                "original deck size must equal the immutable deck size"
            )
        if (
            isinstance(game.deck_pointer, bool)
            or not isinstance(game.deck_pointer, int)
            or not 0 <= game.deck_pointer <= len(deck)
        ):
            raise DomainInvariantError(
                f"{state_name} deck pointer must be within the immutable deck"
            )
        if not isinstance(game.selected_playlist_ids, frozenset) or not (
            game.selected_playlist_ids
        ):
            raise DomainInvariantError(
                f"{state_name} requires a non-empty locked playlist selection"
            )
        if any(
            not isinstance(playlist_id, str) or not playlist_id.strip()
            for playlist_id in game.selected_playlist_ids
        ):
            raise DomainInvariantError("selected playlist IDs must be non-empty strings")
        if game.playlist_selection_editable is not False:
            raise DomainInvariantError(
                "playlist selection must be locked after Start Game"
            )
        if not isinstance(game.selected_playlist_snapshots, tuple):
            raise DomainInvariantError("playlist snapshots must be immutable")
        snapshot_ids: list[str] = []
        for snapshot in game.selected_playlist_snapshots:
            if not isinstance(snapshot, SelectedPlaylistSnapshot):
                raise DomainInvariantError(
                    "playlist snapshots must be SelectedPlaylistSnapshot values"
                )
            snapshot_ids.append(snapshot.playlist_id)
        if len(snapshot_ids) != len(set(snapshot_ids)) or set(snapshot_ids) != set(
            game.selected_playlist_ids
        ):
            raise DomainInvariantError(
                "playlist snapshots must exactly match the locked selection"
            )
        if not isinstance(game.started_at, datetime):
            raise DomainInvariantError(f"{state_name} requires a game start timestamp")

        if not isinstance(game.skipped_deck_entries, tuple):
            raise DomainInvariantError("skipped deck entry records must be immutable")
        skipped_positions: set[int] = set()
        for skipped in game.skipped_deck_entries:
            if not isinstance(skipped, SkippedDeckEntry):
                raise DomainInvariantError(
                    "skipped entries must be SkippedDeckEntry values"
                )
            if skipped.original_position in skipped_positions:
                raise DomainInvariantError("a deck entry cannot be skipped twice")
            if not 1 <= skipped.original_position <= game.deck_pointer:
                raise DomainInvariantError(
                    "skipped entries must precede the current deck pointer"
                )
            source_entry = deck[skipped.original_position - 1]
            if skipped.track_id != source_entry.track.track_id:
                raise DomainInvariantError(
                    "skipped entry identity must match its original deck position"
                )
            if skipped.reason is not DeckSkipReason.DURATION_INELIGIBLE:
                raise DomainInvariantError("skipped entry reason is not supported")
            skipped_positions.add(skipped.original_position)

        if game.displayed_deck_position is not None and (
            isinstance(game.displayed_deck_position, bool)
            or not isinstance(game.displayed_deck_position, int)
            or not 1 <= game.displayed_deck_position <= game.original_deck_size
        ):
            raise DomainInvariantError(
                "displayed deck position must refer to an original deck position"
            )
        if game.displayed_artist is not None and not isinstance(
            game.displayed_artist, str
        ):
            raise DomainInvariantError("displayed artist must be text or blank")
        if game.displayed_title is not None and not isinstance(
            game.displayed_title, str
        ):
            raise DomainInvariantError("displayed title must be text or blank")
        return deck

    def _validate_round_complete_invariants(self) -> None:
        game = self._active_game
        if game is None:
            raise DomainInvariantError("ROUND_COMPLETE requires an active game")
        deck = self._validate_started_game_facts(
            game, state_name="ROUND_COMPLETE"
        )
        if game.active_round is not None:
            raise DomainInvariantError("ROUND_COMPLETE cannot have an active round")
        if game.ended_at is not None or game.end_reason is not None:
            raise DomainInvariantError(
                "ROUND_COMPLETE cannot have game finalization facts"
            )
        if game.final_standings != ():
            raise DomainInvariantError("ROUND_COMPLETE cannot have final standings")
        if not isinstance(game.deck_exhausted, bool):
            raise DomainInvariantError("deck exhaustion must be boolean")
        selection = select_deck_candidate(
            deck,
            game.deck_pointer,
            game.settings.playback_duration_seconds,
        )
        if selection.skipped_entries:
            raise DomainInvariantError(
                "ROUND_COMPLETE pointer must already identify an eligible candidate "
                "or exhaustion"
            )
        if game.deck_exhausted is not selection.exhausted:
            raise DomainInvariantError(
                "deck exhaustion must agree with the established candidate selection"
            )

    def _validate_finished_invariants(self) -> None:
        game = self._active_game
        if game is None:
            raise DomainInvariantError("FINISHED requires an active game")
        deck = self._validate_started_game_facts(game, state_name="FINISHED")
        if game.active_round is not None:
            raise DomainInvariantError("FINISHED cannot have an active round")
        if game.deck_pointer != len(deck) or game.deck_exhausted is not True:
            raise DomainInvariantError(
                "deck-exhausted FINISHED requires the pointer at deck end"
            )
        if game.end_reason is not GameEndReason.DECK_EXHAUSTED:
            raise DomainInvariantError(
                "automatic transition K requires deck_exhausted as end reason"
            )
        if not isinstance(game.ended_at, datetime):
            raise DomainInvariantError("FINISHED requires a game end timestamp")
        if not isinstance(game.final_standings, tuple):
            raise DomainInvariantError("final standings must be immutable")
        expected_standings = self._calculate_final_standings(game)
        if game.final_standings != expected_standings:
            raise DomainInvariantError(
                "final standings must match finalized member scores"
            )
