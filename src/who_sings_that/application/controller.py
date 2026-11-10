"""Serialized application-level ownership of authoritative game work.

The controller deliberately executes synchronous domain and persistence work on
its asyncio event-loop thread.  That keeps each message one indivisible unit of
authoritative work for this phase, at the cost of blocking unrelated loop work
while persistent playlist and media inspection is in progress.
"""

import asyncio
from dataclasses import dataclass
from os import PathLike
from typing import Any, Final

from sqlalchemy import Engine

from who_sings_that.catalog import extract_track_metadata
from who_sings_that.domain import (
    DomainInvariantError,
    GameEngine,
    GameEngineError,
    InvalidPlayerNameError,
)

from .messages import (
    AuthoritativeMessage,
    BrowserAudioCapabilityChangedEvent,
    ClientRequest,
    CreateGameRequest,
    DeselectPlaylistRequest,
    JoinGameRequest,
    MemberDisconnectedEvent,
    PlaySongRequest,
    SelectPlaylistRequest,
    SetAudioPreferenceRequest,
    StartGameRequest,
    SystemEvent,
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


@dataclass(slots=True)
class _QueuedMessage:
    message: AuthoritativeMessage
    response: asyncio.Future[Any] | None


class _StopConsumer:
    pass


_STOP_CONSUMER: Final = _StopConsumer()
_EXPECTED_APPLICATION_ERRORS: Final = (
    InvalidMusicRootError,
    InvalidSelectedPlaylistIdsError,
    NoStructurallyEligibleTracksError,
    SelectedPlaylistNotFoundError,
    StaleGameSessionError,
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
    ) -> None:
        self._game_engine = game_engine if game_engine is not None else GameEngine()
        self._database_engine = database_engine
        self._music_root = music_root
        self._inspect_media = inspect_media
        self._queue: asyncio.Queue[_QueuedMessage | _StopConsumer] = asyncio.Queue()
        self._consumer_task: asyncio.Task[None] | None = None
        self._consumer_ready: asyncio.Event | None = None
        self._accepting_messages = False
        self._failure: BaseException | None = None
        self._system_event_rejections: list[SystemEventRejection] = []

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
            self._queue.put_nowait(_STOP_CONSUMER)
        await task
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
        self._queue.put_nowait(_QueuedMessage(request, response))
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

    def _dispatch(self, message: AuthoritativeMessage) -> object:
        """Route one message to existing application or domain operations."""

        if isinstance(message, CreateGameRequest):
            return self._game_engine.create_game(
                message.browser_token,
                message.display_name,
            )
        if isinstance(message, JoinGameRequest):
            self._require_target_game(message.game_session_id)
            return self._game_engine.join_game(
                message.browser_token,
                message.display_name,
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
        if isinstance(message, PlaySongRequest):
            self._require_target_game(message.game_session_id)
            return self._game_engine.play_song(message.requesting_browser_token)
        if isinstance(message, MemberDisconnectedEvent):
            self._require_target_game(message.game_session_id)
            return self._game_engine.disconnect_member(message.browser_token)
        if isinstance(message, BrowserAudioCapabilityChangedEvent):
            self._require_target_game(message.game_session_id)
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
    "ConsumerAlreadyRunningError",
    "ConsumerSelfRequestError",
    "ControllerFailedError",
    "ControllerNotRunningError",
    "GameController",
    "GameControllerError",
    "StaleGameSessionError",
    "SystemEventRejection",
    "UnsupportedMessageError",
]
