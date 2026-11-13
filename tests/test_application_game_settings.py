from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine

from who_sings_that.application import (
    CreateGameRequest,
    CurrentConnectionRequest,
    GameController,
    SetGameSettingsRequest,
)
from who_sings_that.database import (
    create_sqlite_engine,
    upgrade_database,
)
from who_sings_that.domain import GameSettings, GameSettingsMutationResult


@pytest.fixture
def storage(tmp_path: Path) -> Iterator[tuple[Engine, Path]]:
    database_path = tmp_path / "settings.sqlite3"
    upgrade_database(database_path)
    database_engine = create_sqlite_engine(database_path)
    music_root = tmp_path / "music"
    music_root.mkdir()
    yield database_engine, music_root
    database_engine.dispose()


@pytest.mark.asyncio
async def test_serialized_settings_request_updates_authoritative_projection(
    storage: tuple[Engine, Path],
) -> None:
    database_engine, music_root = storage
    controller = GameController(database_engine, music_root)
    await controller.start()
    try:
        game = await controller.submit_request(
            CreateGameRequest("songmaster-token", "Songmaster", "connection-1")
        )
        before = controller.latest_projection
        assert before is not None

        synchronized = await controller.submit_projected_request(
            CurrentConnectionRequest(
                SetGameSettingsRequest(
                    game.game_session_id,
                    "songmaster-token",
                    GameSettings(7, 42, Decimal("12.5")),
                ),
                "connection-1",
            ),
            "songmaster-token",
        )

        assert isinstance(synchronized.result, GameSettingsMutationResult)
        assert synchronized.result.applied is True
        assert synchronized.update.revision == before.revision + 1
        assert synchronized.update.public is not None
        assert synchronized.update.public.settings == {
            "playback_offset_seconds": 7,
            "playback_duration_seconds": 42,
            "speed_bonus_plateau_percent": "12.5",
        }
        assert "set_game_settings" in synchronized.recipient.recipient.available_songmaster_operations
    finally:
        await controller.stop()

