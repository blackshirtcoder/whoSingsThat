"""Authoritative in-memory state transitions for the game domain."""

from collections.abc import Callable, Iterable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from math import isfinite
from numbers import Real
from random import shuffle as random_shuffle
from time import monotonic
from uuid import UUID, uuid4

from .deck import Deck, DeckEntry, Shuffle, TrackReference, build_deck
from .models import (
    ActiveRound,
    AudioPreparationResponseState,
    AudioPreparationStatus,
    CompletedRound,
    DeckSkipReason,
    FinalStanding,
    GameEndReason,
    GameFinalizationResult,
    GameMember,
    GameSession,
    GameSettings,
    GameState,
    InvalidPlayerNameError,
    PendingAudioPreparation,
    ResolvedPlaylist,
    RoundTermination,
    RoundTerminationReason,
    RoundVoidReason,
    SelectedPlaylistSnapshot,
    SkippedDeckEntry,
    VoidedRound,
    normalize_player_name,
    player_name_uniqueness_key,
)
from .playback import (
    calculate_effective_playback_offset,
    is_duration_eligible,
    select_deck_candidate,
)
from .scoring import ScoringChoice, calculate_points


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


class AudioPreferenceNotAllowedError(GameEngineError):
    """Raised when audio preference changes are not legal in the current state."""


class InvalidAudioPreferenceRequestError(GameEngineError, ValueError):
    """Raised when a member supplies an invalid audio preference."""


class UnauthorizedAudioPreferenceError(GameEngineError):
    """Raised when a browser tries to change a preference it does not own."""


class AudioCapabilityNotAllowedError(GameEngineError):
    """Raised when browser audio capability cannot be updated in this phase."""


class InvalidAudioCapabilityRequestError(GameEngineError, ValueError):
    """Raised when a trusted browser-audio capability update is invalid."""


class PlaySongNotAllowedError(GameEngineError):
    """Raised when Play Song is requested outside ``ROUND_COMPLETE``."""


class UnauthorizedPlaySongError(GameEngineError):
    """Raised when Play Song is not requested by the connected Songmaster."""


class NoValidAudioTargetError(GameEngineError):
    """Expected rejection when no connected, enabled, usable target exists."""


class PreparationEventInapplicableError(GameEngineError):
    """Raised for stale, duplicate, late, or non-target preparation events."""


class PreparationFailureAcknowledgementNotAllowedError(GameEngineError):
    """Raised when transition D acknowledgement preconditions are absent."""


class UnauthorizedPreparationFailureAcknowledgementError(GameEngineError):
    """Raised when someone other than the connected Songmaster acknowledges."""


class BuzzNotAllowedError(GameEngineError):
    """Expected rejection of an invalid, stale, ineligible, or late buzz."""


class PlaybackTimeoutInapplicableError(GameEngineError):
    """Expected discard of an early or stale playback timeout event."""


class RevealNotAllowedError(GameEngineError):
    """Raised when Reveal does not target the current revealable round."""


class UnauthorizedRevealError(GameEngineError):
    """Raised when Reveal is not requested by the connected Songmaster."""


class ScoringNotAllowedError(GameEngineError):
    """Raised when an initial score does not target the current scoring round."""


class UnauthorizedScoringError(GameEngineError):
    """Raised when scoring is not requested by the connected Songmaster."""


class InvalidScoringDecisionError(GameEngineError, ValueError):
    """Raised when a scoring request has no supported adjudication."""


class ScoreCorrectionNotAllowedError(GameEngineError):
    """Raised when a completed round is outside its correction window."""


class UnauthorizedScoreCorrectionError(GameEngineError):
    """Raised when correction is not requested by the connected Songmaster."""


class CancelGameNotAllowedError(GameEngineError):
    """Raised when Cancel Game does not target the current lobby."""


class UnauthorizedCancelGameError(GameEngineError):
    """Raised when Cancel Game is not requested by the connected Songmaster."""


class EndGameNotAllowedError(GameEngineError):
    """Raised when End Game is requested outside a started active state."""


class UnauthorizedEndGameError(GameEngineError):
    """Raised when End Game is not requested by the connected Songmaster."""


class CloseGameNotAllowedError(GameEngineError):
    """Raised when Close Game does not target the current finished game."""


class UnauthorizedCloseGameError(GameEngineError):
    """Raised when Close Game is not requested by the connected Songmaster."""


class PreparationOutcome(Enum):
    """Outcome exposed internally after applying one preparation event."""

    COLLECTING = "collecting"
    FAILURE_PENDING_ACKNOWLEDGEMENT = "failure_pending_acknowledgement"
    PLAYING = "playing"


@dataclass(frozen=True, slots=True)
class PreparationEventResult:
    """Internal result for future preparation/playback notifications."""

    preparation_id: UUID
    outcome: PreparationOutcome
    ready_browser_tokens: frozenset[str]
    failed_browser_tokens: frozenset[str]
    pending_browser_tokens: frozenset[str]
    active_round: ActiveRound | None = None


class GameEngine:
    """Own the single authoritative live game and its normal-flow state."""

    def __init__(
        self,
        installation_defaults: GameSettings | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
        game_id_factory: Callable[[], UUID] | None = None,
        preparation_id_factory: Callable[[], UUID] | None = None,
        round_id_factory: Callable[[], UUID] | None = None,
        shuffle: Shuffle | None = None,
    ) -> None:
        self._installation_defaults = deepcopy(
            installation_defaults
            if installation_defaults is not None
            else GameSettings()
        )
        self._clock = clock if clock is not None else lambda: datetime.now(UTC)
        self._monotonic_clock = (
            monotonic_clock if monotonic_clock is not None else monotonic
        )
        self._game_id_factory = (
            game_id_factory if game_id_factory is not None else uuid4
        )
        self._preparation_id_factory = (
            preparation_id_factory
            if preparation_id_factory is not None
            else uuid4
        )
        self._round_id_factory = (
            round_id_factory if round_id_factory is not None else uuid4
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

    def monotonic_time(self) -> float:
        """Return a validated timestamp from the authoritative timing clock.

        The application controller uses this same clock domain at trusted buzz
        ingress, when scheduling the playback timer, and when dispatching its
        expiry. Wall-clock time is deliberately absent from those decisions.
        """

        return self._validated_monotonic_timestamp(
            self._monotonic_clock(),
            error_type=DomainInvariantError,
            name="the engine monotonic clock value",
        )

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
            audio_enabled=True,
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

    def cancel_game(
        self,
        game_session_id: UUID,
        requesting_browser_token: str,
    ) -> None:
        """Cancel an unstarted lobby directly into ``NO_GAME``."""

        if self._state is not GameState.LOBBY:
            raise CancelGameNotAllowedError("Cancel Game is only allowed in LOBBY")
        game = self._active_game
        if game is None or game.game_session_id != game_session_id:
            raise CancelGameNotAllowedError("Cancel Game targets a stale game")
        token = self._validate_browser_token(
            requesting_browser_token,
            error_type=UnauthorizedCancelGameError,
        )
        if token != game.songmaster_browser_token or not game.songmaster.connected:
            raise UnauthorizedCancelGameError(
                "only the current connected Songmaster may cancel the game"
            )
        self._validate_state_invariants(GameState.LOBBY)

        self._active_game = None
        self._transition_to(GameState.NO_GAME)

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

        self.validate_start_game_request(requesting_browser_token)
        game = self._active_game
        if game is None:  # pragma: no cover - established by the validation above
            raise DomainInvariantError("LOBBY requires an active game")

        # Validate and prepare every expected failure point before committing any
        # Start Game fact to the authoritative session.
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

    def validate_start_game_request(
        self,
        requesting_browser_token: str,
    ) -> tuple[str, ...]:
        """Validate Start Game preconditions without mutating live game state.

        The application layer uses this narrow preflight before filesystem and
        media inspection. ``start_game`` repeats it when committing transition B
        so the domain engine remains authoritative if established facts changed.
        Selected IDs are returned in deterministic order for resolution only.
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

        self._validate_state_invariants(GameState.LOBBY)
        return tuple(sorted(game.selected_playlist_ids))

    def set_audio_preference(
        self, requesting_browser_token: str, audio_enabled: bool
    ) -> GameMember:
        """Set the requesting member's own per-device audio preference.

        This is an internal action in ``LOBBY`` or ``ROUND_COMPLETE``. Browser
        audio usability is an independent connection fact and is deliberately
        not inferred from this preference.
        """

        if self._state not in {GameState.LOBBY, GameState.ROUND_COMPLETE}:
            raise AudioPreferenceNotAllowedError(
                "audio preference may only change in LOBBY or ROUND_COMPLETE"
            )
        game = self._active_game
        if game is None:
            raise DomainInvariantError(
                f"{self._state.name} requires an active game"
            )
        token = self._validate_browser_token(
            requesting_browser_token,
            error_type=InvalidAudioPreferenceRequestError,
        )
        member = game.members.get(token)
        if member is None:
            raise UnauthorizedAudioPreferenceError(
                "a member may change only their own audio preference"
            )
        if not isinstance(audio_enabled, bool):
            raise InvalidAudioPreferenceRequestError(
                "audio_enabled must be a boolean"
            )

        previous_value = member.audio_enabled
        member.audio_enabled = audio_enabled
        try:
            self._validate_state_invariants(self._state)
        except Exception:
            member.audio_enabled = previous_value
            raise
        return member

    def set_browser_audio_capability(
        self, browser_token: str, browser_audio_usable: bool
    ) -> GameMember:
        """Record a trusted connection's initialized/unlocked audio condition.

        Capability is a connection fact, so current connections may update it
        throughout the joinable game lifecycle. Frozen preparation and round
        snapshots are not rewritten by this operation.
        """

        if self._state not in {
            GameState.LOBBY,
            GameState.ROUND_COMPLETE,
            GameState.AUDIO_PREPARING,
            GameState.PLAYING,
            GameState.AWAITING_REVEAL,
            GameState.SCORING,
        }:
            raise AudioCapabilityNotAllowedError(
                "browser audio capability updates are not allowed in the "
                "current state"
            )
        game = self._active_game
        if game is None:
            raise DomainInvariantError(
                f"{self._state.name} requires an active game"
            )
        token = self._validate_browser_token(
            browser_token,
            error_type=InvalidAudioCapabilityRequestError,
        )
        member = game.members.get(token)
        if member is None:
            raise UnknownGameMemberError(
                "browser_token does not belong to the active game"
            )
        if not isinstance(browser_audio_usable, bool):
            raise InvalidAudioCapabilityRequestError(
                "browser_audio_usable must be a boolean"
            )
        if browser_audio_usable and not member.connected:
            raise InvalidAudioCapabilityRequestError(
                "a disconnected browser cannot have usable audio capability"
            )

        previous_value = member.browser_audio_usable
        member.browser_audio_usable = browser_audio_usable
        try:
            self._validate_state_invariants(self._state)
        except Exception:
            member.browser_audio_usable = previous_value
            raise
        return member

    def play_song(
        self, requesting_browser_token: str
    ) -> PendingAudioPreparation | GameFinalizationResult:
        """Apply transition C or K from ``ROUND_COMPLETE``.

        Candidate availability is established by ``ROUND_COMPLETE`` entry.
        An available candidate takes C and returns its immutable preparation;
        an exhausted deck takes K and returns frozen finalization facts.
        """

        if self._state is not GameState.ROUND_COMPLETE:
            raise PlaySongNotAllowedError(
                "Play Song is only allowed in ROUND_COMPLETE"
            )
        game = self._active_game
        if game is None:
            raise DomainInvariantError("ROUND_COMPLETE requires an active game")

        token = self._validate_browser_token(
            requesting_browser_token,
            error_type=UnauthorizedPlaySongError,
        )
        if (
            token != game.songmaster_browser_token
            or not game.songmaster.connected
        ):
            raise UnauthorizedPlaySongError(
                "only the current connected Songmaster may request Play Song"
            )

        # ROUND_COMPLETE entry already performed candidate scanning. Inspect
        # only the established outcome; neither transition C nor K searches
        # ahead. Exhaustion dispatches K before candidate/audio validation.
        self._validate_state_invariants(GameState.ROUND_COMPLETE)
        deck = game.deck
        if deck is None:
            raise DomainInvariantError(
                "Play Song requires an established deck"
            )
        if game.deck_exhausted:
            self._take_transition_k()
            return GameFinalizationResult(
                end_reason=GameEndReason.DECK_EXHAUSTED,
                final_standings=game.final_standings,
            )
        if game.deck_pointer >= len(deck):
            raise DomainInvariantError(
                "a non-exhausted deck requires a candidate under the pointer"
            )
        deck_entry = deck[game.deck_pointer]
        if not is_duration_eligible(
            deck_entry.track, game.settings.playback_duration_seconds
        ):
            raise DomainInvariantError(
                "ROUND_COMPLETE candidate must be duration eligible"
            )

        audio_targets = frozenset(
            member.browser_token
            for member in game.members.values()
            if member.is_valid_audio_target
        )
        if not audio_targets:
            raise NoValidAudioTargetError(
                "Play Song requires at least one connected, enabled browser "
                "with usable audio"
            )

        try:
            effective_offset = calculate_effective_playback_offset(
                deck_entry.track,
                game.settings.playback_offset_seconds,
                game.settings.playback_duration_seconds,
            )
        except (TypeError, ValueError) as error:
            raise DomainInvariantError(
                "established candidate and game settings must produce a valid offset"
            ) from error
        preparation_id = self._preparation_id_factory()
        if not isinstance(preparation_id, UUID):
            raise DomainInvariantError(
                "the preparation ID factory must return a UUID"
            )

        pending = PendingAudioPreparation(
            preparation_id=preparation_id,
            deck_entry=deck_entry,
            configured_playback_offset_seconds=(
                game.settings.playback_offset_seconds
            ),
            effective_playback_offset_seconds=effective_offset,
            playback_duration_seconds=game.settings.playback_duration_seconds,
            speed_bonus_plateau_percent=(
                game.settings.speed_bonus_plateau_percent
            ),
            buzz_eligible_browser_tokens=frozenset(
                member.browser_token
                for member in game.members.values()
                if member.connected
            ),
            audio_target_browser_tokens=audio_targets,
        )

        game.pending_audio_preparation = pending
        game.audio_preparation_responses = AudioPreparationResponseState(
            preparation_id=preparation_id,
            target_statuses={
                token: AudioPreparationStatus.PENDING for token in audio_targets
            },
        )
        # The prior round remains correctable until transition C successfully
        # commits a preparation. All request validation above is non-mutating.
        game.scoring_correction_round_id = None
        self._transition_to(GameState.AUDIO_PREPARING)
        return pending

    def end_game(
        self,
        game_session_id: UUID,
        requesting_browser_token: str,
    ) -> GameFinalizationResult:
        """Voluntarily finalize a started game from any active gameplay state."""

        legal_states = {
            GameState.ROUND_COMPLETE,
            GameState.AUDIO_PREPARING,
            GameState.PLAYING,
            GameState.AWAITING_REVEAL,
            GameState.SCORING,
        }
        source_state = self._state
        if source_state not in legal_states:
            raise EndGameNotAllowedError(
                "End Game is only allowed after Start Game and before FINISHED"
            )
        game = self._active_game
        if game is None or game.game_session_id != game_session_id:
            raise EndGameNotAllowedError("End Game targets a stale game")
        token = self._validate_browser_token(
            requesting_browser_token,
            error_type=UnauthorizedEndGameError,
        )
        if token != game.songmaster_browser_token or not game.songmaster.connected:
            raise UnauthorizedEndGameError(
                "only the current connected Songmaster may end the game"
            )
        self._validate_state_invariants(source_state)

        if source_state in {
            GameState.PLAYING,
            GameState.AWAITING_REVEAL,
            GameState.SCORING,
        }:
            active_round = game.active_round
            if not isinstance(active_round, ActiveRound):  # pragma: no cover
                raise DomainInvariantError(
                    f"{source_state.name} requires an actual round"
                )
            game.last_voided_round = VoidedRound(
                active_round=active_round,
                reason=RoundVoidReason.GAME_ENDED,
                termination=game.round_termination,
                answer_revealed=source_state is GameState.SCORING,
            )

        game.end_reason = GameEndReason.SONGMASTER_ENDED_GAME
        self._transition_to(GameState.FINISHED)
        return GameFinalizationResult(
            end_reason=GameEndReason.SONGMASTER_ENDED_GAME,
            final_standings=game.final_standings,
        )

    def close_game(
        self,
        game_session_id: UUID,
        requesting_browser_token: str,
    ) -> None:
        """Apply transition L and discard the retained finished session."""

        if self._state is not GameState.FINISHED:
            raise CloseGameNotAllowedError(
                "Close Game is only allowed in FINISHED"
            )
        game = self._active_game
        if game is None or game.game_session_id != game_session_id:
            raise CloseGameNotAllowedError("Close Game targets a stale game")
        token = self._validate_browser_token(
            requesting_browser_token,
            error_type=UnauthorizedCloseGameError,
        )
        if token != game.songmaster_browser_token or not game.songmaster.connected:
            raise UnauthorizedCloseGameError(
                "only the current connected Songmaster may close the game"
            )
        self._validate_state_invariants(GameState.FINISHED)

        self._active_game = None
        self._transition_to(GameState.NO_GAME)

    def record_audio_preparation_ready(
        self,
        game_session_id: UUID,
        preparation_id: UUID,
        browser_token: str,
    ) -> PreparationEventResult:
        """Accept a target's first READY response and evaluate when complete."""

        return self._record_audio_preparation_response(
            game_session_id,
            preparation_id,
            browser_token,
            AudioPreparationStatus.READY,
        )

    def record_audio_preparation_failed(
        self,
        game_session_id: UUID,
        preparation_id: UUID,
        browser_token: str,
    ) -> PreparationEventResult:
        """Accept a target's first FAILED response and evaluate when complete."""

        return self._record_audio_preparation_response(
            game_session_id,
            preparation_id,
            browser_token,
            AudioPreparationStatus.FAILED,
        )

    def audio_preparation_timed_out(
        self,
        game_session_id: UUID,
        preparation_id: UUID,
    ) -> PreparationEventResult:
        """Finalize response collection when its application timer expires."""

        _, responses = self._current_preparation_for_event(
            game_session_id,
            preparation_id,
        )
        return self._evaluate_preparation_outcome(responses)

    def acknowledge_audio_preparation_failure(
        self,
        game_session_id: UUID,
        preparation_id: UUID,
        requesting_browser_token: str,
    ) -> GameSession:
        """Apply transition D after the connected Songmaster acknowledges failure."""

        if self._state is not GameState.AUDIO_PREPARING:
            raise PreparationFailureAcknowledgementNotAllowedError(
                "preparation failure acknowledgement requires AUDIO_PREPARING"
            )
        game = self._active_game
        if game is None or game.game_session_id != game_session_id:
            raise PreparationFailureAcknowledgementNotAllowedError(
                "preparation failure acknowledgement targets a stale game"
            )
        pending = game.pending_audio_preparation
        responses = game.audio_preparation_responses
        if not isinstance(pending, PendingAudioPreparation) or not isinstance(
            responses, AudioPreparationResponseState
        ):
            raise DomainInvariantError(
                "AUDIO_PREPARING requires pending preparation response state"
            )
        if pending.preparation_id != preparation_id:
            raise PreparationFailureAcknowledgementNotAllowedError(
                "preparation failure acknowledgement targets a stale preparation"
            )
        if responses.preparation_id != pending.preparation_id:
            raise DomainInvariantError(
                "preparation responses must bind the pending preparation"
            )
        if not isinstance(responses.target_statuses, dict):
            raise DomainInvariantError(
                "preparation target statuses must be a mutable mapping"
            )
        self._validate_state_invariants(GameState.AUDIO_PREPARING)

        token = self._validate_browser_token(
            requesting_browser_token,
            error_type=UnauthorizedPreparationFailureAcknowledgementError,
        )
        if token != game.songmaster_browser_token or not game.songmaster.connected:
            raise UnauthorizedPreparationFailureAcknowledgementError(
                "only the current connected Songmaster may acknowledge failure"
            )
        if (
            not responses.outcome_determined
            or not responses.failure_acknowledgement_pending
            or responses.ready_browser_tokens
        ):
            raise PreparationFailureAcknowledgementNotAllowedError(
                "preparation failure is not pending acknowledgement"
            )

        entry = pending.deck_entry
        game.skipped_deck_entries += (
            SkippedDeckEntry(
                original_position=entry.original_position,
                track_id=entry.track.track_id,
                reason=DeckSkipReason.AUDIO_PREPARATION_FAILED,
            ),
        )
        game.deck_pointer += 1
        game.pending_audio_preparation = None
        game.audio_preparation_responses = None
        self._transition_to(GameState.ROUND_COMPLETE)
        return game

    def accept_buzz(
        self,
        game_session_id: UUID,
        round_id: UUID,
        requesting_browser_token: str,
        received_at_monotonic: float,
    ) -> RoundTermination:
        """Apply transition F using an application-trusted receipt timestamp."""

        game, active_round = self._current_playing_round_for_event(
            game_session_id,
            round_id,
            error_type=BuzzNotAllowedError,
            event_name="buzz",
        )
        if not game.buzzing_enabled:
            raise BuzzNotAllowedError("buzzing is not enabled for this round")

        token = self._validate_browser_token(
            requesting_browser_token,
            error_type=BuzzNotAllowedError,
        )
        member = game.members.get(token)
        if member is None:
            raise BuzzNotAllowedError("the requesting browser is not a game member")
        if not member.connected:
            raise BuzzNotAllowedError("a disconnected member cannot buzz")
        if token not in active_round.buzz_eligible_browser_tokens:
            raise BuzzNotAllowedError(
                "the requesting member is not eligible for this round"
            )

        receipt = self._validated_monotonic_timestamp(
            received_at_monotonic,
            error_type=BuzzNotAllowedError,
            name="trusted buzz receipt timestamp",
        )
        if receipt < active_round.playback_started_monotonic:
            raise BuzzNotAllowedError("buzz was received before playback started")
        if receipt >= active_round.playback_deadline_monotonic:
            raise BuzzNotAllowedError("buzz was received at or after the deadline")

        termination = RoundTermination(
            reason=RoundTerminationReason.BUZZ,
            winning_browser_token=token,
            buzz_elapsed_seconds=(
                receipt - active_round.playback_started_monotonic
            ),
        )
        game.round_termination = termination
        self._transition_to(GameState.AWAITING_REVEAL)
        return termination

    def playback_timed_out(
        self,
        game_session_id: UUID,
        round_id: UUID,
        observed_at_monotonic: float,
    ) -> RoundTermination:
        """Apply transition G after the authoritative playback deadline."""

        game, active_round = self._current_playing_round_for_event(
            game_session_id,
            round_id,
            error_type=PlaybackTimeoutInapplicableError,
            event_name="playback timeout",
        )
        observed_at = self._validated_monotonic_timestamp(
            observed_at_monotonic,
            error_type=PlaybackTimeoutInapplicableError,
            name="trusted playback timeout timestamp",
        )
        if observed_at < active_round.playback_deadline_monotonic:
            raise PlaybackTimeoutInapplicableError(
                "playback timeout arrived before the authoritative deadline"
            )

        termination = RoundTermination(
            reason=RoundTerminationReason.PLAYBACK_TIMEOUT,
        )
        game.round_termination = termination
        self._transition_to(GameState.AWAITING_REVEAL)
        return termination

    def reveal_answer(
        self,
        game_session_id: UUID,
        round_id: UUID,
        requesting_browser_token: str,
    ) -> GameSession:
        """Apply transition H or I according to the recorded termination."""

        if self._state is not GameState.AWAITING_REVEAL:
            raise RevealNotAllowedError(
                "Reveal is only allowed in AWAITING_REVEAL"
            )
        game = self._active_game
        if game is None or game.game_session_id != game_session_id:
            raise RevealNotAllowedError("Reveal targets a stale game")

        token = self._validate_browser_token(
            requesting_browser_token,
            error_type=UnauthorizedRevealError,
        )
        if token != game.songmaster_browser_token or not game.songmaster.connected:
            raise UnauthorizedRevealError(
                "only the current connected Songmaster may reveal the answer"
            )

        active_round = game.active_round
        if not isinstance(active_round, ActiveRound):
            raise DomainInvariantError(
                "AWAITING_REVEAL requires an actual played round"
            )
        if active_round.round_id != round_id:
            raise RevealNotAllowedError("Reveal targets a stale round")
        self._validate_state_invariants(GameState.AWAITING_REVEAL)

        artist, title = self._answer_for_reveal(active_round)
        termination = game.round_termination
        if not isinstance(termination, RoundTermination):
            raise DomainInvariantError(
                "AWAITING_REVEAL requires a termination outcome"
            )

        # Revealing the answer is transition-specific work shared by H and I.
        # The established display position already identifies this actual round.
        game.displayed_artist = artist
        game.displayed_title = title

        if termination.reason is RoundTerminationReason.BUZZ:
            self._transition_to(GameState.SCORING)
            return game
        if termination.reason is RoundTerminationReason.PLAYBACK_TIMEOUT:
            # Transition I finalizes the zero-point round and consumes its bound
            # entry before normal ROUND_COMPLETE candidate scanning runs.
            game.last_completed_round = CompletedRound(
                active_round=active_round,
                termination=termination,
                scoring_choice=None,
                awarded_points=0,
            )
            game.scoring_correction_round_id = None
            game.deck_pointer += 1
            game.active_round = None
            game.round_termination = None
            self._transition_to(GameState.ROUND_COMPLETE)
            return game
        raise DomainInvariantError("round termination reason is not supported")

    def accept_score(
        self,
        game_session_id: UUID,
        round_id: UUID,
        requesting_browser_token: str,
        scoring_choice: ScoringChoice,
    ) -> CompletedRound:
        """Apply transition J and commit one buzz round's initial award."""

        if self._state is not GameState.SCORING:
            raise ScoringNotAllowedError("initial scoring is only allowed in SCORING")
        game = self._active_game
        if game is None or game.game_session_id != game_session_id:
            raise ScoringNotAllowedError("initial scoring targets a stale game")

        token = self._validate_browser_token(
            requesting_browser_token,
            error_type=UnauthorizedScoringError,
        )
        if token != game.songmaster_browser_token or not game.songmaster.connected:
            raise UnauthorizedScoringError(
                "only the current connected Songmaster may accept a score"
            )
        if not isinstance(scoring_choice, ScoringChoice):
            raise InvalidScoringDecisionError(
                "scoring_choice must be a ScoringChoice"
            )

        active_round = game.active_round
        if not isinstance(active_round, ActiveRound):
            raise DomainInvariantError("SCORING requires an actual played round")
        if active_round.round_id != round_id:
            raise ScoringNotAllowedError("initial scoring targets a stale round")
        self._validate_state_invariants(GameState.SCORING)
        termination = game.round_termination
        if not isinstance(termination, RoundTermination):  # pragma: no cover
            raise DomainInvariantError("SCORING requires a termination outcome")
        winner_token = termination.winning_browser_token
        elapsed = termination.buzz_elapsed_seconds
        if winner_token is None or elapsed is None:  # pragma: no cover
            raise DomainInvariantError("SCORING requires winner and elapsed facts")

        awarded_points = self._calculate_round_points(
            active_round,
            elapsed,
            scoring_choice,
        )
        completed = CompletedRound(
            active_round=active_round,
            termination=termination,
            scoring_choice=scoring_choice,
            awarded_points=awarded_points,
        )

        # Transition-J-specific work is committed exactly once before normal
        # ROUND_COMPLETE entry scanning executes without finalizing the game.
        game.members[winner_token].committed_score += awarded_points
        game.last_completed_round = completed
        game.scoring_correction_round_id = round_id
        game.deck_pointer += 1
        game.active_round = None
        game.round_termination = None
        self._transition_to(GameState.ROUND_COMPLETE)
        return completed

    def correct_score(
        self,
        game_session_id: UUID,
        round_id: UUID,
        requesting_browser_token: str,
        scoring_choice: ScoringChoice,
    ) -> CompletedRound:
        """Replace the most recent buzz round's award within its open window."""

        if self._state is not GameState.ROUND_COMPLETE:
            raise ScoreCorrectionNotAllowedError(
                "score correction is only allowed in ROUND_COMPLETE"
            )
        game = self._active_game
        if game is None or game.game_session_id != game_session_id:
            raise ScoreCorrectionNotAllowedError("score correction targets a stale game")

        token = self._validate_browser_token(
            requesting_browser_token,
            error_type=UnauthorizedScoreCorrectionError,
        )
        if token != game.songmaster_browser_token or not game.songmaster.connected:
            raise UnauthorizedScoreCorrectionError(
                "only the current connected Songmaster may correct a score"
            )
        if not isinstance(scoring_choice, ScoringChoice):
            raise InvalidScoringDecisionError(
                "scoring_choice must be a ScoringChoice"
            )

        completed = game.last_completed_round
        if (
            not isinstance(completed, CompletedRound)
            or completed.active_round.round_id != round_id
            or game.scoring_correction_round_id != round_id
            or completed.termination.reason is not RoundTerminationReason.BUZZ
            or completed.scoring_choice is None
        ):
            raise ScoreCorrectionNotAllowedError(
                "score correction does not target the open completed buzz round"
            )
        self._validate_state_invariants(GameState.ROUND_COMPLETE)

        winner_token = completed.termination.winning_browser_token
        elapsed = completed.termination.buzz_elapsed_seconds
        if winner_token is None or elapsed is None:  # pragma: no cover
            raise DomainInvariantError(
                "a correctable completed round requires winner and elapsed facts"
            )
        new_points = self._calculate_round_points(
            completed.active_round,
            elapsed,
            scoring_choice,
        )
        if (
            scoring_choice is completed.scoring_choice
            and new_points == completed.awarded_points
        ):
            return completed

        adjusted_score = (
            game.members[winner_token].committed_score
            + new_points
            - completed.awarded_points
        )
        if adjusted_score < 0:
            raise DomainInvariantError(
                "replacement scoring cannot make a cumulative score negative"
            )
        replacement = CompletedRound(
            active_round=completed.active_round,
            termination=completed.termination,
            scoring_choice=scoring_choice,
            awarded_points=new_points,
        )
        game.members[winner_token].committed_score = adjusted_score
        game.last_completed_round = replacement
        self._validate_state_invariants(GameState.ROUND_COMPLETE)
        return replacement

    @staticmethod
    def _calculate_round_points(
        active_round: ActiveRound,
        elapsed_seconds: float,
        scoring_choice: ScoringChoice,
    ) -> int:
        """Delegate award arithmetic to the pure scoring module."""

        try:
            return calculate_points(
                scoring_choice,
                active_round.playback_duration_seconds,
                active_round.speed_bonus_plateau_percent / 100,
                elapsed_seconds,
            )
        except (TypeError, ValueError) as error:  # immutable facts are invalid
            raise DomainInvariantError(
                "actual-round scoring inputs must satisfy the scoring rules"
            ) from error

    @staticmethod
    def _answer_for_reveal(active_round: ActiveRound) -> tuple[str, str]:
        track = active_round.deck_entry.track
        if (
            not isinstance(track.artist, str)
            or not track.artist
            or not isinstance(track.title, str)
            or not track.title
        ):
            raise DomainInvariantError(
                "an actual round must retain artist and title for reveal"
            )
        return track.artist, track.title

    def _current_playing_round_for_event(
        self,
        game_session_id: UUID,
        round_id: UUID,
        *,
        error_type: type[GameEngineError],
        event_name: str,
    ) -> tuple[GameSession, ActiveRound]:
        game = self._active_game
        if (
            self._state is not GameState.PLAYING
            or game is None
            or game.game_session_id != game_session_id
        ):
            raise error_type(f"{event_name} does not target the playing game")
        active_round = game.active_round
        if not isinstance(active_round, ActiveRound):
            raise DomainInvariantError("PLAYING requires an actual round")
        if active_round.round_id != round_id:
            raise error_type(f"{event_name} targets a stale round")
        self._validate_state_invariants(GameState.PLAYING)
        return game, active_round

    def _record_audio_preparation_response(
        self,
        game_session_id: UUID,
        preparation_id: UUID,
        browser_token: str,
        status: AudioPreparationStatus,
    ) -> PreparationEventResult:
        _, responses = self._current_preparation_for_event(
            game_session_id,
            preparation_id,
        )
        if browser_token not in responses.target_statuses:
            raise PreparationEventInapplicableError(
                "preparation response did not come from a targeted member"
            )
        current_status = responses.target_statuses[browser_token]
        if current_status is not AudioPreparationStatus.PENDING:
            raise PreparationEventInapplicableError(
                "a target's first accepted terminal response is final"
            )

        responses.target_statuses[browser_token] = status
        if responses.pending_browser_tokens:
            self._validate_state_invariants(GameState.AUDIO_PREPARING)
            return self._preparation_event_result(
                responses,
                PreparationOutcome.COLLECTING,
            )
        return self._evaluate_preparation_outcome(responses)

    def _current_preparation_for_event(
        self,
        game_session_id: UUID,
        preparation_id: UUID,
    ) -> tuple[PendingAudioPreparation, AudioPreparationResponseState]:
        game = self._active_game
        if (
            self._state is not GameState.AUDIO_PREPARING
            or game is None
            or game.game_session_id != game_session_id
        ):
            raise PreparationEventInapplicableError(
                "preparation event does not target the active preparation"
            )
        pending = game.pending_audio_preparation
        responses = game.audio_preparation_responses
        if not isinstance(pending, PendingAudioPreparation) or not isinstance(
            responses, AudioPreparationResponseState
        ):
            raise DomainInvariantError(
                "AUDIO_PREPARING requires pending preparation response state"
            )
        if pending.preparation_id != preparation_id:
            raise PreparationEventInapplicableError(
                "preparation event targets a stale preparation"
            )
        if responses.preparation_id != pending.preparation_id:
            raise DomainInvariantError(
                "preparation responses must bind the pending preparation"
            )
        self._validate_state_invariants(GameState.AUDIO_PREPARING)
        if responses.outcome_determined:
            raise PreparationEventInapplicableError(
                "preparation response collection has already completed"
            )
        return pending, responses

    def _evaluate_preparation_outcome(
        self,
        responses: AudioPreparationResponseState,
    ) -> PreparationEventResult:
        responses.outcome_determined = True
        ready_tokens = responses.ready_browser_tokens
        if ready_tokens:
            return self._take_transition_e(responses, ready_tokens)

        responses.failure_acknowledgement_pending = True
        self._validate_state_invariants(GameState.AUDIO_PREPARING)
        return self._preparation_event_result(
            responses,
            PreparationOutcome.FAILURE_PENDING_ACKNOWLEDGEMENT,
        )

    def _take_transition_e(
        self,
        responses: AudioPreparationResponseState,
        ready_tokens: frozenset[str],
    ) -> PreparationEventResult:
        game = self._active_game
        pending = game.pending_audio_preparation if game is not None else None
        if game is None or pending is None or not ready_tokens:
            raise DomainInvariantError(
                "transition E requires a pending preparation with READY targets"
            )
        round_id = self._round_id_factory()
        if not isinstance(round_id, UUID):
            raise DomainInvariantError("the round ID factory must return a UUID")
        playback_started_at = self._clock()
        if not isinstance(playback_started_at, datetime):
            raise DomainInvariantError("the engine clock must return a datetime")
        playback_started_monotonic = self.monotonic_time()
        playback_deadline_monotonic = (
            playback_started_monotonic + pending.playback_duration_seconds
        )

        active_round = ActiveRound(
            round_id=round_id,
            preparation_id=pending.preparation_id,
            deck_entry=pending.deck_entry,
            original_deck_position=pending.deck_entry.original_position,
            track_id=pending.deck_entry.track.track_id,
            configured_playback_offset_seconds=(
                pending.configured_playback_offset_seconds
            ),
            effective_playback_offset_seconds=(
                pending.effective_playback_offset_seconds
            ),
            playback_duration_seconds=pending.playback_duration_seconds,
            speed_bonus_plateau_percent=pending.speed_bonus_plateau_percent,
            buzz_eligible_browser_tokens=pending.buzz_eligible_browser_tokens,
            playback_clients=ready_tokens,
            playback_started_at=playback_started_at,
            playback_started_monotonic=playback_started_monotonic,
            playback_deadline_monotonic=playback_deadline_monotonic,
        )
        result = self._preparation_event_result(
            responses,
            PreparationOutcome.PLAYING,
            active_round=active_round,
        )
        game.active_round = active_round
        game.round_termination = None
        game.pending_audio_preparation = None
        game.audio_preparation_responses = None
        self._transition_to(GameState.PLAYING)
        return result

    @staticmethod
    def _preparation_event_result(
        responses: AudioPreparationResponseState,
        outcome: PreparationOutcome,
        *,
        active_round: ActiveRound | None = None,
    ) -> PreparationEventResult:
        return PreparationEventResult(
            preparation_id=responses.preparation_id,
            outcome=outcome,
            ready_browser_tokens=responses.ready_browser_tokens,
            failed_browser_tokens=responses.failed_browser_tokens,
            pending_browser_tokens=responses.pending_browser_tokens,
            active_round=active_round,
        )

    def join_game(
        self,
        browser_token: str,
        display_name: str | None = None,
        *,
        connection_changed: bool = True,
    ) -> GameMember:
        """Join or reconnect to a lobby or started game.

        A token already in this game reconnects that exact member without
        requiring a display name. If a name is supplied for an existing token,
        it is ignored because the stored game-local identity is authoritative.
        A new token must supply a valid, unique display name.

        This is an internal membership operation, not a state transition.
        ``connection_changed`` is supplied by the serialized application
        registry so an idempotent repeat for the same live connection does not
        reset its connection-specific audio capability.
        """

        game = self._game_for_membership_operation(allow_finished=False)
        token = self._validate_browser_token(
            browser_token, error_type=InvalidJoinGameRequestError
        )

        existing_member = game.members.get(token)
        if existing_member is not None:
            if not connection_changed:
                self._validate_state_invariants(self._state)
                return existing_member
            was_connected = existing_member.connected
            was_audio_usable = existing_member.browser_audio_usable
            existing_member.connected = True
            # A new connection must initialize/unlock audio for itself; a prior
            # connection's capability assertion is never inherited.
            existing_member.browser_audio_usable = False
            try:
                self._validate_state_invariants(self._state)
            except Exception:
                existing_member.connected = was_connected
                existing_member.browser_audio_usable = was_audio_usable
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
        """Mark a non-Songmaster member disconnected without removing it.

        Unexpected Songmaster loss is deliberately rejected here because it
        requires the section-37 reconnect-grace/failover overlay. This lower-
        level operation must not claim to have handled that global behavior.
        Repeated disconnect notifications for another member are idempotent.
        """

        game = self._game_for_membership_operation(allow_finished=True)
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
        was_audio_usable = member.browser_audio_usable
        member.connected = False
        member.browser_audio_usable = False
        try:
            self._validate_state_invariants(self._state)
        except Exception:
            member.connected = was_connected
            member.browser_audio_usable = was_audio_usable
            raise
        return member

    def record_connection_established(
        self,
        browser_token: str,
        *,
        connection_changed: bool,
    ) -> GameMember:
        """Apply domain connection facts after trusted registry validation."""

        game = self._game_for_membership_operation(allow_finished=True)
        token = self._validate_browser_token(
            browser_token, error_type=InvalidJoinGameRequestError
        )
        member = game.members.get(token)
        if member is None:
            raise UnknownGameMemberError(
                "browser_token does not belong to the active game"
            )
        if not isinstance(connection_changed, bool):
            raise InvalidJoinGameRequestError(
                "connection_changed must be a boolean"
            )
        if not connection_changed:
            self._validate_state_invariants(self._state)
            return member

        was_connected = member.connected
        was_audio_usable = member.browser_audio_usable
        member.connected = True
        member.browser_audio_usable = False
        try:
            self._validate_state_invariants(self._state)
        except Exception:
            member.connected = was_connected
            member.browser_audio_usable = was_audio_usable
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
    def _validated_monotonic_timestamp(
        value: object,
        *,
        error_type: type[GameEngineError],
        name: str,
    ) -> float:
        if (
            isinstance(value, bool)
            or not isinstance(value, Real)
            or not isfinite(value)
        ):
            raise error_type(f"{name} must be a finite real number")
        return float(value)

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

    def _game_for_membership_operation(
        self, *, allow_finished: bool
    ) -> GameSession:
        game = self._active_game
        if game is None:
            raise NoActiveGameError("there is no active game")
        allowed_states = {
            GameState.LOBBY,
            GameState.ROUND_COMPLETE,
            GameState.AUDIO_PREPARING,
            GameState.PLAYING,
            GameState.AWAITING_REVEAL,
            GameState.SCORING,
        }
        if allow_finished:
            allowed_states.add(GameState.FINISHED)
        if self._state not in allowed_states:
            raise MembershipOperationNotAllowedError(
                "membership operation is not allowed in the current state"
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

    def _enter_state(self, state: GameState) -> None:
        match state:
            case GameState.NO_GAME | GameState.LOBBY:
                # These states have invariants but no entry actions in this phase.
                return
            case GameState.ROUND_COMPLETE:
                self._enter_round_complete()
            case GameState.AUDIO_PREPARING:
                self._enter_audio_preparing()
            case GameState.PLAYING:
                self._enter_playing()
            case GameState.AWAITING_REVEAL:
                self._enter_awaiting_reveal()
            case GameState.SCORING:
                self._enter_scoring()
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

    def _enter_audio_preparing(self) -> None:
        """Enter preparation without playing audio or creating a real round.

        A future application/transport layer will translate the already-built
        pending context into opaque client preload commands. There is no domain
        effect or response collector in this increment.
        """

        game = self._active_game
        if game is None or game.pending_audio_preparation is None:
            raise DomainInvariantError(
                "AUDIO_PREPARING entry requires a pending preparation"
            )

    def _enter_playing(self) -> None:
        """Establish player-facing and buzz state for an actual round."""

        game = self._active_game
        if game is None or game.active_round is None:
            raise DomainInvariantError("PLAYING entry requires an actual round")
        game.displayed_deck_position = game.active_round.original_deck_position
        game.displayed_artist = None
        game.displayed_title = None
        game.buzzing_enabled = True

    def _enter_awaiting_reveal(self) -> None:
        """Disable buzzing while preserving the unconsumed current round."""

        game = self._active_game
        if (
            game is None
            or game.active_round is None
            or game.round_termination is None
        ):
            raise DomainInvariantError(
                "AWAITING_REVEAL entry requires a terminated actual round"
            )
        # Physical audio stop and browser notification remain transport work.
        # The application controller retires its playback timer after dispatch.
        game.buzzing_enabled = False

    def _enter_scoring(self) -> None:
        """Establish state-wide behavior after a buzz answer is revealed."""

        game = self._active_game
        if game is None or game.active_round is None:
            raise DomainInvariantError("SCORING entry requires an actual round")
        game.buzzing_enabled = False

    def _take_transition_k(self) -> None:
        """Apply transition K on authorized Play Song after deck exhaustion."""

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
        game.round_termination = None
        game.pending_audio_preparation = None
        game.audio_preparation_responses = None
        game.scoring_correction_round_id = None
        game.buzzing_enabled = False

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
            case GameState.AUDIO_PREPARING:
                self._validate_audio_preparing_invariants()
            case GameState.PLAYING:
                self._validate_playing_invariants()
            case GameState.AWAITING_REVEAL:
                self._validate_awaiting_reveal_invariants()
            case GameState.SCORING:
                self._validate_scoring_invariants()
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
        if game.round_termination is not None:
            raise DomainInvariantError("LOBBY cannot have round termination facts")
        if game.last_completed_round is not None:
            raise DomainInvariantError("LOBBY cannot have a completed round")
        if game.last_voided_round is not None:
            raise DomainInvariantError("LOBBY cannot have a voided round")
        if game.scoring_correction_round_id is not None:
            raise DomainInvariantError("LOBBY cannot have a score correction window")
        if game.pending_audio_preparation is not None:
            raise DomainInvariantError("LOBBY cannot have a pending preparation")
        if game.audio_preparation_responses is not None:
            raise DomainInvariantError("LOBBY cannot have preparation responses")
        if game.buzzing_enabled is not False:
            raise DomainInvariantError("buzzing must be disabled in LOBBY")
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
            if not isinstance(member.audio_enabled, bool):
                raise DomainInvariantError(
                    "game member audio preference must be boolean"
                )
            if not isinstance(member.browser_audio_usable, bool):
                raise DomainInvariantError(
                    "game member browser audio capability must be boolean"
                )
            if member.browser_audio_usable and not member.connected:
                raise DomainInvariantError(
                    "a disconnected member cannot retain usable browser audio"
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
            if skipped.reason not in {
                DeckSkipReason.DURATION_INELIGIBLE,
                DeckSkipReason.AUDIO_PREPARATION_FAILED,
            }:
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
        self._validate_last_completed_round(game, deck)
        return deck

    def _validate_last_completed_round(
        self,
        game: GameSession,
        deck: Deck,
    ) -> None:
        completed = game.last_completed_round
        if completed is None:
            return
        if not isinstance(completed, CompletedRound):
            raise DomainInvariantError(
                "the last completed round must be an immutable snapshot"
            )
        active_round = completed.active_round
        if not isinstance(active_round, ActiveRound):
            raise DomainInvariantError(
                "a completed round must retain its actual-round facts"
            )
        position = active_round.original_deck_position
        if not 1 <= position <= len(deck) or active_round.deck_entry is not deck[position - 1]:
            raise DomainInvariantError(
                "a completed round must retain its original deck entry"
            )
        if game.deck_pointer < position:
            raise DomainInvariantError(
                "the pointer cannot precede the last completed round"
            )
        if (
            isinstance(completed.awarded_points, bool)
            or not isinstance(completed.awarded_points, int)
            or completed.awarded_points < 0
        ):
            raise DomainInvariantError(
                "completed-round points must be a non-negative integer"
            )
        termination = completed.termination
        if not isinstance(termination, RoundTermination):
            raise DomainInvariantError(
                "a completed round must retain its termination outcome"
            )
        if termination.reason is RoundTerminationReason.PLAYBACK_TIMEOUT:
            if (
                termination.winning_browser_token is not None
                or termination.buzz_elapsed_seconds is not None
                or completed.scoring_choice is not None
                or completed.awarded_points != 0
            ):
                raise DomainInvariantError(
                    "a completed timeout round must have no scoring decision, "
                    "winner, or points"
                )
        elif termination.reason is RoundTerminationReason.BUZZ:
            winner = termination.winning_browser_token
            if (
                winner not in game.members
                or winner not in active_round.buzz_eligible_browser_tokens
            ):
                raise DomainInvariantError(
                    "a completed buzz round must retain an eligible winner"
                )
            elapsed = self._validated_monotonic_timestamp(
                termination.buzz_elapsed_seconds,
                error_type=DomainInvariantError,
                name="completed buzz elapsed time",
            )
            if not 0 <= elapsed < active_round.playback_duration_seconds:
                raise DomainInvariantError(
                    "completed buzz elapsed time must be within playback"
                )
            if not isinstance(completed.scoring_choice, ScoringChoice):
                raise DomainInvariantError(
                    "a completed buzz round must retain its scoring decision"
                )
            expected_points = self._calculate_round_points(
                active_round,
                elapsed,
                completed.scoring_choice,
            )
            if completed.awarded_points != expected_points:
                raise DomainInvariantError(
                    "completed buzz points must match its original scoring inputs"
                )
            if game.members[winner].committed_score < completed.awarded_points:
                raise DomainInvariantError(
                    "the winner's cumulative score must include the current award"
                )
        else:
            raise DomainInvariantError(
                "completed-round termination reason is not supported"
            )
        self._answer_for_reveal(active_round)

    def _validate_completed_round_display(
        self,
        game: GameSession,
        *,
        state_name: str,
    ) -> None:
        completed = game.last_completed_round
        if completed is None:
            return
        active_round = completed.active_round
        artist, title = self._answer_for_reveal(active_round)
        if (
            game.displayed_deck_position != active_round.original_deck_position
            or game.displayed_artist != artist
            or game.displayed_title != title
        ):
            raise DomainInvariantError(
                f"{state_name} must preserve the last completed answer display"
            )

    def _validate_round_complete_invariants(self) -> None:
        game = self._active_game
        if game is None:
            raise DomainInvariantError("ROUND_COMPLETE requires an active game")
        deck = self._validate_started_game_facts(
            game, state_name="ROUND_COMPLETE"
        )
        if game.active_round is not None:
            raise DomainInvariantError("ROUND_COMPLETE cannot have an active round")
        if game.last_voided_round is not None:
            raise DomainInvariantError("ROUND_COMPLETE cannot have a voided round")
        if game.round_termination is not None:
            raise DomainInvariantError(
                "ROUND_COMPLETE cannot have round termination facts"
            )
        if game.pending_audio_preparation is not None:
            raise DomainInvariantError(
                "ROUND_COMPLETE cannot have a pending preparation"
            )
        if game.audio_preparation_responses is not None:
            raise DomainInvariantError(
                "ROUND_COMPLETE cannot have preparation responses"
            )
        if game.buzzing_enabled is not False:
            raise DomainInvariantError(
                "buzzing must be disabled in ROUND_COMPLETE"
            )
        if game.ended_at is not None or game.end_reason is not None:
            raise DomainInvariantError(
                "ROUND_COMPLETE cannot have game finalization facts"
            )
        if game.final_standings != ():
            raise DomainInvariantError("ROUND_COMPLETE cannot have final standings")
        correction_round_id = game.scoring_correction_round_id
        completed = game.last_completed_round
        if correction_round_id is not None and (
            not isinstance(correction_round_id, UUID)
            or completed is None
            or completed.active_round.round_id != correction_round_id
            or completed.termination.reason is not RoundTerminationReason.BUZZ
            or completed.scoring_choice is None
        ):
            raise DomainInvariantError(
                "score correction eligibility must identify the completed buzz round"
            )
        self._validate_completed_round_display(
            game,
            state_name="ROUND_COMPLETE",
        )
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

    def _validate_audio_preparing_invariants(self) -> None:
        game = self._active_game
        if game is None:
            raise DomainInvariantError("AUDIO_PREPARING requires an active game")
        deck = self._validate_started_game_facts(
            game, state_name="AUDIO_PREPARING"
        )
        if game.deck_exhausted is not False or game.deck_pointer >= len(deck):
            raise DomainInvariantError(
                "AUDIO_PREPARING requires a non-exhausted candidate under the pointer"
            )
        if game.active_round is not None:
            raise DomainInvariantError(
                "AUDIO_PREPARING cannot have an active played round"
            )
        if game.last_voided_round is not None:
            raise DomainInvariantError("AUDIO_PREPARING cannot have a voided round")
        if game.round_termination is not None:
            raise DomainInvariantError(
                "AUDIO_PREPARING cannot have round termination facts"
            )
        if game.buzzing_enabled is not False:
            raise DomainInvariantError(
                "buzzing must be disabled in AUDIO_PREPARING"
            )
        if game.ended_at is not None or game.end_reason is not None:
            raise DomainInvariantError(
                "AUDIO_PREPARING cannot have game finalization facts"
            )
        if game.final_standings != ():
            raise DomainInvariantError(
                "AUDIO_PREPARING cannot have final standings"
            )
        if game.scoring_correction_round_id is not None:
            raise DomainInvariantError(
                "AUDIO_PREPARING cannot retain a score correction window"
            )

        pending = game.pending_audio_preparation
        if not isinstance(pending, PendingAudioPreparation):
            raise DomainInvariantError(
                "AUDIO_PREPARING requires an immutable pending preparation"
            )
        responses = game.audio_preparation_responses
        if not isinstance(responses, AudioPreparationResponseState):
            raise DomainInvariantError(
                "AUDIO_PREPARING requires preparation response bookkeeping"
            )
        if responses.preparation_id != pending.preparation_id:
            raise DomainInvariantError(
                "preparation responses must bind the pending preparation"
            )
        if not isinstance(responses.target_statuses, dict):
            raise DomainInvariantError(
                "preparation target statuses must be a mutable mapping"
            )
        if set(responses.target_statuses) != set(
            pending.audio_target_browser_tokens
        ):
            raise DomainInvariantError(
                "preparation response targets must match the immutable snapshot"
            )
        if any(
            not isinstance(status, AudioPreparationStatus)
            for status in responses.target_statuses.values()
        ):
            raise DomainInvariantError("preparation target statuses are invalid")
        if not isinstance(responses.outcome_determined, bool) or not isinstance(
            responses.failure_acknowledgement_pending, bool
        ):
            raise DomainInvariantError("preparation outcome flags must be boolean")
        if responses.outcome_determined:
            if (
                not responses.failure_acknowledgement_pending
                or responses.ready_browser_tokens
            ):
                raise DomainInvariantError(
                    "a determined preparation remaining in AUDIO_PREPARING must "
                    "await acknowledgement with zero READY targets"
                )
        elif responses.failure_acknowledgement_pending:
            raise DomainInvariantError(
                "failure acknowledgement cannot precede outcome determination"
            )
        elif not responses.pending_browser_tokens:
            raise DomainInvariantError(
                "all terminal responses must determine the preparation outcome"
            )
        if not isinstance(pending.preparation_id, UUID):
            raise DomainInvariantError("preparation identity must be a UUID")
        if pending.deck_entry is not deck[game.deck_pointer]:
            raise DomainInvariantError(
                "pending preparation must bind the exact deck entry under the pointer"
            )
        if not is_duration_eligible(
            pending.deck_entry.track, pending.playback_duration_seconds
        ):
            raise DomainInvariantError(
                "pending preparation deck entry must be duration eligible"
            )
        if (
            pending.configured_playback_offset_seconds
            != game.settings.playback_offset_seconds
            or pending.playback_duration_seconds
            != game.settings.playback_duration_seconds
            or pending.speed_bonus_plateau_percent
            != game.settings.speed_bonus_plateau_percent
        ):
            raise DomainInvariantError(
                "pending preparation settings must snapshot the prospective round"
            )
        expected_effective_offset = calculate_effective_playback_offset(
            pending.deck_entry.track,
            pending.configured_playback_offset_seconds,
            pending.playback_duration_seconds,
        )
        if pending.effective_playback_offset_seconds != expected_effective_offset:
            raise DomainInvariantError(
                "pending preparation effective offset is inconsistent"
            )
        if not isinstance(pending.buzz_eligible_browser_tokens, frozenset):
            raise DomainInvariantError("buzz eligibility snapshot must be immutable")
        if not isinstance(pending.audio_target_browser_tokens, frozenset) or not (
            pending.audio_target_browser_tokens
        ):
            raise DomainInvariantError(
                "audio target snapshot must be immutable and non-empty"
            )
        member_tokens = frozenset(game.members)
        if not pending.buzz_eligible_browser_tokens <= member_tokens:
            raise DomainInvariantError(
                "buzz eligibility must contain only game members"
            )
        if game.songmaster_browser_token not in (
            pending.buzz_eligible_browser_tokens
        ):
            raise DomainInvariantError(
                "the connected Songmaster must be prospectively buzz eligible"
            )
        if not pending.audio_target_browser_tokens <= (
            pending.buzz_eligible_browser_tokens
        ):
            raise DomainInvariantError(
                "audio targets must be connected prospective players"
            )
        self._validate_completed_round_display(
            game,
            state_name="AUDIO_PREPARING",
        )

    def _validate_actual_round_facts(
        self,
        game: GameSession,
        *,
        state_name: str,
        answer_revealed: bool = False,
    ) -> ActiveRound:
        deck = self._validate_started_game_facts(game, state_name=state_name)
        if game.deck_exhausted is not False or game.deck_pointer >= len(deck):
            raise DomainInvariantError(
                f"{state_name} requires a non-exhausted entry under the pointer"
            )
        active_round = game.active_round
        if not isinstance(active_round, ActiveRound):
            raise DomainInvariantError(f"{state_name} requires an actual round")
        if game.last_voided_round is not None:
            raise DomainInvariantError(f"{state_name} cannot have a voided round")
        if active_round.deck_entry is not deck[game.deck_pointer]:
            raise DomainInvariantError(
                "the actual round must bind the unconsumed deck entry under the pointer"
            )
        if active_round.original_deck_position != active_round.deck_entry.original_position:
            raise DomainInvariantError("actual round deck position is inconsistent")
        if active_round.track_id != active_round.deck_entry.track.track_id:
            raise DomainInvariantError("actual round track identity is inconsistent")
        if not isinstance(active_round.round_id, UUID) or not isinstance(
            active_round.preparation_id, UUID
        ):
            raise DomainInvariantError("round and preparation identities must be UUIDs")
        if not isinstance(active_round.playback_started_at, datetime):
            raise DomainInvariantError(
                f"{state_name} requires a UTC playback start timestamp"
            )
        started_monotonic = self._validated_monotonic_timestamp(
            active_round.playback_started_monotonic,
            error_type=DomainInvariantError,
            name="playback monotonic start",
        )
        deadline_monotonic = self._validated_monotonic_timestamp(
            active_round.playback_deadline_monotonic,
            error_type=DomainInvariantError,
            name="playback monotonic deadline",
        )
        if deadline_monotonic != (
            started_monotonic + active_round.playback_duration_seconds
        ):
            raise DomainInvariantError(
                "playback deadline must use the start clock domain and duration"
            )
        if (
            active_round.configured_playback_offset_seconds
            != game.settings.playback_offset_seconds
            or active_round.playback_duration_seconds
            != game.settings.playback_duration_seconds
            or active_round.speed_bonus_plateau_percent
            != game.settings.speed_bonus_plateau_percent
        ):
            raise DomainInvariantError(
                "actual round settings must preserve the preparation snapshot"
            )
        expected_effective_offset = calculate_effective_playback_offset(
            active_round.deck_entry.track,
            active_round.configured_playback_offset_seconds,
            active_round.playback_duration_seconds,
        )
        if active_round.effective_playback_offset_seconds != expected_effective_offset:
            raise DomainInvariantError("actual round effective offset is inconsistent")
        if not isinstance(
            active_round.buzz_eligible_browser_tokens, frozenset
        ) or not isinstance(active_round.playback_clients, frozenset):
            raise DomainInvariantError(
                "round eligibility and playback client snapshots must be immutable"
            )
        if not active_round.playback_clients:
            raise DomainInvariantError(
                f"{state_name} requires at least one playback client"
            )
        if not active_round.playback_clients <= active_round.buzz_eligible_browser_tokens:
            raise DomainInvariantError(
                "playback clients must belong to the frozen eligible player set"
            )
        if not active_round.buzz_eligible_browser_tokens <= frozenset(game.members):
            raise DomainInvariantError("round eligibility must contain only members")
        if game.pending_audio_preparation is not None:
            raise DomainInvariantError(
                f"{state_name} cannot retain a pending preparation"
            )
        if game.audio_preparation_responses is not None:
            raise DomainInvariantError(
                f"{state_name} cannot retain preparation responses"
            )
        if game.displayed_deck_position != active_round.original_deck_position:
            raise DomainInvariantError(
                f"{state_name} must display the actual deck position"
            )
        if answer_revealed:
            artist, title = self._answer_for_reveal(active_round)
            if (
                game.displayed_artist != artist
                or game.displayed_title != title
            ):
                raise DomainInvariantError(
                    f"{state_name} must display the actual round answer"
                )
        elif game.displayed_artist is not None or game.displayed_title is not None:
            raise DomainInvariantError(
                f"{state_name} answer display must remain blank"
            )
        if game.ended_at is not None or game.end_reason is not None:
            raise DomainInvariantError(
                f"{state_name} cannot have game finalization facts"
            )
        if game.final_standings != ():
            raise DomainInvariantError(f"{state_name} cannot have final standings")
        if game.scoring_correction_round_id is not None:
            raise DomainInvariantError(
                f"{state_name} cannot retain a score correction window"
            )
        return active_round

    def _validate_playing_invariants(self) -> None:
        game = self._active_game
        if game is None:
            raise DomainInvariantError("PLAYING requires an active game")
        self._validate_actual_round_facts(game, state_name="PLAYING")
        if game.round_termination is not None:
            raise DomainInvariantError("PLAYING cannot have round termination facts")
        if game.buzzing_enabled is not True:
            raise DomainInvariantError("buzzing must be enabled in PLAYING")

    def _validate_awaiting_reveal_invariants(self) -> None:
        game = self._active_game
        if game is None:
            raise DomainInvariantError("AWAITING_REVEAL requires an active game")
        active_round = self._validate_actual_round_facts(
            game, state_name="AWAITING_REVEAL"
        )
        if game.buzzing_enabled is not False:
            raise DomainInvariantError(
                "buzzing must be disabled in AWAITING_REVEAL"
            )
        termination = game.round_termination
        if not isinstance(termination, RoundTermination):
            raise DomainInvariantError(
                "AWAITING_REVEAL requires an immutable termination outcome"
            )
        if termination.reason is RoundTerminationReason.BUZZ:
            winner = termination.winning_browser_token
            if (
                winner not in game.members
                or winner not in active_round.buzz_eligible_browser_tokens
            ):
                raise DomainInvariantError(
                    "a buzz termination requires an eligible member as winner"
                )
            elapsed = self._validated_monotonic_timestamp(
                termination.buzz_elapsed_seconds,
                error_type=DomainInvariantError,
                name="buzz elapsed time",
            )
            if not 0 <= elapsed < active_round.playback_duration_seconds:
                raise DomainInvariantError(
                    "buzz elapsed time must be within the strict playback interval"
                )
        elif termination.reason is RoundTerminationReason.PLAYBACK_TIMEOUT:
            if (
                termination.winning_browser_token is not None
                or termination.buzz_elapsed_seconds is not None
            ):
                raise DomainInvariantError(
                    "a timeout termination cannot contain buzz winner facts"
                )
        else:
            raise DomainInvariantError("round termination reason is not supported")

    def _validate_scoring_invariants(self) -> None:
        game = self._active_game
        if game is None:
            raise DomainInvariantError("SCORING requires an active game")
        active_round = self._validate_actual_round_facts(
            game,
            state_name="SCORING",
            answer_revealed=True,
        )
        if game.buzzing_enabled is not False:
            raise DomainInvariantError("buzzing must be disabled in SCORING")
        termination = game.round_termination
        if not isinstance(termination, RoundTermination) or (
            termination.reason is not RoundTerminationReason.BUZZ
        ):
            raise DomainInvariantError(
                "SCORING requires a buzz termination outcome"
            )
        winner = termination.winning_browser_token
        if (
            winner not in game.members
            or winner not in active_round.buzz_eligible_browser_tokens
        ):
            raise DomainInvariantError(
                "SCORING requires an eligible winning member"
            )
        elapsed = self._validated_monotonic_timestamp(
            termination.buzz_elapsed_seconds,
            error_type=DomainInvariantError,
            name="buzz elapsed time",
        )
        if not 0 <= elapsed < active_round.playback_duration_seconds:
            raise DomainInvariantError(
                "SCORING requires elapsed time within the playback interval"
            )
        if (
            game.last_completed_round is not None
            and game.last_completed_round.active_round.round_id
            == active_round.round_id
        ):
            raise DomainInvariantError(
                "the current scoring round cannot already be completed"
            )

    def _validate_finished_invariants(self) -> None:
        game = self._active_game
        if game is None:
            raise DomainInvariantError("FINISHED requires an active game")
        deck = self._validate_started_game_facts(game, state_name="FINISHED")
        if game.active_round is not None:
            raise DomainInvariantError("FINISHED cannot have an active round")
        if game.round_termination is not None:
            raise DomainInvariantError("FINISHED cannot have round termination facts")
        if game.pending_audio_preparation is not None:
            raise DomainInvariantError("FINISHED cannot have a pending preparation")
        if game.audio_preparation_responses is not None:
            raise DomainInvariantError("FINISHED cannot have preparation responses")
        if game.buzzing_enabled is not False:
            raise DomainInvariantError("buzzing must be disabled in FINISHED")
        if game.scoring_correction_round_id is not None:
            raise DomainInvariantError(
                "FINISHED cannot retain a score correction window"
            )
        if game.end_reason is GameEndReason.DECK_EXHAUSTED:
            if game.deck_pointer != len(deck) or game.deck_exhausted is not True:
                raise DomainInvariantError(
                    "deck-exhausted FINISHED requires the pointer at deck end"
                )
            if game.last_voided_round is not None:
                raise DomainInvariantError(
                    "natural completion cannot retain a voided round"
                )
        elif game.end_reason is GameEndReason.SONGMASTER_ENDED_GAME:
            if game.last_voided_round is not None:
                self._validate_last_voided_round(game, deck)
        else:
            raise DomainInvariantError("FINISHED requires a supported end reason")
        if not isinstance(game.ended_at, datetime):
            raise DomainInvariantError("FINISHED requires a game end timestamp")
        if not isinstance(game.final_standings, tuple):
            raise DomainInvariantError("final standings must be immutable")
        if game.last_voided_round is None:
            self._validate_completed_round_display(
                game,
                state_name="FINISHED",
            )
        expected_standings = self._calculate_final_standings(game)
        if game.final_standings != expected_standings:
            raise DomainInvariantError(
                "final standings must match finalized member scores"
            )

    def _validate_last_voided_round(self, game: GameSession, deck: Deck) -> None:
        """Validate the bounded unfinished-round snapshot retained in FINISHED."""

        voided = game.last_voided_round
        if not isinstance(voided, VoidedRound):
            raise DomainInvariantError("a voided round must be an immutable snapshot")
        active_round = voided.active_round
        if not isinstance(active_round, ActiveRound):
            raise DomainInvariantError("a voided round must retain actual-round facts")
        position = active_round.original_deck_position
        if (
            not 1 <= position <= len(deck)
            or active_round.deck_entry is not deck[position - 1]
            or active_round.track_id != active_round.deck_entry.track.track_id
            or active_round.original_deck_position
            != active_round.deck_entry.original_position
        ):
            raise DomainInvariantError(
                "a voided round must retain its original deck entry"
            )
        if game.deck_pointer != position - 1 or game.deck_exhausted is not False:
            raise DomainInvariantError(
                "a voluntarily voided round must remain under the deck pointer"
            )
        if not isinstance(active_round.round_id, UUID) or not isinstance(
            active_round.preparation_id, UUID
        ):
            raise DomainInvariantError(
                "voided round and preparation identities must be UUIDs"
            )
        if not isinstance(active_round.playback_started_at, datetime):
            raise DomainInvariantError(
                "a voided round must retain its UTC playback timestamp"
            )
        started_monotonic = self._validated_monotonic_timestamp(
            active_round.playback_started_monotonic,
            error_type=DomainInvariantError,
            name="voided playback monotonic start",
        )
        deadline_monotonic = self._validated_monotonic_timestamp(
            active_round.playback_deadline_monotonic,
            error_type=DomainInvariantError,
            name="voided playback monotonic deadline",
        )
        if deadline_monotonic != (
            started_monotonic + active_round.playback_duration_seconds
        ):
            raise DomainInvariantError(
                "voided playback deadline must retain its start and duration"
            )
        if (
            active_round.configured_playback_offset_seconds
            != game.settings.playback_offset_seconds
            or active_round.playback_duration_seconds
            != game.settings.playback_duration_seconds
            or active_round.speed_bonus_plateau_percent
            != game.settings.speed_bonus_plateau_percent
        ):
            raise DomainInvariantError(
                "a voided round must retain its settings snapshot"
            )
        expected_offset = calculate_effective_playback_offset(
            active_round.deck_entry.track,
            active_round.configured_playback_offset_seconds,
            active_round.playback_duration_seconds,
        )
        if active_round.effective_playback_offset_seconds != expected_offset:
            raise DomainInvariantError(
                "voided round effective offset is inconsistent"
            )
        if not isinstance(
            active_round.buzz_eligible_browser_tokens, frozenset
        ) or not isinstance(active_round.playback_clients, frozenset):
            raise DomainInvariantError(
                "voided eligibility and playback clients must be immutable"
            )
        if not active_round.playback_clients or not (
            active_round.playback_clients
            <= active_round.buzz_eligible_browser_tokens
            <= frozenset(game.members)
        ):
            raise DomainInvariantError(
                "voided playback clients and eligibility are inconsistent"
            )
        if voided.reason is not RoundVoidReason.GAME_ENDED:
            raise DomainInvariantError("voided-round reason is not supported")
        if not isinstance(voided.answer_revealed, bool):
            raise DomainInvariantError("voided answer-reveal status must be boolean")

        termination = voided.termination
        if termination is not None:
            if not isinstance(termination, RoundTermination):
                raise DomainInvariantError(
                    "voided termination facts must be immutable"
                )
            if termination.reason is RoundTerminationReason.BUZZ:
                winner = termination.winning_browser_token
                if (
                    winner not in game.members
                    or winner not in active_round.buzz_eligible_browser_tokens
                ):
                    raise DomainInvariantError(
                        "a voided buzz termination requires its eligible winner"
                    )
                elapsed = self._validated_monotonic_timestamp(
                    termination.buzz_elapsed_seconds,
                    error_type=DomainInvariantError,
                    name="voided buzz elapsed time",
                )
                if not 0 <= elapsed < active_round.playback_duration_seconds:
                    raise DomainInvariantError(
                        "voided buzz elapsed time must be within playback"
                    )
            elif termination.reason is RoundTerminationReason.PLAYBACK_TIMEOUT:
                if (
                    termination.winning_browser_token is not None
                    or termination.buzz_elapsed_seconds is not None
                ):
                    raise DomainInvariantError(
                        "a voided timeout cannot contain buzz winner facts"
                    )
            else:
                raise DomainInvariantError(
                    "voided-round termination reason is not supported"
                )
        if voided.answer_revealed and (
            termination is None
            or termination.reason is not RoundTerminationReason.BUZZ
        ):
            raise DomainInvariantError(
                "a revealed void requires the prior buzz termination"
            )

        if game.displayed_deck_position != position:
            raise DomainInvariantError(
                "FINISHED must preserve the voided round's displayed position"
            )
        if voided.answer_revealed:
            artist, title = self._answer_for_reveal(active_round)
            if game.displayed_artist != artist or game.displayed_title != title:
                raise DomainInvariantError(
                    "FINISHED must preserve the revealed voided answer"
                )
        elif game.displayed_artist is not None or game.displayed_title is not None:
            raise DomainInvariantError(
                "FINISHED cannot reveal an unanswered voided round"
            )
