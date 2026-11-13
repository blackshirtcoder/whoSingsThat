"""Importable FastAPI application and lifespan construction."""

import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import Engine

from who_sings_that.application import GameController
from who_sings_that.database import (
    create_sqlite_engine,
    list_playlists,
    upgrade_database,
)

from .config import ServerSettings
from .media import media_response
from .transport import WebSocketTransport

ControllerFactory = Callable[[Engine, ServerSettings], GameController]
_STATIC_DIRECTORY = Path(__file__).with_name("static")


def create_app(
    settings: ServerSettings | None = None,
    *,
    controller_factory: ControllerFactory | None = None,
) -> FastAPI:
    """Build an app with no database/controller startup side effects."""

    configured = settings if settings is not None else ServerSettings.from_environment()
    build_controller = controller_factory or _default_controller_factory

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        upgrade_database(configured.database_path)
        database_engine = create_sqlite_engine(configured.database_path)
        controller = build_controller(database_engine, configured)
        transport = WebSocketTransport(
            controller,
            configured.max_websocket_message_bytes,
        )
        application.state.settings = configured
        application.state.database_engine = database_engine
        application.state.game_controller = controller
        application.state.websocket_transport = transport
        try:
            await controller.start()
            yield
        finally:
            await transport.shutdown()
            try:
                await controller.stop()
            finally:
                database_engine.dispose()

    application = FastAPI(title="Who Sings That?", lifespan=lifespan)

    @application.get("/", include_in_schema=False)
    async def production_page() -> FileResponse:
        return FileResponse(
            _STATIC_DIRECTORY / "index.html",
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/audio-test", include_in_schema=False)
    async def audio_test_page() -> FileResponse:
        return FileResponse(
            _STATIC_DIRECTORY / "audio-test.html",
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/api/playlists", include_in_schema=False)
    async def diagnostic_playlist_metadata(request: Request) -> JSONResponse:
        playlists = list_playlists(request.app.state.database_engine)
        return JSONResponse(
            {
                "playlists": [
                    {
                        "playlist_id": playlist.playlist_id,
                        "name": playlist.name,
                        "song_count": playlist.song_count,
                    }
                    for playlist in playlists
                ]
            },
            headers={"Cache-Control": "no-store"},
        )

    @application.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await websocket.app.state.websocket_transport.handle(websocket)

    @application.get("/media/{opaque_capability}", include_in_schema=False)
    async def protected_media_endpoint(
        request: Request,
        opaque_capability: str,
    ):
        return await media_response(request, opaque_capability)

    application.mount(
        "/static",
        StaticFiles(directory=_STATIC_DIRECTORY),
        name="static",
    )

    return application


def _default_controller_factory(
    database_engine: Engine,
    settings: ServerSettings,
) -> GameController:
    return GameController(
        database_engine,
        settings.music_root,
        media_capability_lifetime_seconds=(settings.media_capability_lifetime_seconds),
    )


logging.getLogger(__name__).addHandler(logging.NullHandler())
app = create_app()


__all__ = ["ControllerFactory", "app", "create_app"]
