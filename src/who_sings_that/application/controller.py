"""Serialized application-level ownership of authoritative game work.

The controller deliberately executes synchronous domain and persistence work on
its asyncio event-loop thread.  That keeps each message one indivisible unit of
authoritative work for this phase, at the cost of blocking unrelated loop work
while persistent playlist and media inspection is in progress.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from math import isfinite
from numbers import Real
from os import PathLike
from typing import Any, Final
from uuid import UUID

from sqlalchemy import Engine

from who_sings_that.catalog import extract_track_metadata
from who_sings_that.domain import (
    DomainInvariantError,
    GameEngine,
    GameEngineError,
    GameFinalizationResult,
    GameState,
    InvalidPlayerNameError,
    PendingAudioPreparation,
    PreparationEventResult,
    PreparationOutcome,
    SongmasterRecovery,
    SongmasterRecoveryStatus,
    UnknownGameMemberError,
)

from .connections import (
    ConnectionRegistration,
    ConnectionRegistrationStatus,
    ConnectionRegistry,
    InvalidConnectionIdError,
)
from .messages import (
    AcceptScoreRequest,
    AcknowledgeAudioPreparationFailureRequest,
    AudioPreparationFailedEvent,
    AudioPreparationReadyEvent,
    AudioPreparationTimeoutEvent,
    AuthoritativeMessage,
    BrowserAudioCapabilityChangedEvent,
    BuzzRequest,
    CancelGameRequest,
    ClientRequest,
    CloseGameRequest,
    CorrectScoreRequest,
    CreateGameRequest,
    DeselectPlaylistRequest,
    EndGameRequest,
    JoinGameRequest,
    MemberDisconnectedEvent,
    PlaybackTimeoutEvent,
    PlaySongRequest,
    RegisterConnectionRequest,
    RevealRequest,
    SelectPlaylistRequest,
    SetAudioPreferenceRequest,
    SongmasterReconnectGraceExpiredEvent,
    StartGameRequest,
    SystemEvent,
    TransferSongmasterRequest,
)
from .start_game import (
    InvalidMusicRootError,
    InvalidSelectedPlaylistIdsError,
    MediaInspector,
    NoStructurallyEligibleTracksError,
    SelectedPlaylistNotFoundError,
    start_game_from_persistent_playlists,
)


class GameControllerError(RuntimeError):
    """Base class for controller lifecycle and routing errors."""


class ControllerNotRunningError(GameControllerError):
    """Raised when new work is submitted to a stopped controller."""


class ConsumerAlreadyRunningError(GameControllerError):
    """Raised when startup would create a second message consumer."""


class ConsumerSelfRequestError(GameControllerError):
    """Raised instead of allowing the consumer to await its own queue."""


class UnsupportedMessageError(GameControllerError):
    """Raised when dispatch has no implementation for a message record."""


class StaleGameSessionError(GameControllerError):
    """Expected rejection for work that does not target the active game."""


class StaleConnectionError(GameControllerError):
    """Expected rejection for work from a non-current browser connection."""


class ControllerFailedError(GameControllerError):
    """Raised when an earlier unexpected handler error stopped the controller."""

    def __init__(self, failure: BaseException):
        super().__init__(
            "the authoritative message consumer stopped after an unexpected error"
        )
        self.failure = failure


@dataclass(frozen=True, slots=True)
class SystemEventRejection:
    """Observable expected rejection of an unsolicited system event."""

    event: SystemEvent
    error: Exception


@dataclass(frozen=True, slots=True)
class _TrustedBuzzSubmission:
    """Application-owned metadata attached at authoritative request ingress."""

    request: BuzzRequest
    received_at_monotonic: float


@dataclass(frozen=True, slots=True)
class _TrustedConnectionSubmission:
    """Trusted ingress time for connection establishment and grace ordering."""

    request: JoinGameRequest | RegisterConnectionRequest
    received_at_monotonic: float


@dataclass(slots=True)
class _QueuedMessage:
    message: (
        AuthoritativeMessage
        | _TrustedBuzzSubmission
        | _TrustedConnectionSubmission
    )
    response: asyncio.Future[Any] | None


class _StopConsumer:
    pass


_STOP_CONSUMER: Final = _StopConsumer()
DEFAULT_PREPARATION_TIMEOUT_SECONDS: Final = 5.0
DEFAULT_SONGMASTER_RECONNECT_GRACE_SECONDS: Final = 10.0
_EXPECTED_APPLICATION_ERRORS: Final = (
    InvalidMusicRootError,
    InvalidSelectedPlaylistIdsError,
    NoStructurallyEligibleTracksError,
    SelectedPlaylistNotFoundError,
    StaleGameSessionError,
    StaleConnectionError,
    InvalidConnectionIdError,
)


class GameController:
    """Own one engine, one global FIFO queue, and one synchronous dispatcher.

    ``stop`` is a draining shutdown: it stops accepting new messages, appends a
    sentinel to the queue, and processes every message accepted before that
    sentinel.  All accepted client requests therefore receive a result or an
    exception.  An unexpected handler error instead fails the controller
    immediately; the triggering request receives that error, queued requests
    receive :class:`ControllerFailedError`, and queued system events are dropped.
    The original failure remains available through :attr:`failure`.
    """

    def __init__(
        self,
        database_engine: Engine,
        music_root: str | PathLike[str],
        *,
        game_engine: GameEngine | None = None,
        inspect_media: MediaInspector = extract_track_metadata,
        preparation_timeout_seconds: float = DEFAULT_PREPARATION_TIMEOUT_SECONDS,
        preparation_timeout_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        playback_timeout_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        songmaster_reconnect_grace_seconds: float = (
            DEFAULT_SONGMASTER_RECONNECT_GRACE_SECONDS
        ),
        songmaster_reconnect_grace_sleep: Callable[
            [float], Awaitable[None]
        ] = asyncio.sleep,
    ) -> None:
        if (
            isinstance(preparation_timeout_seconds, bool)
            or not isinstance(preparation_timeout_seconds, Real)
            or not isfinite(preparation_timeout_seconds)
            or preparation_timeout_seconds <= 0
        ):
            raise ValueError(
                "preparation_timeout_seconds must be a positive finite number"
            )
        if (
            isinstance(songmaster_reconnect_grace_seconds, bool)
            or not isinstance(songmaster_reconnect_grace_seconds, Real)
            or not isfinite(songmaster_reconnect_grace_seconds)
            or songmaster_reconnect_grace_seconds <= 0
        ):
            raise ValueError(
                "songmaster_reconnect_grace_seconds must be a positive finite number"
            )
        self._game_engine = game_engine if game_engine is not None else GameEngine()
        self._database_engine = database_engine
        self._music_root = music_root
        self._inspect_media = inspect_media
        self._preparation_timeout_seconds = float(preparation_timeout_seconds)
        self._preparation_timeout_sleep = preparation_timeout_sleep
        self._playback_timeout_sleep = playback_timeout_sleep
        self._songmaster_reconnect_grace_seconds = float(
            songmaster_reconnect_grace_seconds
        )
        self._songmaster_reconnect_grace_sleep = (
            songmaster_reconnect_grace_sleep
        )
        self._queue: asyncio.Queue[_QueuedMessage | _StopConsumer] = asyncio.Queue()
        self._consumer_task: asyncio.Task[None] | None = None
        self._consumer_ready: asyncio.Event | None = None
        self._accepting_messages = False
        self._failure: BaseException | None = None
        self._system_event_rejections: list[SystemEventRejection] = []
        self._connection_registry = ConnectionRegistry()
        self._preparation_timeout_task: asyncio.Task[None] | None = None
        self._playback_timeout_task: asyncio.Task[None] | None = None
        self._songmaster_reconnect_grace_task: asyncio.Task[None] | None = None
        self._owned_timer_tasks: set[asyncio.Task[None]] = set()

    @property
    def game_engine(self) -> GameEngine:
        """Return the one authoritative synchronous domain engine."""

        return self._game_engine

    @property
    def is_running(self) -> bool:
        """Return whether the controller is accepting and consuming messages."""

        task = self._consumer_task
        return self._accepting_messages and task is not None and not task.done()

    @property
    def failure(self) -> BaseException | None:
        """Return the unexpected error that stopped the consumer, if any."""

        return self._failure

    @property
    def system_event_rejections(self) -> tuple[SystemEventRejection, ...]:
        """Return expected event rejections observed since construction."""

        return tuple(self._system_event_rejections)

    @property
    def connection_registry(self) -> ConnectionRegistry:
        """Return the application-owned registry for diagnostics and adapters."""

        return self._connection_registry

    @property
    def preparation_timeout_active(self) -> bool:
        """Return whether a live preparation timeout task is currently owned."""

        task = self._preparation_timeout_task
        return task is not None and not task.done()

    @property
    def playback_timeout_active(self) -> bool:
        """Return whether a live playback timeout task is currently owned."""

        task = self._playback_timeout_task
        return task is not None and not task.done()

    @property
    def songmaster_reconnect_grace_active(self) -> bool:
        """Return whether the controller owns a live reconnect-grace timer."""

        task = self._songmaster_reconnect_grace_task
        return task is not None and not task.done()

    async def start(self) -> None:
        """Start exactly one consumer and wait until it is ready for work."""

        if self._failure is not None:
            raise ControllerFailedError(self._failure) from self._failure
        if self._consumer_task is not None:
            raise ConsumerAlreadyRunningError(
                "this controller already has a message consumer"
            )

        self._consumer_ready = asyncio.Event()
        self._accepting_messages = True
        self._consumer_task = asyncio.create_task(
            self._consume_messages(),
            name="wst-authoritative-game-consumer",
        )
        await self._consumer_ready.wait()

    async def stop(self) -> None:
        """Stop accepting work, drain accepted messages, and stop the consumer."""

        task = self._consumer_task
        if task is None:
            if self._failure is not None:
                raise ControllerFailedError(self._failure) from self._failure
            return
        if asyncio.current_task() is task:
            raise ConsumerSelfRequestError("the consumer cannot stop and await itself")

        if self._accepting_messages:
            self._accepting_messages = False
            self._retire_preparation_timeout()
            self._retire_playback_timeout()
            self._retire_songmaster_reconnect_grace()
            self._queue.put_nowait(_STOP_CONSUMER)
        await task
        self._retire_preparation_timeout()
        self._retire_playback_timeout()
        self._retire_songmaster_reconnect_grace()
        await self._await_timer_cleanup()
        self._consumer_task = None
        self._consumer_ready = None
        if self._failure is not None:
            raise ControllerFailedError(self._failure) from self._failure

    async def submit_request(self, request: ClientRequest) -> Any:
        """Queue a client request and await its terminal result or rejection.

        Shielding the private response future means cancellation of the waiting
        caller does not cancel authoritative work that the controller accepted.
        """

        if not isinstance(request, ClientRequest):
            raise TypeError("request must be a ClientRequest")
        if asyncio.current_task() is self._consumer_task:
            raise ConsumerSelfRequestError(
                "the queue consumer cannot submit and await its own request"
            )
        self._require_running()

        response = asyncio.get_running_loop().create_future()
        response.add_done_callback(_observe_response_exception)
        queued_request: ClientRequest | _TrustedBuzzSubmission = request
        if isinstance(request, BuzzRequest):
            # This is the current trusted ingress boundary. Future WebSocket
            # integration must perform this capture promptly when the message
            # reaches authoritative server code, before any queue wait.
            queued_request = _TrustedBuzzSubmission(
                request=request,
                received_at_monotonic=self._game_engine.monotonic_time(),
            )
        elif isinstance(request, (JoinGameRequest, RegisterConnectionRequest)):
            queued_request = _TrustedConnectionSubmission(
                request=request,
                received_at_monotonic=self._game_engine.monotonic_time(),
            )
        self._queue.put_nowait(_QueuedMessage(queued_request, response))
        return await asyncio.shield(response)

    def post_system_event(self, event: SystemEvent) -> None:
        """Append a trusted unsolicited event without waiting for processing."""

        if not isinstance(event, SystemEvent):
            raise TypeError("event must be a SystemEvent")
        self._require_running()
        self._queue.put_nowait(_QueuedMessage(event, None))

    def _require_running(self) -> None:
        if not self.is_running:
            if self._failure is not None:
                raise ControllerFailedError(self._failure) from self._failure
            raise ControllerNotRunningError(
                "the controller must be started before messages are submitted"
            )

    async def _consume_messages(self) -> None:
        ready = self._consumer_ready
        if ready is None:  # pragma: no cover - start establishes this fact
            raise RuntimeError("consumer readiness was not initialized")
        ready.set()

        while True:
            queued = await self._queue.get()
            try:
                if queued is _STOP_CONSUMER:
                    return
                try:
                    result = self._dispatch(queued.message)
                except Exception as error:  # noqa: BLE001 - consumer failure boundary
                    if self._is_expected_operational_error(error):
                        self._complete_expected_rejection(queued, error)
                        continue
                    self._fail_controller(queued, error)
                    return
                self._complete_response(queued.response, result=result)
            finally:
                self._queue.task_done()

    def _dispatch(
        self,
        message: (
            AuthoritativeMessage
            | _TrustedBuzzSubmission
            | _TrustedConnectionSubmission
        ),
    ) -> object:
        """Route one message to existing application or domain operations."""

        if isinstance(message, _TrustedBuzzSubmission):
            request = message.request
            self._require_current_connection(
                request.game_session_id,
                request.requesting_browser_token,
                request.source_connection_id,
            )
            result = self._game_engine.accept_buzz(
                request.game_session_id,
                request.round_id,
                request.requesting_browser_token,
                message.received_at_monotonic,
            )
            self._retire_playback_timeout()
            return result

        if isinstance(message, _TrustedConnectionSubmission):
            request = message.request
            if isinstance(request, JoinGameRequest):
                return self._join_game_from_connection(
                    request,
                    message.received_at_monotonic,
                )
            return self._register_member_connection(
                request,
                message.received_at_monotonic,
            )

        if isinstance(message, CreateGameRequest):
            self._connection_registry.validate_connection_id(
                message.source_connection_id
            )
            game = self._game_engine.create_game(
                message.browser_token,
                message.display_name,
            )
            self._game_engine.record_connection_established(
                message.browser_token,
                connection_changed=True,
            )
            self._connection_registry.register(
                game.game_session_id,
                message.browser_token,
                message.source_connection_id,
            )
            return game
        if isinstance(message, CancelGameRequest):
            self._require_target_game(message.game_session_id)
            result = self._game_engine.cancel_game(
                message.game_session_id,
                message.requesting_browser_token,
            )
            self._retire_preparation_timeout()
            self._retire_playback_timeout()
            self._connection_registry.clear_game(message.game_session_id)
            return result
        if isinstance(message, (JoinGameRequest, RegisterConnectionRequest)):
            raise DomainInvariantError(
                "connection requests require trusted ingress metadata"
            )
        if isinstance(message, SelectPlaylistRequest):
            self._require_target_game(message.game_session_id)
            return self._game_engine.select_playlist(
                message.requesting_browser_token,
                message.playlist_id,
            )
        if isinstance(message, DeselectPlaylistRequest):
            self._require_target_game(message.game_session_id)
            return self._game_engine.deselect_playlist(
                message.requesting_browser_token,
                message.playlist_id,
            )
        if isinstance(message, StartGameRequest):
            self._require_target_game(message.game_session_id)
            return start_game_from_persistent_playlists(
                self._game_engine,
                self._database_engine,
                self._music_root,
                message.requesting_browser_token,
                inspect_media=self._inspect_media,
            )
        if isinstance(message, SetAudioPreferenceRequest):
            self._require_target_game(message.game_session_id)
            return self._game_engine.set_audio_preference(
                message.requesting_browser_token,
                message.audio_enabled,
            )
        if isinstance(message, TransferSongmasterRequest):
            self._require_target_game(message.game_session_id)
            return self._game_engine.transfer_songmaster(
                message.requesting_browser_token,
                message.recipient_browser_token,
            )
        if isinstance(message, PlaySongRequest):
            self._require_target_game(message.game_session_id)
            result = self._game_engine.play_song(message.requesting_browser_token)
            if isinstance(result, PendingAudioPreparation):
                self._schedule_preparation_timeout(
                    message.game_session_id,
                    result.preparation_id,
                )
            elif not isinstance(result, GameFinalizationResult):  # pragma: no cover
                raise DomainInvariantError(
                    "Play Song must return preparation or finalization facts"
                )
            return result
        if isinstance(message, EndGameRequest):
            self._require_target_game(message.game_session_id)
            result = self._game_engine.end_game(
                message.game_session_id,
                message.requesting_browser_token,
            )
            self._retire_preparation_timeout()
            self._retire_playback_timeout()
            return result
        if isinstance(message, CloseGameRequest):
            self._require_target_game(message.game_session_id)
            result = self._game_engine.close_game(
                message.game_session_id,
                message.requesting_browser_token,
            )
            self._retire_preparation_timeout()
            self._retire_playback_timeout()
            self._connection_registry.clear_game(message.game_session_id)
            return result
        if isinstance(message, AudioPreparationReadyEvent):
            self._require_current_connection(
                message.game_session_id,
                message.browser_token,
                message.source_connection_id,
            )
            result = self._game_engine.record_audio_preparation_ready(
                message.game_session_id,
                message.preparation_id,
                message.browser_token,
            )
            self._complete_preparation_event(result)
            return result
        if isinstance(message, AudioPreparationFailedEvent):
            self._require_current_connection(
                message.game_session_id,
                message.browser_token,
                message.source_connection_id,
            )
            result = self._game_engine.record_audio_preparation_failed(
                message.game_session_id,
                message.preparation_id,
                message.browser_token,
            )
            self._complete_preparation_event(result)
            return result
        if isinstance(message, AudioPreparationTimeoutEvent):
            result = self._game_engine.audio_preparation_timed_out(
                message.game_session_id,
                message.preparation_id,
            )
            self._complete_preparation_event(result)
            return result
        if isinstance(message, PlaybackTimeoutEvent):
            result = self._game_engine.playback_timed_out(
                message.game_session_id,
                message.round_id,
                self._game_engine.monotonic_time(),
            )
            self._retire_playback_timeout()
            return result
        if isinstance(message, SongmasterReconnectGraceExpiredEvent):
            result = self._expire_songmaster_recovery(
                message.game_session_id,
                message.recovery_id,
                self._game_engine.monotonic_time(),
            )
            self._retire_songmaster_reconnect_grace()
            return result
        if isinstance(message, RevealRequest):
            self._require_target_game(message.game_session_id)
            return self._game_engine.reveal_answer(
                message.game_session_id,
                message.round_id,
                message.requesting_browser_token,
            )
        if isinstance(message, AcceptScoreRequest):
            return self._game_engine.accept_score(
                message.game_session_id,
                message.round_id,
                message.requesting_browser_token,
                message.scoring_choice,
            )
        if isinstance(message, CorrectScoreRequest):
            return self._game_engine.correct_score(
                message.game_session_id,
                message.round_id,
                message.requesting_browser_token,
                message.scoring_choice,
            )
        if isinstance(message, AcknowledgeAudioPreparationFailureRequest):
            result = self._game_engine.acknowledge_audio_preparation_failure(
                message.game_session_id,
                message.preparation_id,
                message.requesting_browser_token,
            )
            self._retire_preparation_timeout()
            return result
        if isinstance(message, MemberDisconnectedEvent):
            self._require_current_connection(
                message.game_session_id,
                message.browser_token,
                message.source_connection_id,
            )
            game = self._game_engine.active_game
            if game is None:  # pragma: no cover - current connection established it
                raise DomainInvariantError("disconnect requires an active game")
            was_playing = self._game_engine.state is GameState.PLAYING
            if message.browser_token == game.songmaster_browser_token:
                started = self._game_engine.monotonic_time()
                recovery = self._game_engine.begin_songmaster_recovery(
                    message.browser_token,
                    started,
                    started + self._songmaster_reconnect_grace_seconds,
                )
                member = game.members[message.browser_token]
            else:
                recovery = None
                member = self._game_engine.disconnect_member(message.browser_token)
            if not self._connection_registry.mark_disconnected(
                message.game_session_id,
                message.browser_token,
                message.source_connection_id,
            ):
                raise DomainInvariantError(
                    "validated current connection could not be retired"
                )
            if was_playing and self._game_engine.state is GameState.ROUND_COMPLETE:
                self._retire_playback_timeout()
            if recovery is not None:
                self._retire_preparation_timeout()
                self._retire_playback_timeout()
                self._schedule_songmaster_reconnect_grace(
                    message.game_session_id,
                    recovery,
                )
            return member
        if isinstance(message, BrowserAudioCapabilityChangedEvent):
            self._require_current_connection(
                message.game_session_id,
                message.browser_token,
                message.source_connection_id,
            )
            return self._game_engine.set_browser_audio_capability(
                message.browser_token,
                message.browser_audio_usable,
            )
        raise UnsupportedMessageError(
            f"no authoritative handler for {type(message).__name__}"
        )

    def _require_target_game(self, game_session_id: object) -> None:
        active_game = self._game_engine.active_game
        if active_game is None or active_game.game_session_id != game_session_id:
            raise StaleGameSessionError(
                "message does not target the active game session"
            )

    def _join_game_from_connection(
        self,
        message: JoinGameRequest,
        received_at_monotonic: float,
    ) -> object:
        self._require_target_game(message.game_session_id)
        status = self._connection_registry.registration_status(
            message.game_session_id,
            message.browser_token,
            message.source_connection_id,
        )
        prior_state = self._game_engine.state
        handled = self._prepare_songmaster_connection(
            message.game_session_id,
            message.browser_token,
            status,
            received_at_monotonic,
        )
        game = self._game_engine.active_game
        if game is None:  # pragma: no cover - target validation established it
            raise DomainInvariantError("Join Game requires an active game")
        resume_indefinite = (
            game.songmaster_recovery is not None
            and game.songmaster_recovery.status
            is SongmasterRecoveryStatus.INDEFINITE_SUSPENSION
        )
        if handled:
            member = game.members[message.browser_token]
        else:
            member = self._game_engine.join_game(
                message.browser_token,
                message.display_name,
                connection_changed=(
                    status is not ConnectionRegistrationStatus.UNCHANGED
                ),
                resume_indefinite_as_songmaster=resume_indefinite,
            )
        self._connection_registry.register(
            message.game_session_id,
            message.browser_token,
            message.source_connection_id,
        )
        self._retire_interrupted_normal_timers(prior_state)
        if game.songmaster_recovery is None:
            self._retire_songmaster_reconnect_grace()
        return member

    def _register_member_connection(
        self,
        message: RegisterConnectionRequest,
        received_at_monotonic: float,
    ) -> ConnectionRegistration:
        game = self._game_engine.active_game
        if game is None or message.browser_token not in game.members:
            raise UnknownGameMemberError(
                "browser_token does not belong to the active game"
            )
        status = self._connection_registry.registration_status(
            message.game_session_id,
            message.browser_token,
            message.connection_id,
        )
        prior_state = self._game_engine.state
        handled = self._prepare_songmaster_connection(
            message.game_session_id,
            message.browser_token,
            status,
            received_at_monotonic,
        )
        recovery = game.songmaster_recovery
        if not handled:
            self._game_engine.record_connection_established(
                message.browser_token,
                connection_changed=(
                    status is not ConnectionRegistrationStatus.UNCHANGED
                ),
                resume_indefinite_as_songmaster=(
                    recovery is not None
                    and recovery.status
                    is SongmasterRecoveryStatus.INDEFINITE_SUSPENSION
                ),
            )
        registration = self._connection_registry.register(
            message.game_session_id,
            message.browser_token,
            message.connection_id,
        )
        self._retire_interrupted_normal_timers(prior_state)
        if game.songmaster_recovery is None:
            self._retire_songmaster_reconnect_grace()
        return registration

    def _prepare_songmaster_connection(
        self,
        game_session_id: UUID,
        browser_token: str,
        status: ConnectionRegistrationStatus,
        received_at_monotonic: float,
    ) -> bool:
        """Apply grace ordering or instantaneous replacement normalization."""

        game = self._game_engine.active_game
        if game is None:
            raise DomainInvariantError("connection establishment requires a game")
        recovery = game.songmaster_recovery
        if (
            recovery is not None
            and recovery.status is SongmasterRecoveryStatus.RECONNECT_GRACE
        ):
            deadline = recovery.grace_deadline_monotonic
            if deadline is None:  # pragma: no cover - domain invariant
                raise DomainInvariantError("reconnect grace requires a deadline")
            if received_at_monotonic >= deadline:
                self._expire_songmaster_recovery(
                    game_session_id,
                    recovery.recovery_id,
                    received_at_monotonic,
                )
                self._retire_songmaster_reconnect_grace()
                recovery = game.songmaster_recovery
            elif browser_token == recovery.original_songmaster_browser_token:
                self._game_engine.recover_original_songmaster(
                    browser_token,
                    received_at_monotonic,
                )
                return True

        if (
            game.songmaster_recovery is None
            and browser_token == game.songmaster_browser_token
            and status is ConnectionRegistrationStatus.REPLACED
        ):
            started = received_at_monotonic
            self._game_engine.begin_songmaster_recovery(
                browser_token,
                started,
                started + self._songmaster_reconnect_grace_seconds,
            )
            self._game_engine.recover_original_songmaster(
                browser_token,
                received_at_monotonic,
            )
            return True
        return False

    def _expire_songmaster_recovery(
        self,
        game_session_id: UUID,
        recovery_id: UUID,
        observed_at_monotonic: float,
    ) -> object:
        game = self._game_engine.active_game
        recovery = game.songmaster_recovery if game is not None else None
        if game is None or recovery is None:
            return self._game_engine.expire_songmaster_recovery(
                game_session_id,
                recovery_id,
                observed_at_monotonic,
                None,
            )
        connected = dict(
            self._connection_registry.connected_members(game_session_id)
        )
        candidates = tuple(
            member
            for member in game.members.values()
            if member.connected
            and member.browser_token
            != recovery.original_songmaster_browser_token
            and member.browser_token in connected
        )
        replacement = min(
            candidates,
            key=lambda member: (
                connected[member.browser_token],
                member.join_order,
            ),
            default=None,
        )
        return self._game_engine.expire_songmaster_recovery(
            game_session_id,
            recovery_id,
            observed_at_monotonic,
            None if replacement is None else replacement.browser_token,
        )

    def _retire_interrupted_normal_timers(self, prior_state: GameState) -> None:
        if prior_state is GameState.AUDIO_PREPARING and (
            self._game_engine.state is not GameState.AUDIO_PREPARING
        ):
            self._retire_preparation_timeout()
        if prior_state is GameState.PLAYING and (
            self._game_engine.state is not GameState.PLAYING
        ):
            self._retire_playback_timeout()

    def _require_current_connection(
        self,
        game_session_id: UUID,
        browser_token: str,
        source_connection_id: str,
    ) -> None:
        self._require_target_game(game_session_id)
        game = self._game_engine.active_game
        if game is None or browser_token not in game.members:  # pragma: no cover
            raise UnknownGameMemberError(
                "browser_token does not belong to the active game"
            )
        if not self._connection_registry.is_current(
            game_session_id,
            browser_token,
            source_connection_id,
        ):
            raise StaleConnectionError(
                "event or request did not come from the member's current connection"
            )

    def _schedule_preparation_timeout(
        self,
        game_session_id: UUID,
        preparation_id: UUID,
    ) -> None:
        self._retire_preparation_timeout()
        task = asyncio.create_task(
            self._post_preparation_timeout(game_session_id, preparation_id),
            name="wst-audio-preparation-timeout",
        )
        self._preparation_timeout_task = task
        self._owned_timer_tasks.add(task)
        task.add_done_callback(self._timer_task_done)

    async def _post_preparation_timeout(
        self,
        game_session_id: UUID,
        preparation_id: UUID,
    ) -> None:
        try:
            await self._preparation_timeout_sleep(
                self._preparation_timeout_seconds
            )
        except asyncio.CancelledError:
            return
        try:
            self.post_system_event(
                AudioPreparationTimeoutEvent(
                    game_session_id=game_session_id,
                    preparation_id=preparation_id,
                )
            )
        except (ControllerNotRunningError, ControllerFailedError):
            # Shutdown/failure retires owned timers; a racing expiry is harmless.
            return

    def _complete_preparation_event(
        self,
        result: PreparationEventResult,
    ) -> None:
        if result.outcome is not PreparationOutcome.COLLECTING:
            self._retire_preparation_timeout()
        if result.outcome is PreparationOutcome.PLAYING:
            game = self._game_engine.active_game
            active_round = result.active_round
            if game is None or active_round is None:
                raise DomainInvariantError(
                    "a PLAYING preparation result requires an actual round"
                )
            self._schedule_playback_timeout(
                game.game_session_id,
                active_round.round_id,
                active_round.playback_deadline_monotonic,
            )

    def _retire_preparation_timeout(self) -> None:
        task = self._preparation_timeout_task
        self._preparation_timeout_task = None
        if task is not None and not task.done():
            task.cancel()

    def _schedule_playback_timeout(
        self,
        game_session_id: UUID,
        round_id: UUID,
        deadline_monotonic: float,
    ) -> None:
        self._retire_playback_timeout()
        task = asyncio.create_task(
            self._post_playback_timeout(
                game_session_id,
                round_id,
                deadline_monotonic,
            ),
            name="wst-playback-timeout",
        )
        self._playback_timeout_task = task
        self._owned_timer_tasks.add(task)
        task.add_done_callback(self._timer_task_done)

    async def _post_playback_timeout(
        self,
        game_session_id: UUID,
        round_id: UUID,
        deadline_monotonic: float,
    ) -> None:
        remaining_seconds = max(
            0.0,
            deadline_monotonic - self._game_engine.monotonic_time(),
        )
        try:
            await self._playback_timeout_sleep(remaining_seconds)
        except asyncio.CancelledError:
            return
        try:
            self.post_system_event(
                PlaybackTimeoutEvent(
                    game_session_id=game_session_id,
                    round_id=round_id,
                )
            )
        except (ControllerNotRunningError, ControllerFailedError):
            # Shutdown/failure retires owned timers; a racing expiry is harmless.
            return

    def _retire_playback_timeout(self) -> None:
        task = self._playback_timeout_task
        self._playback_timeout_task = None
        if task is not None and not task.done():
            task.cancel()

    def _schedule_songmaster_reconnect_grace(
        self,
        game_session_id: UUID,
        recovery: SongmasterRecovery,
    ) -> None:
        deadline = recovery.grace_deadline_monotonic
        if (
            recovery.status is not SongmasterRecoveryStatus.RECONNECT_GRACE
            or deadline is None
        ):
            raise DomainInvariantError(
                "a reconnect-grace timer requires authoritative deadline facts"
            )
        self._retire_songmaster_reconnect_grace()
        task = asyncio.create_task(
            self._post_songmaster_reconnect_grace_expiry(
                game_session_id,
                recovery.recovery_id,
                deadline,
            ),
            name="wst-songmaster-reconnect-grace",
        )
        self._songmaster_reconnect_grace_task = task
        self._owned_timer_tasks.add(task)
        task.add_done_callback(self._timer_task_done)

    async def _post_songmaster_reconnect_grace_expiry(
        self,
        game_session_id: UUID,
        recovery_id: UUID,
        deadline_monotonic: float,
    ) -> None:
        remaining_seconds = max(
            0.0,
            deadline_monotonic - self._game_engine.monotonic_time(),
        )
        try:
            await self._songmaster_reconnect_grace_sleep(remaining_seconds)
        except asyncio.CancelledError:
            return
        try:
            self.post_system_event(
                SongmasterReconnectGraceExpiredEvent(
                    game_session_id=game_session_id,
                    recovery_id=recovery_id,
                )
            )
        except (ControllerNotRunningError, ControllerFailedError):
            return

    def _retire_songmaster_reconnect_grace(self) -> None:
        task = self._songmaster_reconnect_grace_task
        self._songmaster_reconnect_grace_task = None
        if task is not None and not task.done():
            task.cancel()

    def _timer_task_done(self, task: asyncio.Task[None]) -> None:
        self._owned_timer_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            task.get_loop().call_exception_handler(
                {
                    "message": "WST owned timer task failed",
                    "exception": error,
                    "task": task,
                }
            )

    async def _await_timer_cleanup(self) -> None:
        tasks = tuple(self._owned_timer_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    def _is_expected_operational_error(error: Exception) -> bool:
        if isinstance(error, DomainInvariantError):
            return False
        return isinstance(
            error,
            (GameEngineError, InvalidPlayerNameError, *_EXPECTED_APPLICATION_ERRORS),
        )

    def _complete_expected_rejection(
        self,
        queued: _QueuedMessage,
        error: Exception,
    ) -> None:
        if queued.response is not None:
            self._complete_response(queued.response, error=error)
            return
        if not isinstance(queued.message, SystemEvent):  # pragma: no cover
            raise TypeError("a response-free message must be a SystemEvent")
        self._system_event_rejections.append(
            SystemEventRejection(queued.message, error)
        )

    def _fail_controller(
        self,
        queued: _QueuedMessage,
        error: Exception,
    ) -> None:
        self._failure = error
        self._accepting_messages = False
        self._retire_preparation_timeout()
        self._retire_playback_timeout()
        self._retire_songmaster_reconnect_grace()
        if queued.response is not None:
            self._complete_response(queued.response, error=error)

        while True:
            try:
                pending = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            try:
                if isinstance(pending, _QueuedMessage) and pending.response is not None:
                    self._complete_response(
                        pending.response,
                        error=ControllerFailedError(error),
                    )
            finally:
                self._queue.task_done()

    @staticmethod
    def _complete_response(
        response: asyncio.Future[Any] | None,
        *,
        result: object | None = None,
        error: Exception | None = None,
    ) -> None:
        if response is None or response.done():
            return
        if error is not None:
            response.set_exception(error)
        else:
            response.set_result(result)


def _observe_response_exception(response: asyncio.Future[Any]) -> None:
    """Consume exception bookkeeping even when the submitting task was canceled."""

    if not response.cancelled():
        response.exception()


__all__ = [
    "DEFAULT_PREPARATION_TIMEOUT_SECONDS",
    "DEFAULT_SONGMASTER_RECONNECT_GRACE_SECONDS",
    "ConsumerAlreadyRunningError",
    "ConsumerSelfRequestError",
    "ControllerFailedError",
    "ControllerNotRunningError",
    "GameController",
    "GameControllerError",
    "StaleConnectionError",
    "StaleGameSessionError",
    "SystemEventRejection",
    "UnsupportedMessageError",
]
