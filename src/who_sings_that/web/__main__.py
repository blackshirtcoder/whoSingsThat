"""Development/process entry point using the environment-backed settings."""

import uvicorn

from .config import ServerSettings


def main() -> None:
    settings = ServerSettings.from_environment()
    uvicorn.run(
        "who_sings_that.web:app",
        host=settings.bind_host,
        port=settings.bind_port,
        log_level=settings.log_level,
        ws_max_size=settings.max_websocket_message_bytes,
        workers=1,
    )


if __name__ == "__main__":
    main()
