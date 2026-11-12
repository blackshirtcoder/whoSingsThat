"""Connection-scoped media capabilities and outbound audio-control DTOs."""

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from enum import Enum
from secrets import token_urlsafe
from uuid import UUID


class MediaCapabilityUnavailable(LookupError):
    """Raised when an opaque media capability is unknown or no longer valid."""


class MediaCapabilityPhase(Enum):
    PREPARING = "preparing"
    PLAYING = "playing"


@dataclass(frozen=True, slots=True)
class MediaCapability:
    """Private server-side scope for one opaque bearer capability."""

    opaque_token: str
    game_session_id: UUID
    preparation_id: UUID
    round_id: UUID | None
    browser_token: str
    connection_id: str
    track_id: int
    expires_at_monotonic: float
    phase: MediaCapabilityPhase


class MediaCapabilityStore:
    """Small in-memory capability store; active-game crash recovery is out of scope."""

    def __init__(
        self,
        lifetime_seconds: float,
        monotonic_clock: Callable[[], float],
    ) -> None:
        self._lifetime_seconds = lifetime_seconds
        self._monotonic_clock = monotonic_clock
        self._records: dict[str, MediaCapability] = {}

    @property
    def active_count(self) -> int:
        self._discard_expired()
        return len(self._records)

    def issue(
        self,
        *,
        game_session_id: UUID,
        preparation_id: UUID,
        browser_token: str,
        connection_id: str,
        track_id: int,
    ) -> MediaCapability:
        token = token_urlsafe(32)
        while token in self._records:  # pragma: no cover - defensive entropy guard
            token = token_urlsafe(32)
        record = MediaCapability(
            opaque_token=token,
            game_session_id=game_session_id,
            preparation_id=preparation_id,
            round_id=None,
            browser_token=browser_token,
            connection_id=connection_id,
            track_id=track_id,
            expires_at_monotonic=(self._monotonic_clock() + self._lifetime_seconds),
            phase=MediaCapabilityPhase.PREPARING,
        )
        self._records[token] = record
        return record

    def authorize(self, opaque_token: str) -> MediaCapability:
        self._discard_expired()
        record = self._records.get(opaque_token)
        if record is None:
            raise MediaCapabilityUnavailable("media capability is unavailable")
        return record

    def promote_ready(
        self,
        preparation_id: UUID,
        ready_browser_tokens: frozenset[str],
        round_id: UUID,
    ) -> None:
        for token, record in tuple(self._records.items()):
            if record.preparation_id != preparation_id:
                continue
            if record.browser_token not in ready_browser_tokens:
                del self._records[token]
                continue
            self._records[token] = replace(
                record,
                round_id=round_id,
                phase=MediaCapabilityPhase.PLAYING,
            )

    def revoke_preparation(self, preparation_id: UUID) -> None:
        self._revoke_where(lambda record: record.preparation_id == preparation_id)

    def revoke_recipient(
        self,
        game_session_id: UUID,
        browser_token: str,
        connection_id: str | None = None,
    ) -> None:
        self._revoke_where(
            lambda record: (
                record.game_session_id == game_session_id
                and record.browser_token == browser_token
                and (connection_id is None or record.connection_id == connection_id)
            )
        )

    def revoke_game(self, game_session_id: UUID) -> None:
        self._revoke_where(lambda record: record.game_session_id == game_session_id)

    def revoke_all(self) -> None:
        self._records.clear()

    def reconcile_connections(
        self,
        is_current: Callable[[UUID, str, str], bool],
    ) -> None:
        self._revoke_where(
            lambda record: (
                not is_current(
                    record.game_session_id,
                    record.browser_token,
                    record.connection_id,
                )
            )
        )

    def _discard_expired(self) -> None:
        now = self._monotonic_clock()
        self._revoke_where(lambda record: now >= record.expires_at_monotonic)

    def _revoke_where(self, predicate: Callable[[MediaCapability], bool]) -> None:
        for token, record in tuple(self._records.items()):
            if predicate(record):
                del self._records[token]


class AudioStopReason(Enum):
    ACCEPTED_BUZZ = "accepted_buzz"
    PLAYBACK_TIMEOUT = "playback_timeout"
    ROUND_VOID = "round_void"
    PLAYBACK_CLIENT_LOSS = "playback_client_loss"
    SONGMASTER_LOSS = "songmaster_loss"
    PREPARATION_FAILED = "preparation_failed"
    GAME_TERMINATION = "game_termination"
    CONNECTION_REPLACED = "connection_replaced"


@dataclass(frozen=True, slots=True)
class PrepareAudioCommand:
    game_session_id: UUID
    preparation_id: UUID
    browser_token: str
    connection_id: str
    media_url: str
    effective_playback_offset_seconds: Decimal
    playback_duration_seconds: int
    preparation_timeout_seconds: float


@dataclass(frozen=True, slots=True)
class PlayAudioCommand:
    game_session_id: UUID
    preparation_id: UUID
    round_id: UUID
    browser_token: str
    connection_id: str
    effective_playback_offset_seconds: Decimal
    playback_duration_seconds: int
    authoritative_playback_started_at: datetime


@dataclass(frozen=True, slots=True)
class StopAudioCommand:
    game_session_id: UUID
    preparation_id: UUID | None
    round_id: UUID | None
    browser_token: str
    connection_id: str
    reason: AudioStopReason


AudioControlCommand = PrepareAudioCommand | PlayAudioCommand | StopAudioCommand


__all__ = [
    "AudioControlCommand",
    "AudioStopReason",
    "MediaCapability",
    "MediaCapabilityPhase",
    "MediaCapabilityStore",
    "MediaCapabilityUnavailable",
    "PlayAudioCommand",
    "PrepareAudioCommand",
    "StopAudioCommand",
]
