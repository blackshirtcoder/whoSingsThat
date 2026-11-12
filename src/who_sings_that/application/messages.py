"""Typed authoritative messages accepted by the application controller."""

from dataclasses import dataclass
from uuid import UUID

from who_sings_that.domain import ScoringChoice


class ClientRequest:
    """Marker for queued work whose submitting client expects one response."""


class SystemEvent:
    """Marker for a trusted, unsolicited fact that does not require a response."""


@dataclass(frozen=True, slots=True)
class CreateGameRequest(ClientRequest):
    """Request transition A for the identified browser-local player."""

    browser_token: str
    display_name: str
    source_connection_id: str


@dataclass(frozen=True, slots=True)
class CancelGameRequest(ClientRequest):
    """Cancel the identified unstarted lobby directly into NO_GAME."""

    game_session_id: UUID
    requesting_browser_token: str


@dataclass(frozen=True, slots=True)
class JoinGameRequest(ClientRequest):
    """Join a new member or reconnect an existing browser identity."""

    browser_token: str
    source_connection_id: str
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class RegisterConnectionRequest(ClientRequest):
    """Trusted transport request to establish or replace a member connection.

    Future WebSocket code must generate and authenticate ``connection_id``;
    browsers must never be trusted to assert arbitrary connection identities.
    This request cannot create game membership.
    """

    game_session_id: UUID
    browser_token: str
    connection_id: str


@dataclass(frozen=True, slots=True)
class CurrentConnectionRequest(ClientRequest):
    """Run an existing member request only for its current live connection.

    Transport adapters use this wrapper for requests whose original application
    record predates physical connection authentication.  Validation and the
    wrapped dispatch occur in one FIFO item, so a queued connection replacement
    cannot race a later command from the superseded socket.
    """

    request: ClientRequest
    source_connection_id: str


@dataclass(frozen=True, slots=True)
class SelectPlaylistRequest(ClientRequest):
    """Request selection of one persistent playlist in the lobby."""

    game_session_id: UUID
    requesting_browser_token: str
    playlist_id: str


@dataclass(frozen=True, slots=True)
class DeselectPlaylistRequest(ClientRequest):
    """Request deselection of one persistent playlist in the lobby."""

    game_session_id: UUID
    requesting_browser_token: str
    playlist_id: str


@dataclass(frozen=True, slots=True)
class StartGameRequest(ClientRequest):
    """Resolve persistent selections and request transition B."""

    game_session_id: UUID
    requesting_browser_token: str


@dataclass(frozen=True, slots=True)
class SetAudioPreferenceRequest(ClientRequest):
    """Request a change to the requesting member's own audio preference."""

    game_session_id: UUID
    requesting_browser_token: str
    audio_enabled: bool


@dataclass(frozen=True, slots=True)
class TransferSongmasterRequest(ClientRequest):
    """Voluntarily transfer the role to another connected member."""

    game_session_id: UUID
    requesting_browser_token: str
    recipient_browser_token: str


@dataclass(frozen=True, slots=True)
class TransferSongmasterByPublicIdRequest(ClientRequest):
    """Transport-safe voluntary transfer resolved inside the controller."""

    game_session_id: UUID
    requesting_browser_token: str
    recipient_public_participant_id: UUID


@dataclass(frozen=True, slots=True)
class PlaySongRequest(ClientRequest):
    """Request transition C or K from the connected Songmaster."""

    game_session_id: UUID
    requesting_browser_token: str


@dataclass(frozen=True, slots=True)
class EndGameRequest(ClientRequest):
    """Voluntarily finalize the identified started game."""

    game_session_id: UUID
    requesting_browser_token: str


@dataclass(frozen=True, slots=True)
class CloseGameRequest(ClientRequest):
    """Request transition L for the identified finished game."""

    game_session_id: UUID
    requesting_browser_token: str


@dataclass(frozen=True, slots=True)
class RetryCompletedGamePersistenceRequest(ClientRequest):
    """Retry the atomic archive write for a retained finished game."""

    game_session_id: UUID
    requesting_browser_token: str


@dataclass(frozen=True, slots=True)
class BuzzRequest(ClientRequest):
    """Request transition F for one actual round.

    Trusted receipt time is intentionally absent. The controller attaches it at
    authoritative ingress before this request waits in the FIFO queue.
    """

    game_session_id: UUID
    round_id: UUID
    requesting_browser_token: str
    source_connection_id: str


@dataclass(frozen=True, slots=True)
class RevealRequest(ClientRequest):
    """Request transition H or I for the current actual round."""

    game_session_id: UUID
    round_id: UUID
    requesting_browser_token: str


@dataclass(frozen=True, slots=True)
class AcceptScoreRequest(ClientRequest):
    """Request transition J for the current revealed buzz round."""

    game_session_id: UUID
    round_id: UUID
    requesting_browser_token: str
    scoring_choice: ScoringChoice


@dataclass(frozen=True, slots=True)
class SelectProvisionalScoreRequest(ClientRequest):
    """Replace the current SCORING display choice without committing it."""

    game_session_id: UUID
    round_id: UUID
    requesting_browser_token: str
    scoring_choice: ScoringChoice


@dataclass(frozen=True, slots=True)
class CorrectScoreRequest(ClientRequest):
    """Replace the most recently completed buzz round's scoring decision."""

    game_session_id: UUID
    round_id: UUID
    requesting_browser_token: str
    scoring_choice: ScoringChoice


@dataclass(frozen=True, slots=True)
class AcknowledgeAudioPreparationFailureRequest(ClientRequest):
    """Request transition D for a finalized unsuccessful preparation."""

    game_session_id: UUID
    preparation_id: UUID
    requesting_browser_token: str


@dataclass(frozen=True, slots=True)
class MemberDisconnectedEvent(SystemEvent):
    """Trusted observation that a particular browser connection was lost."""

    game_session_id: UUID
    browser_token: str
    source_connection_id: str


@dataclass(frozen=True, slots=True)
class BrowserAudioCapabilityChangedEvent(SystemEvent):
    """Trusted observation of one connected browser's usable audio state."""

    game_session_id: UUID
    browser_token: str
    source_connection_id: str
    browser_audio_usable: bool


@dataclass(frozen=True, slots=True)
class AudioPreparationReadyEvent(SystemEvent):
    """Trusted client-originated READY notification for one target."""

    game_session_id: UUID
    preparation_id: UUID
    browser_token: str
    source_connection_id: str


@dataclass(frozen=True, slots=True)
class AudioPreparationFailedEvent(SystemEvent):
    """Trusted client-originated FAILED notification for one target."""

    game_session_id: UUID
    preparation_id: UUID
    browser_token: str
    source_connection_id: str


@dataclass(frozen=True, slots=True)
class AudioPreparationTimeoutEvent(SystemEvent):
    """Application timer expiry for one specific preparation attempt."""

    game_session_id: UUID
    preparation_id: UUID


@dataclass(frozen=True, slots=True)
class PlaybackTimeoutEvent(SystemEvent):
    """Application timer expiry for one specific actual round."""

    game_session_id: UUID
    round_id: UUID


@dataclass(frozen=True, slots=True)
class SongmasterReconnectGraceExpiredEvent(SystemEvent):
    """Application timer expiry for one recovery generation."""

    game_session_id: UUID
    recovery_id: UUID


AuthoritativeMessage = ClientRequest | SystemEvent


__all__ = [
    "AcceptScoreRequest",
    "AcknowledgeAudioPreparationFailureRequest",
    "AudioPreparationFailedEvent",
    "AudioPreparationReadyEvent",
    "AudioPreparationTimeoutEvent",
    "AuthoritativeMessage",
    "BrowserAudioCapabilityChangedEvent",
    "BuzzRequest",
    "CancelGameRequest",
    "ClientRequest",
    "CloseGameRequest",
    "CorrectScoreRequest",
    "CreateGameRequest",
    "CurrentConnectionRequest",
    "DeselectPlaylistRequest",
    "EndGameRequest",
    "JoinGameRequest",
    "MemberDisconnectedEvent",
    "PlaySongRequest",
    "PlaybackTimeoutEvent",
    "RegisterConnectionRequest",
    "RetryCompletedGamePersistenceRequest",
    "RevealRequest",
    "SelectPlaylistRequest",
    "SelectProvisionalScoreRequest",
    "SetAudioPreferenceRequest",
    "SongmasterReconnectGraceExpiredEvent",
    "StartGameRequest",
    "SystemEvent",
    "TransferSongmasterByPublicIdRequest",
    "TransferSongmasterRequest",
]
