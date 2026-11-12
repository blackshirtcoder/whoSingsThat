"""Versioned, validated JSON records for the Phase A WebSocket protocol."""

from typing import Annotated, Any, Final
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StrictBool, StringConstraints

PROTOCOL_VERSION: Final = 1

RequestId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=128),
]
BrowserToken = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=16, max_length=256),
]
DisplayName = Annotated[str, StringConstraints(min_length=1, max_length=128)]
OpaqueId = Annotated[str, StringConstraints(min_length=1, max_length=256)]


class ProtocolModel(BaseModel):
    """Forbid undeclared fields, including client-asserted connection IDs."""

    model_config = ConfigDict(extra="forbid")


class CommandEnvelope(ProtocolModel):
    version: int
    request_id: RequestId
    type: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    payload: dict[str, Any]


class EmptyPayload(ProtocolModel):
    pass


class EstablishIdentityPayload(ProtocolModel):
    browser_token: BrowserToken | None = None


class CreateGamePayload(ProtocolModel):
    display_name: DisplayName


class JoinGamePayload(ProtocolModel):
    game_session_id: UUID
    display_name: DisplayName


class ReconnectPayload(ProtocolModel):
    game_session_id: UUID


class PlaylistPayload(ProtocolModel):
    playlist_id: OpaqueId


class AudioPreferencePayload(ProtocolModel):
    audio_enabled: StrictBool


class AudioCapabilityPayload(ProtocolModel):
    browser_audio_usable: StrictBool


class RoundPayload(ProtocolModel):
    round_id: UUID


class PreparationPayload(ProtocolModel):
    preparation_id: UUID


class ScoringPayload(RoundPayload):
    scoring_choice: Annotated[
        str,
        StringConstraints(to_lower=True, min_length=4, max_length=7),
    ]


class TransferSongmasterPayload(ProtocolModel):
    recipient_public_participant_id: UUID


PAYLOAD_MODELS: Final[dict[str, type[ProtocolModel]]] = {
    "establish_identity": EstablishIdentityPayload,
    "create_game": CreateGamePayload,
    "join_game": JoinGamePayload,
    "reconnect": ReconnectPayload,
    "select_playlist": PlaylistPayload,
    "deselect_playlist": PlaylistPayload,
    "start_game": EmptyPayload,
    "set_audio_preference": AudioPreferencePayload,
    "set_audio_capability": AudioCapabilityPayload,
    "play_song": EmptyPayload,
    "audio_preparation_ready": PreparationPayload,
    "audio_preparation_failed": PreparationPayload,
    "acknowledge_audio_preparation_failure": PreparationPayload,
    "buzz": RoundPayload,
    "reveal": RoundPayload,
    "select_score": ScoringPayload,
    "accept_score": ScoringPayload,
    "correct_score": ScoringPayload,
    "end_game": EmptyPayload,
    "cancel_game": EmptyPayload,
    "close_game": EmptyPayload,
    "retry_completed_game_persistence": EmptyPayload,
    "transfer_songmaster": TransferSongmasterPayload,
}


def result_response(request_id: str | None, result: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": PROTOCOL_VERSION,
        "request_id": request_id,
        "type": "result",
        "ok": True,
        "result": result,
    }


def error_response(
    request_id: str | None,
    code: str,
    message: str,
) -> dict[str, Any]:
    return {
        "version": PROTOCOL_VERSION,
        "request_id": request_id,
        "type": "result",
        "ok": False,
        "error": {"code": code, "message": message},
    }


__all__ = [
    "PAYLOAD_MODELS",
    "PROTOCOL_VERSION",
    "CommandEnvelope",
    "ProtocolModel",
    "error_response",
    "result_response",
]
