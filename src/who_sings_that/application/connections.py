"""Small application-owned registry of authoritative browser connections."""

from dataclasses import dataclass
from enum import Enum
from uuid import UUID


class InvalidConnectionIdError(ValueError):
    """Raised when trusted transport code supplies an unusable connection ID."""


class ConnectionRegistrationStatus(Enum):
    """How one serialized registration changed the authoritative association."""

    ESTABLISHED = "established"
    REPLACED = "replaced"
    RECONNECTED = "reconnected"
    UNCHANGED = "unchanged"


@dataclass(frozen=True, slots=True)
class ConnectionRegistration:
    """Result of registering one game-scoped member connection."""

    game_session_id: UUID
    browser_token: str
    connection_id: str
    status: ConnectionRegistrationStatus
    continuous_connection_sequence: int

    @property
    def connection_changed(self) -> bool:
        """Return whether connection-specific domain facts must be reset."""

        return self.status is not ConnectionRegistrationStatus.UNCHANGED


@dataclass(slots=True)
class _ConnectionRecord:
    connection_id: str
    connected: bool
    continuous_connection_sequence: int


class ConnectionRegistry:
    """Track one current connection per game-scoped browser membership.

    Membership, role, and score remain exclusively in ``GameSession``. This
    registry stores only transport identity and whether that exact connection
    remains live.
    """

    def __init__(self) -> None:
        self._records: dict[tuple[UUID, str], _ConnectionRecord] = {}
        self._next_connection_sequence = 0

    @staticmethod
    def validate_connection_id(connection_id: str) -> str:
        """Validate and return one opaque transport-generated connection ID."""

        if not isinstance(connection_id, str):
            raise InvalidConnectionIdError("connection_id must be a string")
        if not connection_id:
            raise InvalidConnectionIdError("connection_id must not be empty")
        return connection_id

    def registration_status(
        self,
        game_session_id: UUID,
        browser_token: str,
        connection_id: str,
    ) -> ConnectionRegistrationStatus:
        """Preview a registration without mutating the registry."""

        validated_id = self.validate_connection_id(connection_id)
        record = self._records.get((game_session_id, browser_token))
        if record is None:
            return ConnectionRegistrationStatus.ESTABLISHED
        if record.connection_id != validated_id:
            return ConnectionRegistrationStatus.REPLACED
        if not record.connected:
            return ConnectionRegistrationStatus.RECONNECTED
        return ConnectionRegistrationStatus.UNCHANGED

    def register(
        self,
        game_session_id: UUID,
        browser_token: str,
        connection_id: str,
    ) -> ConnectionRegistration:
        """Make a connection current, replacing or reviving the prior record."""

        validated_id = self.validate_connection_id(connection_id)
        status = self.registration_status(
            game_session_id,
            browser_token,
            validated_id,
        )
        key = (game_session_id, browser_token)
        existing = self._records.get(key)
        if status is ConnectionRegistrationStatus.UNCHANGED:
            if existing is None:  # pragma: no cover - established by status
                raise RuntimeError("unchanged registration requires a record")
            sequence = existing.continuous_connection_sequence
        else:
            sequence = self._next_connection_sequence
            self._next_connection_sequence += 1
        self._records[key] = _ConnectionRecord(
            connection_id=validated_id,
            connected=True,
            continuous_connection_sequence=sequence,
        )
        return ConnectionRegistration(
            game_session_id=game_session_id,
            browser_token=browser_token,
            connection_id=validated_id,
            status=status,
            continuous_connection_sequence=sequence,
        )

    def connected_members(
        self,
        game_session_id: UUID,
    ) -> tuple[tuple[str, int], ...]:
        """Return connected member tokens with continuous-connection order."""

        return tuple(
            (browser_token, record.continuous_connection_sequence)
            for (record_game_id, browser_token), record in self._records.items()
            if record_game_id == game_session_id and record.connected
        )

    def is_current(
        self,
        game_session_id: UUID,
        browser_token: str,
        connection_id: str,
    ) -> bool:
        """Return whether the exact identified connection is currently live."""

        record = self._records.get((game_session_id, browser_token))
        return bool(
            record is not None
            and record.connected
            and record.connection_id == connection_id
        )

    def current_connection_id(
        self,
        game_session_id: UUID,
        browser_token: str,
    ) -> str | None:
        """Return the current live transport identity for one membership."""

        record = self._records.get((game_session_id, browser_token))
        if record is None or not record.connected:
            return None
        return record.connection_id

    def mark_disconnected(
        self,
        game_session_id: UUID,
        browser_token: str,
        connection_id: str,
    ) -> bool:
        """Retire an exact current connection and retain its stale identity."""

        if not self.is_current(game_session_id, browser_token, connection_id):
            return False
        self._records[(game_session_id, browser_token)].connected = False
        return True

    def clear_game(self, game_session_id: UUID) -> None:
        """Discard all connection associations belonging to one ended session."""

        keys = tuple(
            key for key in self._records if key[0] == game_session_id
        )
        for key in keys:
            del self._records[key]

