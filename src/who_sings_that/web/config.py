"""Environment-backed configuration for the FastAPI process."""

from dataclasses import dataclass
from os import environ
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ServerSettings:
    """Small set of process settings needed by the Phase A server."""

    database_path: Path = Path("wst.sqlite3")
    music_root: Path = Path(".")
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000
    log_level: str = "info"
    max_websocket_message_bytes: int = 65_536

    def __post_init__(self) -> None:
        if not 1 <= self.bind_port <= 65_535:
            raise ValueError("bind_port must be between 1 and 65535")
        if self.max_websocket_message_bytes <= 0:
            raise ValueError("max_websocket_message_bytes must be positive")

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
        )


def _environment_integer(name: str, default: int) -> int:
    value = environ.get(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error


__all__ = ["ServerSettings"]
