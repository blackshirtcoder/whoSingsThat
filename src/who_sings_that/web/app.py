"""Importable FastAPI application and lifespan construction."""

import logging
from collections.abc import Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket
from sqlalchemy import Engine

from who_sings_that.application import GameController
from who_sings_that.database import (
    create_sqlite_engine,
    upgrade_database,
)

from .config import ServerSettings
from .transport import WebSocketTransport

ControllerFactory = Callable[[Engine, ServerSettings], GameController]


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

    @application.websocket("/ws")
    async def websocket_endpoint(websocket: WebSocket) -> None:
        await websocket.app.state.websocket_transport.handle(websocket)

    return application


def _default_controller_factory(
    database_engine: Engine,
    settings: ServerSettings,
) -> GameController:
    return GameController(database_engine, settings.music_root)


logging.getLogger(__name__).addHandler(logging.NullHandler())
app = create_app()


__all__ = ["ControllerFactory", "app", "create_app"]
