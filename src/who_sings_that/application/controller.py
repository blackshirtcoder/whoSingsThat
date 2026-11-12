"""Serialized application-level ownership of authoritative game work.

The controller deliberately executes synchronous domain and persistence work on
its asyncio event-loop thread.  That keeps each message one indivisible unit of
authoritative work for this phase, at the cost of blocking unrelated loop work
while persistent playlist and media inspection is in progress.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite
from numbers import Real
from os import PathLike
from typing import Any, Final
from uuid import UUID

from sqlalchemy import Engine

from who_sings_that.catalog import extract_track_metadata
from who_sings_that.database import (
    CompletedGameHistoryError,
    save_completed_game_history,
)
from who_sings_that.domain import (
    ActiveRound,
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
    SongmasterTransferNotAllowedError,
    UnknownGameMemberError,
)

from .audio import (
    AudioControlCommand,
    AudioStopReason,
    MediaCapability,
    MediaCapabilityStore,
    PlayAudioCommand,
    PrepareAudioCommand,
    StopAudioCommand,
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
    CurrentConnectionRequest,
    DeselectPlaylistRequest,
    EndGameRequest,
    JoinGameRequest,
    MemberDisconnectedEvent,
    PlaybackTimeoutEvent,
    PlaySongRequest,
    RegisterConnectionRequest,
    RetryCompletedGamePersistenceRequest,
    RevealRequest,
    SelectPlaylistRequest,
    SelectProvisionalScoreRequest,
    SetAudioPreferenceRequest,
    SongmasterReconnectGraceExpiredEvent,
    StartGameRequest,
    SystemEvent,
    TransferSongmasterByPublicIdRequest,
    TransferSongmasterRequest,
)
from .projections import (
    AuthoritativeProjectionUpdate,
    CapturedRecipientProjection,
    GameEventNotification,
    MembershipSynchronization,
    PublicGameProjection,
    build_authoritative_projection,
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


class CompletedGamePersistenceNotAllowedError(GameControllerError):
    """A history retry lacks a retained finished game or Songmaster authority."""


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
    projection_browser_token: str | None = None


@dataclass(frozen=True, slots=True)
class _AudioContext:
    game_session_id: UUID | None
    state: GameState
    pending: PendingAudioPreparation | None
    active_round: ActiveRound | None
    connections: dict[str, str]


class _StopConsumer:
    pass


_STOP_CONSUMER: Final = _StopConsumer()
DEFAULT_PREPARATION_TIMEOUT_SECONDS: Final = 5.0
DEFAULT_SONGMASTER_RECONNECT_GRACE_SECONDS: Final = 10.0
DEFAULT_MEDIA_CAPABILITY_LIFETIME_SECONDS: Final = 120.0
_EXPECTED_APPLICATION_ERRORS: Final = (
    CompletedGameHistoryError,
    CompletedGamePersistenceNotAllowedError,
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
        media_capability_lifetime_seconds: float = (
            DEFAULT_MEDIA_CAPABILITY_LIFETIME_SECONDS
        ),
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
        if (
            isinstance(media_capability_lifetime_seconds, bool)
            or not isinstance(media_capability_lifetime_seconds, Real)
            or not isfinite(media_capability_lifetime_seconds)
            or media_capability_lifetime_seconds <= 0
        ):
            raise ValueError(
                "media_capability_lifetime_seconds must be a positive finite number"
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
        self._persisted_completed_game_ids: set[UUID] = set()
        self._preparation_timeout_task: asyncio.Task[None] | None = None
        self._playback_timeout_task: asyncio.Task[None] | None = None
        self._songmaster_reconnect_grace_task: asyncio.Task[None] | None = None
        self._owned_timer_tasks: set[asyncio.Task[None]] = set()
        self._projection_revision = 0
        self._latest_projection: AuthoritativeProjectionUpdate | None = None
        self._last_invalidated_projection: AuthoritativeProjectionUpdate | None = None
        self._projection_listeners: list[
            Callable[[AuthoritativeProjectionUpdate], None]
        ] = []
        self._recovery_deadlines_utc: dict[UUID, datetime] = {}
        self._media_capabilities = MediaCapabilityStore(
            float(media_capability_lifetime_seconds),
            self._game_engine.monotonic_time,
        )
        self._audio_control_listeners: list[Callable[[AudioControlCommand], None]] = []

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
    def media_capabilities(self) -> MediaCapabilityStore:
        """Return the private capability store used by the HTTP adapter."""

        return self._media_capabilities

    def authorize_media_capability(self, opaque_token: str) -> MediaCapability:
        """Validate one bearer capability against expiry and live connection."""

        record = self._media_capabilities.authorize(opaque_token)
        if not self._connection_registry.is_current(
            record.game_session_id,
            record.browser_token,
            record.connection_id,
        ):
            self._media_capabilities.revoke_recipient(
                record.game_session_id,
                record.browser_token,
                record.connection_id,
            )
            return self._media_capabilities.authorize(opaque_token)
        return record

    def add_audio_control_listener(
        self,
        listener: Callable[[AudioControlCommand], None],
    ) -> None:
        if listener not in self._audio_control_listeners:
            self._audio_control_listeners.append(listener)

    def remove_audio_control_listener(
        self,
        listener: Callable[[AudioControlCommand], None],
    ) -> None:
        if listener in self._audio_control_listeners:
            self._audio_control_listeners.remove(listener)

    @property
    def latest_projection(self) -> AuthoritativeProjectionUpdate | None:
        """Return the last immutable FIFO-boundary capture, if a game exists."""

        return self._latest_projection

    def projection_for(
        self,
        browser_token: str,
    ) -> tuple[AuthoritativeProjectionUpdate, CapturedRecipientProjection]:
        """Return the latest already-captured view for one current member."""

        update = self._latest_projection
        recipient = (
            None if update is None else update.recipient_for_token(browser_token)
        )
        if update is None or recipient is None:
            raise UnknownGameMemberError(
                "no authoritative projection exists for this member"
            )
        return update, recipient

    def projection_for_game_member(
        self,
        game_session_id: UUID,
        browser_token: str,
    ) -> tuple[AuthoritativeProjectionUpdate, CapturedRecipientProjection]:
        """Return the newest retained capture for one game/member pair."""

        candidates = (self._latest_projection, self._last_invalidated_projection)
        for update in candidates:
            if update is None or update.game_session_id != game_session_id:
                continue
            recipient = update.recipient_for_token(browser_token)
            if recipient is not None:
                return update, recipient
        raise UnknownGameMemberError(
            "no retained projection exists for this game member"
        )

    def add_projection_listener(
        self,
        listener: Callable[[AuthoritativeProjectionUpdate], None],
    ) -> None:
        """Register a synchronous, non-network projection enqueue callback."""

        if listener not in self._projection_listeners:
            self._projection_listeners.append(listener)

    def remove_projection_listener(
        self,
        listener: Callable[[AuthoritativeProjectionUpdate], None],
    ) -> None:
        """Stop routing future captures to a transport adapter."""

        if listener in self._projection_listeners:
            self._projection_listeners.remove(listener)

    def is_completed_history_persisted(self, game_session_id: UUID) -> bool:
        """Return whether this controller observed a successful archive commit."""

        return game_session_id in self._persisted_completed_game_ids

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
        self._media_capabilities.revoke_all()
        self._consumer_task = None
        self._consumer_ready = None
        if self._failure is not None:
            raise ControllerFailedError(self._failure) from self._failure

    async def submit_request(self, request: ClientRequest) -> Any:
        """Queue a client request and await its terminal result or rejection.

        Shielding the private response future means cancellation of the waiting
        caller does not cancel authoritative work that the controller accepted.
        """

        return await self._submit_request(request, projection_browser_token=None)

    async def submit_membership_request(
        self,
        request: CreateGameRequest | JoinGameRequest | RegisterConnectionRequest,
    ) -> MembershipSynchronization:
        """Return membership result with its exact FIFO-boundary projection."""

        if not isinstance(
            request,
            (CreateGameRequest, JoinGameRequest, RegisterConnectionRequest),
        ):
            raise TypeError("request must establish game membership")
        return await self._submit_request(
            request,
            projection_browser_token=request.browser_token,
        )

    async def submit_projected_request(
        self,
        request: ClientRequest,
        browser_token: str,
    ) -> MembershipSynchronization:
        """Return a client result with its exact recipient-safe state capture."""

        if not isinstance(browser_token, str) or not browser_token:
            raise ValueError("browser_token must be a non-empty string")
        return await self._submit_request(
            request,
            projection_browser_token=browser_token,
        )

    async def _submit_request(
        self,
        request: ClientRequest,
        *,
        projection_browser_token: str | None,
    ) -> Any:
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
        self._queue.put_nowait(
            _QueuedMessage(
                queued_request,
                response,
                projection_browser_token=projection_browser_token,
            )
        )
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
                prior_projection = self._latest_projection
                prior_audio = self._capture_audio_context()
                try:
                    result = self._dispatch(queued.message)
                except Exception as error:  # noqa: BLE001 - consumer failure boundary
                    if self._is_expected_operational_error(error):
                        try:
                            self._complete_audio_effects(
                                queued.message,
                                None,
                                prior_audio,
                            )
                            self._capture_projection_change(prior_projection)
                        except Exception as projection_error:  # noqa: BLE001
                            self._fail_controller(queued, projection_error)
                            return
                        self._complete_expected_rejection(queued, error)
                        continue
                    self._fail_controller(queued, error)
                    return
                try:
                    self._complete_audio_effects(
                        queued.message,
                        result,
                        prior_audio,
                    )
                    self._capture_projection_change(prior_projection)
                    response_result: object = result
                    if queued.projection_browser_token is not None:
                        update, recipient = self.projection_for(
                            queued.projection_browser_token
                        )
                        response_result = MembershipSynchronization(
                            result=result,
                            update=update,
                            recipient=recipient,
                        )
                except Exception as error:  # noqa: BLE001 - capture failure boundary
                    self._fail_controller(queued, error)
                    return
                self._complete_response(queued.response, result=response_result)
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

        if isinstance(message, CurrentConnectionRequest):
            game_session_id, browser_token = _connection_bound_request_scope(
                message.request
            )
            self._require_current_connection(
                game_session_id,
                browser_token,
                message.source_connection_id,
            )
            return self._dispatch(message.request)

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
        if isinstance(message, TransferSongmasterByPublicIdRequest):
            self._require_target_game(message.game_session_id)
            game = self._game_engine.active_game
            if game is None:  # pragma: no cover - target validation established it
                raise DomainInvariantError("Songmaster transfer requires a game")
            recipient = next(
                (
                    member
                    for member in game.members.values()
                    if member.public_participant_id
                    == message.recipient_public_participant_id
                ),
                None,
            )
            if recipient is None:
                raise SongmasterTransferNotAllowedError(
                    "the public participant ID is not a member of this game"
                )
            return self._game_engine.transfer_songmaster(
                message.requesting_browser_token,
                recipient.browser_token,
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
            else:
                self._persist_finished_history(
                    message.requesting_browser_token
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
            self._persist_finished_history(message.requesting_browser_token)
            return result
        if isinstance(message, RetryCompletedGamePersistenceRequest):
            self._require_target_game(message.game_session_id)
            return self._persist_finished_history(
                message.requesting_browser_token
            )
        if isinstance(message, CloseGameRequest):
            self._require_target_game(message.game_session_id)
            if self._game_engine.state is not GameState.FINISHED:
                # Preserve the domain's established state-specific rejection.
                return self._game_engine.close_game(
                    message.game_session_id,
                    message.requesting_browser_token,
                )
            # A game without a known successful commit retries immediately
            # before teardown. A known success needs no second database access.
            if message.game_session_id not in self._persisted_completed_game_ids:
                self._persist_finished_history(message.requesting_browser_token)
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
        if isinstance(message, SelectProvisionalScoreRequest):
            return self._game_engine.select_provisional_score(
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

    def _capture_audio_context(self) -> _AudioContext:
        game = self._game_engine.active_game
        if game is None:
            return _AudioContext(None, self._game_engine.state, None, None, {})
        connections = {
            token: connection_id
            for token in game.members
            if (
                connection_id := self._connection_registry.current_connection_id(
                    game.game_session_id,
                    token,
                )
            )
            is not None
        }
        return _AudioContext(
            game.game_session_id,
            self._game_engine.state,
            game.pending_audio_preparation,
            game.active_round,
            connections,
        )

    def _complete_audio_effects(
        self,
        submitted: object,
        result: object,
        prior: _AudioContext,
    ) -> None:
        """Publish narrow controls from the just-committed FIFO transition."""

        message = submitted
        if isinstance(
            message,
            (
                CurrentConnectionRequest,
                _TrustedBuzzSubmission,
                _TrustedConnectionSubmission,
            ),
        ):
            message = message.request

        game = self._game_engine.active_game

        if isinstance(result, PendingAudioPreparation):
            if game is None:
                raise DomainInvariantError("audio preparation requires an active game")
            for browser_token in sorted(result.audio_target_browser_tokens):
                connection_id = self._connection_registry.current_connection_id(
                    game.game_session_id,
                    browser_token,
                )
                if connection_id is None:
                    raise DomainInvariantError(
                        "an audio preparation target requires a current connection"
                    )
                capability = self._media_capabilities.issue(
                    game_session_id=game.game_session_id,
                    preparation_id=result.preparation_id,
                    browser_token=browser_token,
                    connection_id=connection_id,
                    track_id=result.deck_entry.track.track_id,
                )
                self._publish_audio_control(
                    PrepareAudioCommand(
                        game_session_id=game.game_session_id,
                        preparation_id=result.preparation_id,
                        browser_token=browser_token,
                        connection_id=connection_id,
                        media_url=f"/media/{capability.opaque_token}",
                        effective_playback_offset_seconds=(
                            result.effective_playback_offset_seconds
                        ),
                        playback_duration_seconds=result.playback_duration_seconds,
                        preparation_timeout_seconds=self._preparation_timeout_seconds,
                    )
                )

        if isinstance(message, AudioPreparationFailedEvent):
            self._media_capabilities.revoke_recipient(
                message.game_session_id,
                message.browser_token,
                message.source_connection_id,
            )

        if isinstance(result, PreparationEventResult):
            if result.outcome is PreparationOutcome.PLAYING:
                active_round = result.active_round
                if game is None or active_round is None:
                    raise DomainInvariantError(
                        "PLAYING preparation effects require an active round"
                    )
                self._media_capabilities.promote_ready(
                    result.preparation_id,
                    result.ready_browser_tokens,
                    active_round.round_id,
                )
                if prior.pending is not None:
                    self._stop_preparation_targets(
                        prior,
                        AudioStopReason.PREPARATION_FAILED,
                        browser_tokens=(
                            prior.pending.audio_target_browser_tokens
                            - result.ready_browser_tokens
                        ),
                    )
                for browser_token in sorted(active_round.playback_clients):
                    connection_id = self._connection_registry.current_connection_id(
                        game.game_session_id,
                        browser_token,
                    )
                    if connection_id is None:
                        raise DomainInvariantError(
                            "a playback client requires a current connection"
                        )
                    self._publish_audio_control(
                        PlayAudioCommand(
                            game_session_id=game.game_session_id,
                            preparation_id=active_round.preparation_id,
                            round_id=active_round.round_id,
                            browser_token=browser_token,
                            connection_id=connection_id,
                            effective_playback_offset_seconds=(
                                active_round.effective_playback_offset_seconds
                            ),
                            playback_duration_seconds=(
                                active_round.playback_duration_seconds
                            ),
                            authoritative_playback_started_at=(
                                active_round.playback_started_at
                            ),
                        )
                    )
            elif result.outcome is PreparationOutcome.FAILURE_PENDING_ACKNOWLEDGEMENT:
                self._media_capabilities.revoke_preparation(result.preparation_id)
                self._stop_preparation_targets(
                    prior,
                    AudioStopReason.PREPARATION_FAILED,
                )

        current_active = game.active_round if game is not None else None
        if (
            prior.state is GameState.PLAYING
            and prior.active_round is not None
            and (
                current_active is None
                or current_active.round_id != prior.active_round.round_id
                or self._game_engine.state is not GameState.PLAYING
            )
        ):
            reason = self._audio_stop_reason(message, game)
            self._stop_playback_targets(prior, reason)
            self._media_capabilities.revoke_preparation(
                prior.active_round.preparation_id
            )

        current_pending = game.pending_audio_preparation if game is not None else None
        if (
            prior.state is GameState.AUDIO_PREPARING
            and prior.pending is not None
            and not isinstance(result, PreparationEventResult)
            and not isinstance(
                message,
                AcknowledgeAudioPreparationFailureRequest,
            )
            and (
                current_pending is None
                or current_pending.preparation_id != prior.pending.preparation_id
            )
        ):
            self._stop_preparation_targets(
                prior,
                self._audio_stop_reason(message, game),
            )
            self._media_capabilities.revoke_preparation(prior.pending.preparation_id)

        if isinstance(message, (CancelGameRequest, CloseGameRequest, EndGameRequest)):
            game_id = getattr(message, "game_session_id", prior.game_session_id)
            if game_id is not None:
                self._media_capabilities.revoke_game(game_id)

        self._media_capabilities.reconcile_connections(
            self._connection_registry.is_current
        )

    def _stop_preparation_targets(
        self,
        prior: _AudioContext,
        reason: AudioStopReason,
        *,
        browser_tokens: frozenset[str] | None = None,
    ) -> None:
        pending = prior.pending
        if pending is None or prior.game_session_id is None:
            return
        targets = (
            pending.audio_target_browser_tokens
            if browser_tokens is None
            else browser_tokens
        )
        for browser_token in sorted(targets):
            connection_id = prior.connections.get(browser_token)
            if connection_id is None:
                continue
            self._publish_audio_control(
                StopAudioCommand(
                    game_session_id=prior.game_session_id,
                    preparation_id=pending.preparation_id,
                    round_id=None,
                    browser_token=browser_token,
                    connection_id=connection_id,
                    reason=reason,
                )
            )

    def _stop_playback_targets(
        self,
        prior: _AudioContext,
        reason: AudioStopReason,
    ) -> None:
        active_round = prior.active_round
        if active_round is None or prior.game_session_id is None:
            return
        for browser_token in sorted(active_round.playback_clients):
            connection_id = prior.connections.get(browser_token)
            if connection_id is None:
                continue
            self._publish_audio_control(
                StopAudioCommand(
                    game_session_id=prior.game_session_id,
                    preparation_id=active_round.preparation_id,
                    round_id=active_round.round_id,
                    browser_token=browser_token,
                    connection_id=connection_id,
                    reason=reason,
                )
            )

    @staticmethod
    def _audio_stop_reason(message: object, game: object) -> AudioStopReason:
        if isinstance(message, BuzzRequest):
            return AudioStopReason.ACCEPTED_BUZZ
        if isinstance(message, PlaybackTimeoutEvent):
            return AudioStopReason.PLAYBACK_TIMEOUT
        if isinstance(message, EndGameRequest):
            return AudioStopReason.GAME_TERMINATION
        if isinstance(
            message,
            (MemberDisconnectedEvent, RegisterConnectionRequest, JoinGameRequest),
        ):
            recovery = getattr(game, "songmaster_recovery", None)
            voided = getattr(game, "last_voided_round", None)
            if recovery is not None or (
                voided is not None and voided.reason.value == "songmaster_disconnected"
            ):
                return AudioStopReason.SONGMASTER_LOSS
            if voided is not None and voided.reason.value == "playback_client_lost":
                return AudioStopReason.PLAYBACK_CLIENT_LOSS
            return AudioStopReason.CONNECTION_REPLACED
        return AudioStopReason.ROUND_VOID

    def _publish_audio_control(self, command: AudioControlCommand) -> None:
        for listener in tuple(self._audio_control_listeners):
            listener(command)

    def _capture_projection_change(
        self,
        prior: AuthoritativeProjectionUpdate | None,
    ) -> None:
        """Capture and publish one coherent post-dispatch observable state."""

        game = self._game_engine.active_game
        captured_at = datetime.now(UTC)
        if game is None:
            if prior is None or prior.invalidated:
                return
            update = AuthoritativeProjectionUpdate(
                game_session_id=prior.game_session_id,
                revision=prior.revision + 1,
                captured_at=captured_at,
                public=None,
                recipients=prior.recipients,
                notifications=(
                    GameEventNotification("game_closed", {}),
                ),
                invalidated=True,
            )
            self._projection_revision = update.revision
            self._latest_projection = update
            self._last_invalidated_projection = update
            self._publish_projection(update)
            return

        if prior is None or prior.game_session_id != game.game_session_id:
            self._projection_revision = 0

        recovery_deadline = self._recovery_deadline_for_projection(captured_at)
        public, recipients = build_authoritative_projection(
            game,
            self._game_engine.state,
            self._connection_registry,
            recovery_deadline_utc=recovery_deadline,
        )
        comparable_prior = (
            prior
            if prior is not None
            and prior.game_session_id == game.game_session_id
            and not prior.invalidated
            else None
        )
        if (
            comparable_prior is not None
            and comparable_prior.public == public
            and comparable_prior.recipients == recipients
        ):
            return
        self._projection_revision += 1
        notifications = _derive_notifications(
            None if comparable_prior is None else comparable_prior.public,
            public,
        )
        update = AuthoritativeProjectionUpdate(
            game_session_id=game.game_session_id,
            revision=self._projection_revision,
            captured_at=captured_at,
            public=public,
            recipients=recipients,
            notifications=notifications,
        )
        self._latest_projection = update
        self._publish_projection(update)

    def _recovery_deadline_for_projection(
        self,
        captured_at: datetime,
    ) -> datetime | None:
        game = self._game_engine.active_game
        recovery = game.songmaster_recovery if game is not None else None
        if (
            recovery is None
            or recovery.status is not SongmasterRecoveryStatus.RECONNECT_GRACE
            or recovery.grace_deadline_monotonic is None
        ):
            self._recovery_deadlines_utc.clear()
            return None
        self._recovery_deadlines_utc = {
            key: value
            for key, value in self._recovery_deadlines_utc.items()
            if key == recovery.recovery_id
        }
        existing = self._recovery_deadlines_utc.get(recovery.recovery_id)
        if existing is not None:
            return existing
        remaining = max(
            0.0,
            recovery.grace_deadline_monotonic
            - self._game_engine.monotonic_time(),
        )
        deadline = captured_at + timedelta(seconds=remaining)
        self._recovery_deadlines_utc[recovery.recovery_id] = deadline
        return deadline

    def _publish_projection(self, update: AuthoritativeProjectionUpdate) -> None:
        for listener in tuple(self._projection_listeners):
            listener(update)

    def _persist_finished_history(
        self,
        requesting_browser_token: str,
    ) -> GameFinalizationResult:
        """Commit the retained FINISHED snapshot in the current FIFO handler."""

        game = self._game_engine.active_game
        if self._game_engine.state is not GameState.FINISHED or game is None:
            raise CompletedGamePersistenceNotAllowedError(
                "completed history persistence requires a retained FINISHED game"
            )
        if (
            requesting_browser_token != game.songmaster_browser_token
            or not game.songmaster.connected
        ):
            raise CompletedGamePersistenceNotAllowedError(
                "only the current connected Songmaster may retry persistence"
            )
        history = game.completed_game_history
        if history is None:
            raise DomainInvariantError(
                "FINISHED requires an authoritative completed history snapshot"
            )
        save_completed_game_history(self._database_engine, history)
        self._persisted_completed_game_ids.add(game.game_session_id)
        if game.end_reason is None:  # pragma: no cover - FINISHED invariant
            raise DomainInvariantError("FINISHED requires an end reason")
        return GameFinalizationResult(
            end_reason=game.end_reason,
            final_standings=game.final_standings,
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
        game = self._game_engine.active_game
        if game is None:
            # Let the domain retain authority over the public lifecycle error.
            return self._game_engine.join_game(
                message.browser_token,
                message.display_name,
            )
        game_session_id = game.game_session_id
        status = self._connection_registry.registration_status(
            game_session_id,
            message.browser_token,
            message.source_connection_id,
        )
        prior_state = self._game_engine.state
        handled = self._prepare_songmaster_connection(
            game_session_id,
            message.browser_token,
            status,
            received_at_monotonic,
        )
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
            game_session_id,
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
        self._require_target_game(message.game_session_id)
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
        try:
            while (
                remaining_seconds := (
                    deadline_monotonic - self._game_engine.monotonic_time()
                )
            ) > 0.0:
                # Event-loop timers may fire up to one clock-resolution early.
                # Recheck the authoritative clock so an early wake cannot post
                # the one-shot event before the domain will accept it.
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
        self._media_capabilities.revoke_all()
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
        self._media_capabilities.revoke_all()
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


def _connection_bound_request_scope(request: ClientRequest) -> tuple[UUID, str]:
    """Return the explicit membership scope for a transport-wrapped request."""

    if isinstance(
        request,
        (
            AcceptScoreRequest,
            AcknowledgeAudioPreparationFailureRequest,
            CancelGameRequest,
            CloseGameRequest,
            CorrectScoreRequest,
            DeselectPlaylistRequest,
            EndGameRequest,
            PlaySongRequest,
            RetryCompletedGamePersistenceRequest,
            RevealRequest,
            SelectPlaylistRequest,
            SelectProvisionalScoreRequest,
            SetAudioPreferenceRequest,
            StartGameRequest,
            TransferSongmasterRequest,
            TransferSongmasterByPublicIdRequest,
        ),
    ):
        return request.game_session_id, request.requesting_browser_token
    raise UnsupportedMessageError(
        f"{type(request).__name__} cannot be wrapped as a current-connection request"
    )


def _derive_notifications(
    previous: PublicGameProjection | None,
    current: PublicGameProjection,
) -> tuple[GameEventNotification, ...]:
    """Derive bounded transient notices only from authoritative fact changes."""

    if previous is None:
        return (GameEventNotification("game_created", {}),)
    notifications: list[GameEventNotification] = []
    old_participants = {
        item.public_participant_id: item for item in previous.participants
    }
    new_participants = {
        item.public_participant_id: item for item in current.participants
    }
    for participant_id, participant in new_participants.items():
        old = old_participants.get(participant_id)
        identity = {
            "public_participant_id": str(participant_id),
            "display_name": participant.display_name,
        }
        if old is None:
            notifications.append(GameEventNotification("player_joined", identity))
        elif old.connected and not participant.connected:
            notifications.append(
                GameEventNotification("player_disconnected", identity)
            )
        elif not old.connected and participant.connected:
            notifications.append(GameEventNotification("player_reconnected", identity))

    old_recovery = previous.recovery
    new_recovery = current.recovery
    if old_recovery is None and new_recovery is not None:
        notifications.append(
            GameEventNotification(
                "songmaster_reconnect_grace_began",
                {
                    "recovery_id": new_recovery["recovery_id"],
                    "grace_deadline": new_recovery["grace_deadline"],
                    "round_voided": new_recovery["round_voided"],
                },
            )
        )
    elif (
        old_recovery is not None
        and new_recovery is not None
        and old_recovery["status"] != new_recovery["status"]
    ):
        notifications.append(
            GameEventNotification(
                "songmaster_recovery_suspended",
                {"recovery_id": new_recovery["recovery_id"]},
            )
        )

    old_outcome = previous.last_recovery_outcome
    new_outcome = current.last_recovery_outcome
    if new_outcome is not None and (
        old_outcome is None
        or old_outcome["recovery_id"] != new_outcome["recovery_id"]
    ):
        resolution = new_outcome["resolution"]
        event = {
            "original_reconnected": "songmaster_recovered",
            "automatic_failover": "songmaster_automatically_changed",
            "resumed_after_indefinite_suspension": "songmaster_recovery_resumed",
        }[resolution]
        notifications.append(GameEventNotification(event, dict(new_outcome)))

    old_void = previous.round_status.get("last_voided_round")
    new_void = current.round_status.get("last_voided_round")
    if new_void is not None and new_void != old_void:
        notifications.append(GameEventNotification("round_voided", dict(new_void)))
        if new_void["reason"] == "playback_client_lost":
            notifications.append(
                GameEventNotification("playback_client_lost", dict(new_void))
            )

    old_preparation = previous.round_status.get("audio_preparation")
    new_preparation = current.round_status.get("audio_preparation")
    if (
        new_preparation is not None
        and new_preparation.get("failure_acknowledgement_pending") is True
        and (
            old_preparation is None
            or old_preparation.get("failure_acknowledgement_pending") is not True
        )
    ):
        notifications.append(GameEventNotification("audio_preparation_failed", {}))

    if previous.state is not GameState.FINISHED and current.state is GameState.FINISHED:
        notifications.append(
            GameEventNotification(
                "game_ended",
                {"end_reason": current.end_reason},
            )
        )
    return tuple(notifications)


__all__ = [
    "DEFAULT_PREPARATION_TIMEOUT_SECONDS",
    "DEFAULT_SONGMASTER_RECONNECT_GRACE_SECONDS",
    "CompletedGamePersistenceNotAllowedError",
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
