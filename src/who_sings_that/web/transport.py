"""FastAPI/Starlette WebSocket adapter for the authoritative controller."""

import asyncio
import json
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from secrets import token_urlsafe
from typing import Any, Final
from uuid import UUID, uuid4

from pydantic import ValidationError
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from who_sings_that.application import (
    AcceptScoreRequest,
    AcknowledgeAudioPreparationFailureRequest,
    AudioControlCommand,
    AudioPreparationFailedEvent,
    AudioPreparationReadyEvent,
    AuthoritativeProjectionUpdate,
    BrowserAudioCapabilityChangedEvent,
    BuzzRequest,
    CancelGameRequest,
    CapturedRecipientProjection,
    CloseGameRequest,
    ControllerFailedError,
    ControllerNotRunningError,
    CorrectScoreRequest,
    CreateGameRequest,
    CurrentConnectionRequest,
    DeselectPlaylistRequest,
    EndGameRequest,
    GameController,
    JoinGameRequest,
    MemberDisconnectedEvent,
    PlayAudioCommand,
    PlaySongRequest,
    PrepareAudioCommand,
    RegisterConnectionRequest,
    RetryCompletedGamePersistenceRequest,
    RevealRequest,
    SelectPlaylistRequest,
    SelectProvisionalScoreRequest,
    SetAudioPreferenceRequest,
    StartGameRequest,
    StopAudioCommand,
    TransferSongmasterByPublicIdRequest,
)
from who_sings_that.domain import (
    GameFinalizationResult,
    PendingAudioPreparation,
    ScoringChoice,
)

from .protocol import (
    PAYLOAD_MODELS,
    PROTOCOL_VERSION,
    CommandEnvelope,
    ProtocolModel,
    error_response,
    result_response,
)

LOGGER = logging.getLogger(__name__)
_REPLACED_CLOSE_CODE: Final = 4001
_SHUTDOWN_CLOSE_CODE: Final = 1012
_SLOW_CLIENT_CLOSE_CODE: Final = 1013
_MAX_TRANSIENT_EVENTS: Final = 32
_SEND_TIMEOUT_SECONDS: Final = 5.0


@dataclass(frozen=True, slots=True)
class _OutboundRecord:
    game_session_id: UUID | None
    message: dict[str, Any]


class _OutboundMailbox:
    """Bounded per-socket delivery with one coalesced state snapshot."""

    def __init__(self, max_events: int = _MAX_TRANSIENT_EVENTS) -> None:
        self._max_events = max_events
        self._state: _OutboundRecord | None = None
        self._state_after_snapshot: _OutboundRecord | None = None
        self._discovery: _OutboundRecord | None = None
        self._events: deque[_OutboundRecord] = deque()
        self._controls: deque[_OutboundRecord] = deque()
        self._wake = asyncio.Event()
        self._closed = False
        self._highest_revision_by_game: dict[UUID, int] = {}

    @property
    def backlog_size(self) -> int:
        return (
            (1 if self._state is not None else 0)
            + (1 if self._state_after_snapshot is not None else 0)
            + (1 if self._discovery is not None else 0)
            + len(self._events)
            + len(self._controls)
        )

    def put_state(
        self,
        message_type: str,
        update: AuthoritativeProjectionUpdate,
        recipient: CapturedRecipientProjection,
    ) -> None:
        if self._closed:
            return
        highest = self._highest_revision_by_game.get(update.game_session_id, 0)
        if update.revision < highest:
            return
        self._highest_revision_by_game[update.game_session_id] = update.revision
        record = _OutboundRecord(
            game_session_id=update.game_session_id,
            message={
                "version": PROTOCOL_VERSION,
                "request_id": None,
                "type": message_type,
                "payload": update.state_payload_for(recipient),
            },
        )
        if (
            self._state is not None
            and self._state.message["type"] == "state_snapshot"
            and message_type == "state_update"
        ):
            self._state_after_snapshot = record
        else:
            self._state = record
        self._wake.set()

    def put_discovery(self, update: AuthoritativeProjectionUpdate | None) -> None:
        if self._closed:
            return
        self._discovery = _OutboundRecord(
            game_session_id=None,
            message=_game_discovery_message(update),
        )
        self._wake.set()

    def clear_discovery(self) -> None:
        self._discovery = None

    def put_event(
        self,
        update: AuthoritativeProjectionUpdate,
        event_payload: dict[str, Any],
    ) -> bool:
        if self._closed:
            return True
        if len(self._events) >= self._max_events:
            return False
        self._events.append(
            _OutboundRecord(
                game_session_id=update.game_session_id,
                message={
                    "version": PROTOCOL_VERSION,
                    "request_id": None,
                    "type": "game_event",
                    "payload": event_payload,
                },
            )
        )
        self._wake.set()
        return True

    def put_control(self, command: AudioControlCommand) -> bool:
        if self._closed:
            return True
        if len(self._controls) >= self._max_events:
            return False
        self._controls.append(
            _OutboundRecord(
                game_session_id=command.game_session_id,
                message=_audio_control_message(command),
            )
        )
        self._wake.set()
        return True

    async def get(self) -> _OutboundRecord | None:
        while True:
            if self._closed:
                return None
            if self._controls:
                return self._controls.popleft()
            if self._state is not None:
                record = self._state
                self._state = self._state_after_snapshot
                self._state_after_snapshot = None
                return record
            if self._discovery is not None:
                record = self._discovery
                self._discovery = None
                return record
            if self._events:
                return self._events.popleft()
            self._wake.clear()
            if (
                self._controls
                or self._state is not None
                or self._discovery is not None
                or self._events
            ):
                continue
            await self._wake.wait()

    def close(self) -> None:
        self._closed = True
        self._state = None
        self._state_after_snapshot = None
        self._discovery = None
        self._events.clear()
        self._controls.clear()
        self._wake.set()


@dataclass(slots=True)
class TransportSession:
    connection_id: str
    websocket: WebSocket
    browser_token: str | None = None
    game_session_id: UUID | None = None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    mailbox: _OutboundMailbox = field(default_factory=_OutboundMailbox)
    sender_task: asyncio.Task[None] | None = None
    overload_task: asyncio.Task[None] | None = None


class TransportConnections:
    """Minimal physical-socket map; gameplay membership stays in the controller."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._sessions: dict[str, TransportSession] = {}
        self._current_memberships: dict[tuple[UUID, str], str] = {}
        self._accepting = True

    @property
    def accepting(self) -> bool:
        return self._accepting

    async def add(self, session: TransportSession) -> bool:
        async with self._lock:
            if not self._accepting:
                return False
            self._sessions[session.connection_id] = session
            return True

    async def bind_membership(
        self,
        session: TransportSession,
        game_id: UUID,
        refresh_projection: Callable[
            [], tuple[AuthoritativeProjectionUpdate, CapturedRecipientProjection]
        ],
    ) -> None:
        predecessor: TransportSession | None = None
        token = session.browser_token
        if token is None:  # pragma: no cover - guarded by command routing
            raise RuntimeError("membership binding requires browser identity")
        async with self._lock:
            update, recipient = refresh_projection()
            if (
                update.game_session_id != game_id
                or recipient.browser_token != token
                or recipient.connection_id != session.connection_id
            ):
                raise RuntimeError(
                    "membership synchronization does not match this socket"
                )
            session.mailbox.put_state("state_snapshot", update, recipient)
            session.mailbox.clear_discovery()
            if update.invalidated:
                return
            key = (game_id, token)
            predecessor_id = self._current_memberships.get(key)
            session.game_session_id = game_id
            self._current_memberships[key] = session.connection_id
            if predecessor_id not in {None, session.connection_id}:
                predecessor = self._sessions.get(predecessor_id)
        if predecessor is not None:
            await _close_socket(predecessor.websocket, _REPLACED_CLOSE_CODE)

    def publish(
        self,
        update: AuthoritativeProjectionUpdate,
    ) -> tuple[TransportSession, ...]:
        """Enqueue one capture without awaiting a socket or acquiring a lock."""

        if not self._accepting:
            return ()
        overloaded: list[TransportSession] = []
        for recipient in update.recipients:
            key = (update.game_session_id, recipient.browser_token)
            connection_id = self._current_memberships.get(key)
            if connection_id != recipient.connection_id:
                continue
            session = self._sessions.get(connection_id)
            if session is None or session.game_session_id != update.game_session_id:
                continue
            session.mailbox.put_state("state_update", update, recipient)
            for notification in update.notifications:
                if not session.mailbox.put_event(
                    update,
                    notification.to_payload(
                        update.game_session_id,
                        update.revision,
                    ),
                ):
                    overloaded.append(session)
                    break
        return tuple(overloaded)

    def publish_discovery(
        self,
        update: AuthoritativeProjectionUpdate | None,
    ) -> None:
        """Coalesce public status for identity-established nonmembers."""

        if not self._accepting:
            return
        for session in self._sessions.values():
            if session.browser_token is not None and session.game_session_id is None:
                session.mailbox.put_discovery(update)

    def publish_discovery_to(
        self,
        session: TransportSession,
        update: AuthoritativeProjectionUpdate | None,
    ) -> None:
        if (
            self._accepting
            and session.browser_token is not None
            and session.game_session_id is None
        ):
            session.mailbox.put_discovery(update)

    def publish_audio(
        self,
        command: AudioControlCommand,
    ) -> TransportSession | None:
        """Enqueue one ordered control only for its exact authoritative socket."""

        if not self._accepting:
            return None
        key = (command.game_session_id, command.browser_token)
        if self._current_memberships.get(key) != command.connection_id:
            return None
        session = self._sessions.get(command.connection_id)
        if session is None or session.game_session_id != command.game_session_id:
            return None
        if not session.mailbox.put_control(command):
            return session
        return None

    async def clear_game(self, game_id: UUID) -> None:
        async with self._lock:
            keys = tuple(key for key in self._current_memberships if key[0] == game_id)
            for key in keys:
                connection_id = self._current_memberships.pop(key)
                session = self._sessions.get(connection_id)
                if session is not None and session.game_session_id == game_id:
                    session.game_session_id = None

    async def release(self, session: TransportSession) -> bool:
        """Release a socket and report whether it was still membership-current."""

        async with self._lock:
            self._sessions.pop(session.connection_id, None)
            session.mailbox.close()
            token = session.browser_token
            game_id = session.game_session_id
            if token is None or game_id is None:
                return False
            key = (game_id, token)
            if self._current_memberships.get(key) != session.connection_id:
                return False
            del self._current_memberships[key]
            return True

    async def shutdown(self) -> None:
        async with self._lock:
            self._accepting = False
            sessions = tuple(self._sessions.values())
            self._current_memberships.clear()
            for session in sessions:
                session.mailbox.close()
        if sessions:
            await asyncio.gather(
                *(
                    _close_socket(session.websocket, _SHUTDOWN_CLOSE_CODE)
                    for session in sessions
                ),
                return_exceptions=True,
            )
            sender_tasks = tuple(
                session.sender_task
                for session in sessions
                if session.sender_task is not None
            )
            if sender_tasks:
                for task in sender_tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*sender_tasks, return_exceptions=True)
            overload_tasks = tuple(
                session.overload_task
                for session in sessions
                if session.overload_task is not None
            )
            if overload_tasks:
                for task in overload_tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*overload_tasks, return_exceptions=True)


class WebSocketTransport:
    """Translate validated protocol operations into existing controller messages."""

    def __init__(self, controller: GameController, max_message_bytes: int) -> None:
        self._controller = controller
        self._max_message_bytes = max_message_bytes
        self.connections = TransportConnections()
        self._controller.add_projection_listener(self._publish_projection_update)
        self._controller.add_audio_control_listener(self._publish_audio_control)

    async def handle(self, websocket: WebSocket) -> None:
        connection_id = uuid4().hex
        session = TransportSession(connection_id=connection_id, websocket=websocket)
        await websocket.accept()
        if not await self.connections.add(session):
            await websocket.close(code=_SHUTDOWN_CLOSE_CODE)
            return
        try:
            await self._send_session_json(
                session,
                {
                    "version": PROTOCOL_VERSION,
                    "request_id": None,
                    "type": "transport_connected",
                    "ok": True,
                    "result": {
                        "connection_id": connection_id,
                        "max_message_bytes": self._max_message_bytes,
                    },
                },
            )
            session.sender_task = asyncio.create_task(
                self._deliver_outbound(session),
                name=f"wst-websocket-sender-{connection_id}",
            )
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                raw = message.get("text")
                if raw is None:
                    await self._send_error(
                        session, None, "invalid_command", "JSON text is required."
                    )
                    continue
                if len(raw.encode("utf-8")) > self._max_message_bytes:
                    await self._send_error(
                        session,
                        None,
                        "message_too_large",
                        "The command exceeds the configured message-size limit.",
                    )
                    continue
                if not self.connections.accepting:
                    await _close_socket(websocket, _SHUTDOWN_CLOSE_CODE)
                    break
                await self._handle_text(session, raw)
        except WebSocketDisconnect, TimeoutError:
            pass
        except RuntimeError:
            if websocket.client_state is not WebSocketState.DISCONNECTED:
                raise
        finally:
            await self._cleanup(session)

    async def shutdown(self) -> None:
        self._controller.remove_projection_listener(self._publish_projection_update)
        self._controller.remove_audio_control_listener(self._publish_audio_control)
        await self.connections.shutdown()

    def _publish_projection_update(
        self,
        update: AuthoritativeProjectionUpdate,
    ) -> None:
        """Controller callback: enqueue only; never perform network I/O."""

        self.connections.publish_discovery(update)
        for session in self.connections.publish(update):
            if session.overload_task is None or session.overload_task.done():
                session.overload_task = asyncio.create_task(
                    _close_socket(session.websocket, _SLOW_CLIENT_CLOSE_CODE),
                    name=f"wst-slow-client-close-{session.connection_id}",
                )

    def _publish_audio_control(self, command: AudioControlCommand) -> None:
        """Controller callback: enqueue an ordered control without network I/O."""

        session = self.connections.publish_audio(command)
        if session is not None and (
            session.overload_task is None or session.overload_task.done()
        ):
            session.overload_task = asyncio.create_task(
                _close_socket(session.websocket, _SLOW_CLIENT_CLOSE_CODE),
                name=f"wst-slow-client-close-{session.connection_id}",
            )

    async def _deliver_outbound(self, session: TransportSession) -> None:
        try:
            while True:
                record = await session.mailbox.get()
                if record is None:
                    return
                await self._send_session_json(session, record.message)
        except asyncio.CancelledError:
            return
        except WebSocketDisconnect, RuntimeError, TimeoutError:
            await _close_socket(session.websocket, _SLOW_CLIENT_CLOSE_CODE)
        except Exception:
            LOGGER.exception(
                "Outbound WebSocket delivery failed for %s",
                session.connection_id,
            )
            await _close_socket(session.websocket, _SLOW_CLIENT_CLOSE_CODE)

    @staticmethod
    async def _send_session_json(
        session: TransportSession,
        message: dict[str, Any],
    ) -> None:
        async with session.send_lock:
            await asyncio.wait_for(
                session.websocket.send_json(message),
                timeout=_SEND_TIMEOUT_SECONDS,
            )

    async def _handle_text(self, session: TransportSession, raw: str) -> None:
        request_id: str | None = None
        operation: str | None = None
        try:
            decoded = json.loads(raw)
            if not isinstance(decoded, dict):
                raise TypeError("command must be a JSON object")
            candidate_id = decoded.get("request_id")
            if isinstance(candidate_id, str) and 0 < len(candidate_id) <= 128:
                request_id = candidate_id
            version = decoded.get("version")
            if (
                isinstance(version, bool)
                or not isinstance(version, int)
                or version != PROTOCOL_VERSION
            ):
                await self._send_error(
                    session,
                    request_id,
                    "unsupported_protocol_version",
                    f"Only protocol version {PROTOCOL_VERSION} is supported.",
                )
                return
            envelope = CommandEnvelope.model_validate(decoded)
            request_id = envelope.request_id
            operation = envelope.type
            payload_type = PAYLOAD_MODELS.get(operation)
            if payload_type is None:
                await self._send_error(
                    session,
                    request_id,
                    "unknown_operation",
                    "The requested operation is not supported.",
                )
                return
            payload = payload_type.model_validate(envelope.payload)
        except json.JSONDecodeError, ValueError, ValidationError, TypeError:
            await self._send_error(
                session,
                request_id,
                "invalid_command",
                "The command envelope or payload is invalid.",
            )
            return

        try:
            result = await self._execute(session, operation, payload)
            await self._send_session_json(
                session,
                result_response(request_id, result),
            )
        except TimeoutError:
            await _close_socket(session.websocket, _SLOW_CLIENT_CLOSE_CODE)
        except Exception as error:
            code, message = _public_error(error)
            if code == "internal_error":
                LOGGER.exception(
                    "Unexpected WebSocket command failure for operation %s on %s",
                    operation,
                    session.connection_id,
                )
            await self._send_error(session, request_id, code, message)
            if isinstance(error, (ControllerFailedError, ControllerNotRunningError)):
                await _close_socket(session.websocket, 1011)

    async def _execute(
        self,
        session: TransportSession,
        operation: str,
        payload: ProtocolModel,
    ) -> dict[str, Any]:
        if operation == "establish_identity":
            claimed = payload.browser_token
            if session.browser_token is None:
                session.browser_token = claimed or token_urlsafe(32)
            elif claimed is not None and claimed != session.browser_token:
                raise ProtocolOperationError(
                    "identity_already_established",
                    "This transport already has a browser identity.",
                )
            self.connections.publish_discovery_to(
                session,
                self._controller.latest_projection,
            )
            return {"browser_token": session.browser_token}

        token = session.browser_token
        if token is None:
            raise ProtocolOperationError(
                "identity_required",
                "Establish a browser identity before using this operation.",
            )

        if operation == "create_game":
            synchronized = await self._controller.submit_membership_request(
                CreateGameRequest(token, payload.display_name, session.connection_id)
            )
            await self.connections.bind_membership(
                session,
                synchronized.update.game_session_id,
                lambda: self._controller.projection_for_game_member(
                    synchronized.update.game_session_id,
                    token,
                ),
            )
            return _membership_result(
                synchronized.update,
                synchronized.recipient,
            )

        if operation == "join_game":
            synchronized = await self._controller.submit_membership_request(
                JoinGameRequest(
                    token,
                    session.connection_id,
                    payload.display_name,
                )
            )
            game_id = synchronized.update.game_session_id
            await self.connections.bind_membership(
                session,
                game_id,
                lambda: self._controller.projection_for_game_member(
                    game_id,
                    token,
                ),
            )
            return _membership_result(
                synchronized.update,
                synchronized.recipient,
            )

        if operation == "reconnect":
            game_id = payload.game_session_id
            synchronized = await self._controller.submit_membership_request(
                RegisterConnectionRequest(game_id, token, session.connection_id)
            )
            await self.connections.bind_membership(
                session,
                game_id,
                lambda: self._controller.projection_for_game_member(
                    game_id,
                    token,
                ),
            )
            return _membership_result(
                synchronized.update,
                synchronized.recipient,
            )

        game_id = session.game_session_id
        if game_id is None:
            raise ProtocolOperationError(
                "membership_required",
                "Join, reconnect to, or create a game before using this operation.",
            )

        request = _build_member_request(operation, payload, game_id, token, session)
        if isinstance(
            request,
            (
                AudioPreparationReadyEvent,
                AudioPreparationFailedEvent,
                BrowserAudioCapabilityChangedEvent,
            ),
        ):
            self._controller.post_system_event(request)
            return {"accepted": True, "queued": True}

        if isinstance(request, BuzzRequest):
            synchronized = await self._controller.submit_projected_request(
                request,
                token,
            )
        else:
            synchronized = await self._controller.submit_projected_request(
                CurrentConnectionRequest(request, session.connection_id),
                token,
            )
        response = _command_result(
            synchronized.update,
            operation,
            synchronized.result,
        )
        if operation in {"cancel_game", "close_game"}:
            await self.connections.clear_game(game_id)
        return response

    async def _cleanup(self, session: TransportSession) -> None:
        was_current = await self.connections.release(session)
        sender = session.sender_task
        if sender is not None and not sender.done():
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
        if not was_current:
            return
        token = session.browser_token
        game_id = session.game_session_id
        if token is None or game_id is None:  # pragma: no cover - release checked both
            return
        try:
            self._controller.post_system_event(
                MemberDisconnectedEvent(game_id, token, session.connection_id)
            )
        except ControllerNotRunningError, ControllerFailedError:
            LOGGER.info(
                "Connection %s closed after controller retirement",
                session.connection_id,
            )

    @staticmethod
    async def _send_error(
        session: TransportSession,
        request_id: str | None,
        code: str,
        message: str,
    ) -> None:
        if session.websocket.application_state is WebSocketState.CONNECTED:
            await WebSocketTransport._send_session_json(
                session,
                error_response(request_id, code, message),
            )


class ProtocolOperationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message


def _build_member_request(
    operation: str,
    payload: ProtocolModel,
    game_id: UUID,
    token: str,
    session: TransportSession,
) -> object:
    if operation == "select_playlist":
        return SelectPlaylistRequest(game_id, token, payload.playlist_id)
    if operation == "deselect_playlist":
        return DeselectPlaylistRequest(game_id, token, payload.playlist_id)
    if operation == "start_game":
        return StartGameRequest(game_id, token)
    if operation == "set_audio_preference":
        return SetAudioPreferenceRequest(game_id, token, payload.audio_enabled)
    if operation == "set_audio_capability":
        return BrowserAudioCapabilityChangedEvent(
            game_id,
            token,
            session.connection_id,
            payload.browser_audio_usable,
        )
    if operation == "play_song":
        return PlaySongRequest(game_id, token)
    if operation == "audio_preparation_ready":
        return AudioPreparationReadyEvent(
            game_id,
            payload.preparation_id,
            token,
            session.connection_id,
        )
    if operation == "audio_preparation_failed":
        return AudioPreparationFailedEvent(
            game_id,
            payload.preparation_id,
            token,
            session.connection_id,
        )
    if operation == "acknowledge_audio_preparation_failure":
        return AcknowledgeAudioPreparationFailureRequest(
            game_id,
            payload.preparation_id,
            token,
        )
    if operation == "buzz":
        return BuzzRequest(
            game_id,
            payload.round_id,
            token,
            session.connection_id,
        )
    if operation == "reveal":
        return RevealRequest(game_id, payload.round_id, token)
    if operation == "select_score":
        return SelectProvisionalScoreRequest(
            game_id,
            payload.round_id,
            token,
            ScoringChoice(payload.scoring_choice),
        )
    if operation == "accept_score":
        return AcceptScoreRequest(
            game_id,
            payload.round_id,
            token,
            ScoringChoice(payload.scoring_choice),
        )
    if operation == "correct_score":
        return CorrectScoreRequest(
            game_id,
            payload.round_id,
            token,
            ScoringChoice(payload.scoring_choice),
        )
    if operation == "end_game":
        return EndGameRequest(game_id, token)
    if operation == "cancel_game":
        return CancelGameRequest(game_id, token)
    if operation == "close_game":
        return CloseGameRequest(game_id, token)
    if operation == "retry_completed_game_persistence":
        return RetryCompletedGamePersistenceRequest(game_id, token)
    if operation == "transfer_songmaster":
        return TransferSongmasterByPublicIdRequest(
            game_id,
            token,
            payload.recipient_public_participant_id,
        )
    raise ProtocolOperationError("unknown_operation", "The operation is not supported.")


def _membership_result(
    update: AuthoritativeProjectionUpdate,
    recipient: CapturedRecipientProjection,
) -> dict[str, Any]:
    public = update.public
    if public is None:  # pragma: no cover - membership dispatch creates an active view
        raise RuntimeError("membership response requires an active projection")
    participant = next(
        item
        for item in public.participants
        if item.public_participant_id == recipient.recipient.public_participant_id
    )
    return {
        "game_session_id": str(update.game_session_id),
        "display_name": participant.display_name,
        "public_participant_id": str(participant.public_participant_id),
        "is_songmaster": recipient.recipient.is_songmaster,
        "state": public.state.value,
    }


def _game_discovery_message(
    update: AuthoritativeProjectionUpdate | None,
) -> dict[str, Any]:
    public = None if update is None else update.public
    songmaster_name = None
    if public is not None:
        songmaster = next(
            (
                participant
                for participant in public.participants
                if participant.public_participant_id
                == public.songmaster_public_participant_id
            ),
            None,
        )
        if songmaster is not None:
            songmaster_name = songmaster.display_name
    payload = {
        "schema_version": 1,
        "revision": 0 if update is None else update.revision,
        "active_game_exists": public is not None,
        "joinable": public is not None and not public.finished,
        "state": "no_game" if public is None else public.state.value,
        "songmaster_display_name": songmaster_name,
    }
    return {
        "version": PROTOCOL_VERSION,
        "request_id": None,
        "type": "game_discovery",
        "payload": payload,
    }


def _command_result(
    update: AuthoritativeProjectionUpdate,
    operation: str,
    result: object,
) -> dict[str, Any]:
    response: dict[str, Any] = {"accepted": True}
    if operation not in {"cancel_game", "close_game"}:
        if update.public is None:  # pragma: no cover - only close/cancel invalidate
            raise RuntimeError("accepted gameplay requires an active projection")
        response["state"] = update.public.state.value
    if isinstance(result, PendingAudioPreparation):
        response["preparation_id"] = str(result.preparation_id)
    elif isinstance(result, GameFinalizationResult):
        response["end_reason"] = result.end_reason.value
        response["final_standings"] = [
            {
                "rank": standing.rank,
                "display_name": standing.display_name,
                "score": standing.score,
            }
            for standing in result.final_standings
        ]
    return response


def _audio_control_message(command: AudioControlCommand) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "game_session_id": str(command.game_session_id),
    }
    if isinstance(command, PrepareAudioCommand):
        message_type = "prepare_audio"
        payload.update(
            {
                "preparation_id": str(command.preparation_id),
                "media_url": command.media_url,
                "effective_playback_offset_seconds": str(
                    command.effective_playback_offset_seconds
                ),
                "playback_duration_seconds": command.playback_duration_seconds,
                "preparation_timeout_seconds": command.preparation_timeout_seconds,
            }
        )
    elif isinstance(command, PlayAudioCommand):
        message_type = "play_audio"
        payload.update(
            {
                "preparation_id": str(command.preparation_id),
                "round_id": str(command.round_id),
                "effective_playback_offset_seconds": str(
                    command.effective_playback_offset_seconds
                ),
                "playback_duration_seconds": command.playback_duration_seconds,
                "authoritative_playback_started_at": (
                    command.authoritative_playback_started_at.isoformat()
                ),
            }
        )
    elif isinstance(command, StopAudioCommand):
        message_type = "stop_audio"
        payload.update(
            {
                "preparation_id": (
                    None
                    if command.preparation_id is None
                    else str(command.preparation_id)
                ),
                "round_id": (
                    None if command.round_id is None else str(command.round_id)
                ),
                "reason": command.reason.value,
            }
        )
    else:  # pragma: no cover - closed command union
        raise TypeError("unsupported audio-control command")
    return {
        "version": PROTOCOL_VERSION,
        "request_id": None,
        "type": message_type,
        "payload": payload,
    }


_ERROR_CODES_BY_CLASS: Final[dict[str, str]] = {
    "ActiveGameExistsError": "game_unavailable",
    "NoActiveGameError": "game_unavailable",
    "DuplicatePlayerNameError": "duplicate_player_name",
    "UnknownGameMemberError": "invalid_membership",
    "StaleGameSessionError": "wrong_game",
    "StaleConnectionError": "stale_connection",
    "BuzzNotAllowedError": "buzz_not_allowed",
    "NoValidAudioTargetError": "no_valid_audio_target",
    "NoPlaylistSelectedError": "no_playlist_selected",
    "SelectedPlaylistNotFoundError": "playlist_not_found",
    "NoStructurallyEligibleTracksError": "no_eligible_tracks",
    "GameSuspendedForSongmasterRecoveryError": "game_suspended",
    "CompletedGameHistoryPersistenceError": "history_persistence_failed",
    "CompletedGameHistoryConflictError": "history_persistence_failed",
    "CompletedGamePersistenceNotAllowedError": "illegal_state",
    "ControllerNotRunningError": "server_shutting_down",
    "ControllerFailedError": "internal_error",
}


_PUBLIC_MESSAGES: Final[dict[str, str]] = {
    "game_unavailable": "A game is already active or unavailable.",
    "duplicate_player_name": "That display name is already in use.",
    "invalid_membership": "The browser token is not a member of this game.",
    "wrong_game": "The command does not target the current game.",
    "stale_connection": "This socket is no longer the member's current connection.",
    "buzz_not_allowed": "Buzzing is not allowed for this member or round.",
    "no_valid_audio_target": "No connected audio-ready browser is available.",
    "no_playlist_selected": "Select at least one playlist before starting.",
    "playlist_not_found": "A selected playlist is unavailable.",
    "no_eligible_tracks": "The selected playlists contain no usable tracks.",
    "game_suspended": "Gameplay is suspended during Songmaster recovery.",
    "history_persistence_failed": "Completed game history could not be saved.",
    "server_shutting_down": "The server is shutting down.",
    "not_songmaster": "Only the current connected Songmaster may do that.",
    "illegal_state": "That operation is not allowed in the current game state.",
    "invalid_command": "The command contains an invalid value.",
    "internal_error": "The server could not safely complete the command.",
}


def _public_error(error: Exception) -> tuple[str, str]:
    if isinstance(error, ProtocolOperationError):
        return error.code, error.public_message
    class_name = type(error).__name__
    code = _ERROR_CODES_BY_CLASS.get(class_name)
    if code is None and class_name.startswith("Unauthorized"):
        code = "not_songmaster"
    if code is None and class_name.endswith(("NotAllowedError", "InapplicableError")):
        code = "illegal_state"
    if code is None and (
        class_name.startswith("Invalid") or isinstance(error, (ValueError, TypeError))
    ):
        code = "invalid_command"
    if code is None:
        code = "internal_error"
    return code, _PUBLIC_MESSAGES[code]


async def _close_socket(websocket: WebSocket, code: int) -> None:
    if websocket.application_state is not WebSocketState.CONNECTED:
        return
    try:
        await asyncio.wait_for(websocket.close(code=code), timeout=1.0)
    except TimeoutError, RuntimeError:
        return


__all__ = ["TransportConnections", "TransportSession", "WebSocketTransport"]
