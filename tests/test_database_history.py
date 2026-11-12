from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, inspect, select
from sqlalchemy.orm import Session

from who_sings_that.catalog import (
    MediaStatus,
    MetadataSource,
    MetadataStatus,
    TrackMetadata,
)
from who_sings_that.database import (
    CompletedGameHistoryConflictError,
    add_tracks_to_playlist,
    create_playlist,
    create_sqlite_engine,
    delete_playlist,
    get_completed_game_history,
    import_tracks,
    list_completed_games,
    list_playlists,
    list_tracks,
    save_completed_game_history,
    upgrade_database,
)
from who_sings_that.database import migration as migration_module
from who_sings_that.database.engine import sqlite_url
from who_sings_that.database.models import CompletedGameRow, PlaylistRow, TrackRow
from who_sings_that.domain import (
    CompletedGameHistory,
    DeckSkipReason,
    GameEndReason,
    HistoricalDeckEntry,
    HistoricalDeckOutcome,
    HistoricalParticipant,
    HistoricalSongmasterEvent,
    RoundTerminationReason,
    RoundVoidReason,
    ScoringChoice,
    SelectedPlaylistSnapshot,
    SongmasterEventReason,
)

GAME_ID = UUID("10000000-0000-4000-8000-000000000001")
CREATED = datetime(2026, 11, 11, 10, 0, tzinfo=UTC)


def migration_config(database_path: Path) -> Config:
    migrations_path = Path(migration_module.__file__).with_name("migrations")
    config = Config()
    config.set_main_option("script_location", str(migrations_path))
    config.set_main_option(
        "sqlalchemy.url",
        sqlite_url(database_path)
        .render_as_string(hide_password=False)
        .replace("%", "%%"),
    )
    return config


def upgrade_to_revision(database_path: Path, revision: str) -> None:
    command.upgrade(migration_config(database_path), revision)


def history_snapshot(
    *,
    game_id: UUID = GAME_ID,
    ended_at: datetime = CREATED + timedelta(hours=1),
) -> CompletedGameHistory:
    return CompletedGameHistory(
        game_session_id=game_id,
        created_at=CREATED,
        started_at=CREATED + timedelta(minutes=1),
        ended_at=ended_at,
        end_reason=GameEndReason.SONGMASTER_ENDED_GAME,
        playback_offset_seconds=30,
        playback_duration_seconds=30,
        speed_bonus_plateau_percent=Decimal(10),
        participants=(
            HistoricalParticipant(1, 0, "Songmaster", 2, 0),
            HistoricalParticipant(2, 1, "Winner", 1, 50),
        ),
        selected_playlists=(
            SelectedPlaylistSnapshot(
                "20000000-0000-4000-8000-000000000001",
                "Original Playlist",
                CREATED - timedelta(days=1),
                5,
            ),
        ),
        songmaster_events=(
            HistoricalSongmasterEvent(
                1,
                CREATED,
                SongmasterEventReason.INITIAL_ASSIGNMENT,
                None,
                1,
            ),
            HistoricalSongmasterEvent(
                2,
                CREATED + timedelta(minutes=20),
                SongmasterEventReason.MANUAL_TRANSFER,
                1,
                2,
            ),
        ),
        deck_entries=(
            HistoricalDeckEntry(
                1,
                10,
                "First Artist",
                "First Title",
                Decimal("183.625"),
                HistoricalDeckOutcome.COMPLETED,
                round_id=UUID("30000000-0000-4000-8000-000000000001"),
                configured_playback_offset_seconds=30,
                effective_playback_offset_seconds=Decimal(30),
                playback_duration_seconds=30,
                speed_bonus_plateau_percent=Decimal(10),
                playback_started_at=CREATED + timedelta(minutes=2),
                round_termination_reason=RoundTerminationReason.BUZZ,
                buzzing_participant_id=2,
                buzz_elapsed_seconds=Decimal("12.125"),
                scoring_multiplier=Decimal("2.324074074074074074074074074"),
                scoring_choice=ScoringChoice.ARTIST,
                base_score=20,
                awarded_points=46,
                answer_revealed=True,
            ),
            HistoricalDeckEntry(
                2,
                11,
                "Second Artist",
                "Second Title",
                Decimal(200),
                HistoricalDeckOutcome.COMPLETED,
                round_id=UUID("30000000-0000-4000-8000-000000000002"),
                configured_playback_offset_seconds=30,
                effective_playback_offset_seconds=Decimal(30),
                playback_duration_seconds=30,
                speed_bonus_plateau_percent=Decimal(10),
                playback_started_at=CREATED + timedelta(minutes=5),
                round_termination_reason=RoundTerminationReason.PLAYBACK_TIMEOUT,
                awarded_points=0,
                answer_revealed=True,
            ),
            HistoricalDeckEntry(
                3,
                12,
                "Skipped Artist",
                "Skipped Title",
                Decimal(8),
                HistoricalDeckOutcome.SKIPPED,
                skip_reason=DeckSkipReason.DURATION_INELIGIBLE,
                configured_playback_offset_seconds=30,
                playback_duration_seconds=30,
                speed_bonus_plateau_percent=Decimal(10),
            ),
            HistoricalDeckEntry(
                4,
                13,
                "Voided Artist",
                "Voided Title",
                Decimal(180),
                HistoricalDeckOutcome.VOIDED,
                round_id=UUID("30000000-0000-4000-8000-000000000004"),
                configured_playback_offset_seconds=30,
                effective_playback_offset_seconds=Decimal(30),
                playback_duration_seconds=30,
                speed_bonus_plateau_percent=Decimal(10),
                playback_started_at=CREATED + timedelta(minutes=8),
                void_reason=RoundVoidReason.PLAYBACK_CLIENT_LOST,
                lost_playback_client_participant_id=2,
                answer_revealed=False,
            ),
            HistoricalDeckEntry(
                5,
                14,
                "Unplayed Artist",
                "Unplayed Title",
                Decimal(180),
                HistoricalDeckOutcome.UNPLAYED,
            ),
        ),
    )


def test_save_list_reload_and_idempotent_retry_are_durable(tmp_path: Path) -> None:
    database_path = tmp_path / "history.sqlite3"
    upgrade_database(database_path)
    engine = create_sqlite_engine(database_path)
    history = history_snapshot()

    assert save_completed_game_history(engine, history) is True
    assert save_completed_game_history(engine, history) is False
    assert list_completed_games(engine)[0].game_session_id == GAME_ID
    assert get_completed_game_history(engine, GAME_ID) == history
    engine.dispose()

    reopened = create_sqlite_engine(database_path)
    assert get_completed_game_history(reopened, GAME_ID) == history
    with Session(reopened) as session:
        assert session.scalar(select(func.count()).select_from(CompletedGameRow)) == 1
    reopened.dispose()


def test_list_order_is_newest_first_with_uuid_tie_breaker(tmp_path: Path) -> None:
    database_path = tmp_path / "ordered.sqlite3"
    upgrade_database(database_path)
    engine = create_sqlite_engine(database_path)
    later_id = UUID("10000000-0000-4000-8000-000000000003")
    tied_lower_id = UUID("10000000-0000-4000-8000-000000000002")
    save_completed_game_history(engine, history_snapshot())
    save_completed_game_history(
        engine,
        history_snapshot(game_id=later_id, ended_at=CREATED + timedelta(hours=2)),
    )
    save_completed_game_history(
        engine,
        history_snapshot(
            game_id=tied_lower_id,
            ended_at=CREATED + timedelta(hours=2),
        ),
    )

    assert [item.game_session_id for item in list_completed_games(engine)] == [
        tied_lower_id,
        later_id,
        GAME_ID,
    ]
    engine.dispose()


def test_conflicting_duplicate_is_rejected_without_overwrite(tmp_path: Path) -> None:
    database_path = tmp_path / "conflict.sqlite3"
    upgrade_database(database_path)
    engine = create_sqlite_engine(database_path)
    history = history_snapshot()
    save_completed_game_history(engine, history)
    conflicting = replace(history, playback_duration_seconds=99)

    with pytest.raises(CompletedGameHistoryConflictError):
        save_completed_game_history(engine, conflicting)

    assert get_completed_game_history(engine, GAME_ID) == history
    engine.dispose()


def test_mid_transaction_failure_rolls_back_every_history_row(tmp_path: Path) -> None:
    database_path = tmp_path / "rollback.sqlite3"
    upgrade_database(database_path)
    engine = create_sqlite_engine(database_path)

    def fail_after_participants() -> None:
        raise RuntimeError("injected failure")

    with pytest.raises(RuntimeError, match="injected failure"):
        save_completed_game_history(
            engine,
            history_snapshot(),
            _after_participants=fail_after_participants,
        )

    assert get_completed_game_history(engine, GAME_ID) is None
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(CompletedGameRow)) == 0
    engine.dispose()


def test_catalog_edits_and_deletes_cannot_rewrite_history(tmp_path: Path) -> None:
    database_path = tmp_path / "snapshots.sqlite3"
    upgrade_database(database_path)
    engine = create_sqlite_engine(database_path)
    imported = import_tracks(
        engine,
        [
            TrackMetadata(
                relative_path=Path("Original Artist - Original Title.mp3"),
                artist="Original Artist",
                title="Original Title",
                duration_seconds=180,
                file_size_bytes=123,
                metadata_source=MetadataSource.FILENAME,
                metadata_status=MetadataStatus.COMPLETE,
                media_status=MediaStatus.VALID,
            )
        ],
    )[0]
    playlist = create_playlist(engine, "Original Playlist")
    add_tracks_to_playlist(engine, playlist.playlist_id, [imported.track_id])
    base = history_snapshot()
    saved = replace(
        base,
        selected_playlists=(
            SelectedPlaylistSnapshot(
                playlist.playlist_id,
                playlist.name,
                playlist.created_at,
                1,
            ),
        ),
        deck_entries=(
            replace(
                base.deck_entries[0],
                track_id=imported.track_id,
                artist="Original Artist",
                title="Original Title",
            ),
            *base.deck_entries[1:],
        ),
    )
    save_completed_game_history(engine, saved)

    with Session(engine) as session, session.begin():
        track = session.get(TrackRow, imported.track_id)
        stored_playlist = session.get(PlaylistRow, playlist.playlist_id)
        assert track is not None and stored_playlist is not None
        track.artist = "Edited Artist"
        track.title = "Edited Title"
        stored_playlist.name = "Edited Playlist"
    delete_playlist(engine, playlist.playlist_id)
    with Session(engine) as session, session.begin():
        track = session.get(TrackRow, imported.track_id)
        assert track is not None
        session.delete(track)

    loaded = get_completed_game_history(engine, GAME_ID)
    assert loaded == saved
    assert loaded.deck_entries[0].artist == "Original Artist"
    assert loaded.deck_entries[0].title == "Original Title"
    assert loaded.selected_playlists[0].playlist_name == "Original Playlist"
    engine.dispose()


def test_upgrade_from_previous_head_preserves_catalog_and_playlists(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "from-0002.sqlite3"
    upgrade_to_revision(database_path, "0002_create_playlists")
    old_engine = create_sqlite_engine(database_path)
    imported = import_tracks(
        old_engine,
        [
            TrackMetadata(
                relative_path=Path("Artist - Title.mp3"),
                artist="Artist",
                title="Title",
                duration_seconds=180,
                file_size_bytes=123,
                metadata_source=MetadataSource.FILENAME,
                metadata_status=MetadataStatus.COMPLETE,
                media_status=MediaStatus.VALID,
            )
        ],
    )
    playlist = create_playlist(old_engine, "Before History")
    add_tracks_to_playlist(old_engine, playlist.playlist_id, [imported[0].track_id])
    old_engine.dispose()

    upgrade_database(database_path)
    upgraded = create_sqlite_engine(database_path)

    assert list_tracks(upgraded) == imported
    assert list_playlists(upgraded) == (replace(playlist, song_count=1),)
    assert "completed_games" in inspect(upgraded).get_table_names()
    assert list_completed_games(upgraded) == ()
    upgraded.dispose()


def test_history_migration_downgrade_removes_only_history_tables(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "downgrade.sqlite3"
    upgrade_database(database_path)

    command.downgrade(
        migration_config(database_path),
        "0002_create_playlists",
    )
    engine = create_sqlite_engine(database_path)
    table_names = set(inspect(engine).get_table_names())

    assert table_names == {
        "alembic_version",
        "playlist_tracks",
        "playlists",
        "tracks",
    }
    engine.dispose()
