"""Environment-backed configuration for the FastAPI process."""

from dataclasses import dataclass
from math import isfinite
from os import environ
from pathlib import Path

from who_sings_that.application import DEFAULT_MEDIA_CAPABILITY_LIFETIME_SECONDS


@dataclass(frozen=True, slots=True)
class ServerSettings:
    """Small set of process settings needed by the Phase A server."""

    database_path: Path = Path("wst.sqlite3")
    music_root: Path = Path(".")
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000
    log_level: str = "info"
    max_websocket_message_bytes: int = 65_536
    media_capability_lifetime_seconds: float = DEFAULT_MEDIA_CAPABILITY_LIFETIME_SECONDS

    def __post_init__(self) -> None:
        if not 1 <= self.bind_port <= 65_535:
            raise ValueError("bind_port must be between 1 and 65535")
        if self.max_websocket_message_bytes <= 0:
            raise ValueError("max_websocket_message_bytes must be positive")
        if (
            not isfinite(self.media_capability_lifetime_seconds)
            or self.media_capability_lifetime_seconds <= 0
        ):
            raise ValueError("media_capability_lifetime_seconds must be positive")

    @classmethod
    def from_environment(cls) -> ServerSettings:
        """Read settings without initializing any application resources."""

        return cls(
            database_path=Path(environ.get("WST_DATABASE_PATH", "wst.sqlite3")),
            music_root=Path(environ.get("WST_MUSIC_ROOT", ".")),
            bind_host=environ.get("WST_HOST", "127.0.0.1"),
            bind_port=_environment_integer("WST_PORT", 8000),
            log_level=environ.get("WST_LOG_LEVEL", "info"),
            max_websocket_message_bytes=_environment_integer(
                "WST_MAX_WEBSOCKET_MESSAGE_BYTES",
                65_536,
            ),
            media_capability_lifetime_seconds=_environment_float(
                "WST_MEDIA_CAPABILITY_LIFETIME_SECONDS",
                DEFAULT_MEDIA_CAPABILITY_LIFETIME_SECONDS,
            ),
        )


def _environment_integer(name: str, default: int) -> int:
    value = environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error


def _environment_float(name: str, default: float) -> float:
    value = environ.get(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as error:
        raise ValueError(f"{name} must be a number") from error


__all__ = ["ServerSettings"]
