"""Typed authoritative messages accepted by the application controller."""

from dataclasses import dataclass
from uuid import UUID


class ClientRequest:
    """Marker for queued work whose submitting client expects one response."""


class SystemEvent:
    """Marker for a trusted, unsolicited fact that does not require a response."""


@dataclass(frozen=True, slots=True)
class CreateGameRequest(ClientRequest):
    """Request transition A for the identified browser-local player."""

    browser_token: str
    display_name: str


@dataclass(frozen=True, slots=True)
class JoinGameRequest(ClientRequest):
    """Join a new member or reconnect an existing browser identity."""

    game_session_id: UUID
    browser_token: str
    display_name: str | None = None


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
class PlaySongRequest(ClientRequest):
    """Request transition C from the connected Songmaster."""

    game_session_id: UUID
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


AuthoritativeMessage = ClientRequest | SystemEvent


__all__ = [
    "AuthoritativeMessage",
    "BrowserAudioCapabilityChangedEvent",
    "ClientRequest",
    "CreateGameRequest",
    "DeselectPlaylistRequest",
    "JoinGameRequest",
    "MemberDisconnectedEvent",
    "PlaySongRequest",
    "SelectPlaylistRequest",
    "SetAudioPreferenceRequest",
    "StartGameRequest",
    "SystemEvent",
]
