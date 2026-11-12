"""FastAPI/Starlette WebSocket adapter for the authoritative controller."""

import asyncio
import json
import logging
from dataclasses import dataclass
from secrets import token_urlsafe
from typing import Any, Final
from uuid import UUID, uuid4

from pydantic import ValidationError
from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from who_sings_that.application import (
    AcceptScoreRequest,
    AcknowledgeAudioPreparationFailureRequest,
    AudioPreparationFailedEvent,
    AudioPreparationReadyEvent,
    BrowserAudioCapabilityChangedEvent,
    BuzzRequest,
    CancelGameRequest,
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
    PlaySongRequest,
    RegisterConnectionRequest,
    RetryCompletedGamePersistenceRequest,
    RevealRequest,
    SelectPlaylistRequest,
    SetAudioPreferenceRequest,
    StartGameRequest,
)
from who_sings_that.domain import (
    GameFinalizationResult,
    GameMember,
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


@dataclass(slots=True)
class TransportSession:
    connection_id: str
    websocket: WebSocket
    browser_token: str | None = None
    game_session_id: UUID | None = None


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

    async def bind_membership(self, session: TransportSession, game_id: UUID) -> None:
        predecessor: TransportSession | None = None
        token = session.browser_token
        if token is None:  # pragma: no cover - guarded by command routing
            raise RuntimeError("membership binding requires browser identity")
        async with self._lock:
            key = (game_id, token)
            predecessor_id = self._current_memberships.get(key)
            session.game_session_id = game_id
            self._current_memberships[key] = session.connection_id
            if predecessor_id not in {None, session.connection_id}:
                predecessor = self._sessions.get(predecessor_id)
        if predecessor is not None:
            await _close_socket(predecessor.websocket, _REPLACED_CLOSE_CODE)

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
        if sessions:
            await asyncio.gather(
                *(
                    _close_socket(session.websocket, _SHUTDOWN_CLOSE_CODE)
                    for session in sessions
                ),
                return_exceptions=True,
            )


class WebSocketTransport:
    """Translate validated protocol operations into existing controller messages."""

    def __init__(self, controller: GameController, max_message_bytes: int) -> None:
        self._controller = controller
        self._max_message_bytes = max_message_bytes
        self.connections = TransportConnections()

    async def handle(self, websocket: WebSocket) -> None:
        connection_id = uuid4().hex
        session = TransportSession(connection_id=connection_id, websocket=websocket)
        await websocket.accept()
        if not await self.connections.add(session):
            await websocket.close(code=_SHUTDOWN_CLOSE_CODE)
            return
        await websocket.send_json(
            {
                "version": PROTOCOL_VERSION,
                "request_id": None,
                "type": "transport_connected",
                "ok": True,
                "result": {
                    "connection_id": connection_id,
                    "max_message_bytes": self._max_message_bytes,
                },
            }
        )

        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    break
                raw = message.get("text")
                if raw is None:
                    await self._send_error(
                        websocket, None, "invalid_command", "JSON text is required."
                    )
                    continue
                if len(raw.encode("utf-8")) > self._max_message_bytes:
                    await self._send_error(
                        websocket,
                        None,
                        "message_too_large",
                        "The command exceeds the configured message-size limit.",
                    )
                    continue
                if not self.connections.accepting:
                    await _close_socket(websocket, _SHUTDOWN_CLOSE_CODE)
                    break
                await self._handle_text(session, raw)
        except WebSocketDisconnect:
            pass
        except RuntimeError:
            if websocket.client_state is not WebSocketState.DISCONNECTED:
                raise
        finally:
            await self._cleanup(session)

    async def shutdown(self) -> None:
        await self.connections.shutdown()

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
                    session.websocket,
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
                    session.websocket,
                    request_id,
                    "unknown_operation",
                    "The requested operation is not supported.",
                )
                return
            payload = payload_type.model_validate(envelope.payload)
        except (json.JSONDecodeError, ValueError, ValidationError, TypeError):
            await self._send_error(
                session.websocket,
                request_id,
                "invalid_command",
                "The command envelope or payload is invalid.",
            )
            return

        try:
            result = await self._execute(session, operation, payload)
            await session.websocket.send_json(result_response(request_id, result))
        except Exception as error:
            code, message = _public_error(error)
            if code == "internal_error":
                LOGGER.exception(
                    "Unexpected WebSocket command failure for operation %s on %s",
                    operation,
                    session.connection_id,
                )
            await self._send_error(session.websocket, request_id, code, message)
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
            return {"browser_token": session.browser_token}

        token = session.browser_token
        if token is None:
            raise ProtocolOperationError(
                "identity_required",
                "Establish a browser identity before using this operation.",
            )

        if operation == "create_game":
            game = await self._controller.submit_request(
                CreateGameRequest(token, payload.display_name, session.connection_id)
            )
            await self.connections.bind_membership(session, game.game_session_id)
            return _membership_result(self._controller, game.members[token])

        if operation == "join_game":
            game_id = payload.game_session_id
            member = await self._controller.submit_request(
                JoinGameRequest(
                    game_id,
                    token,
                    session.connection_id,
                    payload.display_name,
                )
            )
            await self.connections.bind_membership(session, game_id)
            return _membership_result(self._controller, member)

        if operation == "reconnect":
            game_id = payload.game_session_id
            await self._controller.submit_request(
                RegisterConnectionRequest(game_id, token, session.connection_id)
            )
            game = self._controller.game_engine.active_game
            if game is None:  # pragma: no cover - controller established membership
                raise RuntimeError("reconnect completed without an active game")
            await self.connections.bind_membership(session, game_id)
            return _membership_result(self._controller, game.members[token])

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
            result = await self._controller.submit_request(request)
        else:
            result = await self._controller.submit_request(
                CurrentConnectionRequest(request, session.connection_id)
            )
        response = _command_result(self._controller, operation, result)
        if operation in {"cancel_game", "close_game"}:
            await self.connections.clear_game(game_id)
        return response

    async def _cleanup(self, session: TransportSession) -> None:
        if not await self.connections.release(session):
            return
        token = session.browser_token
        game_id = session.game_session_id
        if token is None or game_id is None:  # pragma: no cover - release checked both
            return
        try:
            self._controller.post_system_event(
                MemberDisconnectedEvent(game_id, token, session.connection_id)
            )
        except (ControllerNotRunningError, ControllerFailedError):
            LOGGER.info(
                "Connection %s closed after controller retirement",
                session.connection_id,
            )

    @staticmethod
    async def _send_error(
        websocket: WebSocket,
        request_id: str | None,
        code: str,
        message: str,
    ) -> None:
        if websocket.application_state is WebSocketState.CONNECTED:
            await websocket.send_json(error_response(request_id, code, message))


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
    raise ProtocolOperationError("unknown_operation", "The operation is not supported.")


def _membership_result(controller: GameController, member: GameMember) -> dict[str, Any]:
    game = controller.game_engine.active_game
    if game is None:  # pragma: no cover - membership response requires a game
        raise RuntimeError("membership response requires an active game")
    return {
        "game_session_id": str(game.game_session_id),
        "display_name": member.display_name,
        "is_songmaster": member.browser_token == game.songmaster_browser_token,
        "state": controller.game_engine.state.value,
    }


def _command_result(
    controller: GameController,
    operation: str,
    result: object,
) -> dict[str, Any]:
    response: dict[str, Any] = {"accepted": True}
    if operation not in {"cancel_game", "close_game"}:
        response["state"] = controller.game_engine.state.value
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


_ERROR_CODES_BY_CLASS: Final[dict[str, str]] = {
    "ActiveGameExistsError": "game_unavailable",
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
    if code is None and class_name.endswith(
        ("NotAllowedError", "InapplicableError")
    ):
        code = "illegal_state"
    if code is None and (
        class_name.startswith("Invalid")
        or isinstance(error, (ValueError, TypeError))
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
    except (TimeoutError, RuntimeError):
        return


__all__ = ["TransportConnections", "TransportSession", "WebSocketTransport"]
