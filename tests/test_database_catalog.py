from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine, Integer, inspect, text
from sqlalchemy.exc import IntegrityError

from who_sings_that.catalog import (
    CatalogProblem,
    CatalogProblemCode,
    MediaStatus,
    MetadataSource,
    MetadataStatus,
    TrackMetadata,
)
from who_sings_that.database import (
    DuplicateTrackPathError,
    create_sqlite_engine,
    get_track_by_id,
    get_track_by_relative_path,
    import_tracks,
    list_tracks,
    upgrade_database,
)


@pytest.fixture
def migrated_database(tmp_path: Path) -> Iterator[tuple[Path, Engine]]:
    database_path = tmp_path / "catalog.sqlite3"
    upgrade_database(database_path)
    engine = create_sqlite_engine(database_path)
    yield database_path, engine
    engine.dispose()


def complete_track(
    relative_path: str = "Rock/Artist - Title.mp3",
    *,
    duration_seconds: float = 183.625,
) -> TrackMetadata:
    return TrackMetadata(
        relative_path=Path(relative_path),
        artist="Artist",
        title="Title",
        duration_seconds=duration_seconds,
        file_size_bytes=4_321_987,
        metadata_source=MetadataSource.FILENAME,
        metadata_status=MetadataStatus.COMPLETE,
        media_status=MediaStatus.VALID,
    )


def test_alembic_head_creates_versioned_catalog_schema(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "nested name.sqlite3"

    upgrade_database(database_path)
    engine = create_sqlite_engine(database_path)
    database_inspector = inspect(engine)

    assert set(database_inspector.get_table_names()) == {
        "alembic_version",
        "completed_game_deck_entries",
        "completed_game_participants",
        "completed_game_playlists",
        "completed_game_songmaster_events",
        "completed_games",
        "playlist_tracks",
        "playlists",
        "tracks",
    }
    assert {column["name"] for column in database_inspector.get_columns("tracks")} == {
        "track_id",
        "created_at",
        "relative_path",
        "artist",
        "title",
        "duration_seconds",
        "file_size_bytes",
        "metadata_source",
        "metadata_status",
        "media_status",
        "problems",
        "enabled",
    }
    assert database_inspector.get_pk_constraint("tracks")["constrained_columns"] == [
        "track_id"
    ]
    track_id_column = next(
        column
        for column in database_inspector.get_columns("tracks")
        if column["name"] == "track_id"
    )
    assert isinstance(track_id_column["type"], Integer)
    assert {constraint["name"] for constraint in database_inspector.get_unique_constraints(
        "tracks"
    )} == {"uq_tracks_relative_path"}
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0003_create_completed_game_history"
        )
        create_statement = connection.scalar(
            text("SELECT sql FROM sqlite_master WHERE name = 'tracks'")
        )
        assert "PRIMARY KEY AUTOINCREMENT" in create_statement.upper()
    engine.dispose()


def test_imports_and_retrieves_complete_track_metadata(
    migrated_database: tuple[Path, Engine],
) -> None:
    _, engine = migrated_database
    metadata = complete_track()

    imported = import_tracks(engine, [metadata])

    assert len(imported) == 1
    stored = imported[0]
    assert stored.track_id == 1
    assert isinstance(stored.track_id, int)
    assert stored.created_at.tzinfo is UTC
    assert stored.relative_path == "Rock/Artist - Title.mp3"
    assert stored.artist == "Artist"
    assert stored.title == "Title"
    assert stored.duration_seconds == 183.625
    assert stored.file_size_bytes == 4_321_987
    assert stored.metadata_source is MetadataSource.FILENAME
    assert stored.metadata_status is MetadataStatus.COMPLETE
    assert stored.media_status is MediaStatus.VALID
    assert stored.problems == ()
    assert stored.enabled is True
    assert stored.is_catalog_eligible is True
    assert get_track_by_id(engine, stored.track_id) == stored
    assert get_track_by_relative_path(engine, stored.relative_path) == stored
    assert list_tracks(engine) == (stored,)


def test_stores_incomplete_metadata_and_invalid_media_without_fabricated_values(
    migrated_database: tuple[Path, Engine],
) -> None:
    _, engine = migrated_database
    problems = (
        CatalogProblem(
            CatalogProblemCode.METADATA_MISSING,
            "artist and title unavailable",
        ),
        CatalogProblem(
            CatalogProblemCode.INVALID_MP3,
            "MP3 headers unavailable",
        ),
    )
    metadata = TrackMetadata(
        relative_path=Path("Review/unknown.mp3"),
        artist=None,
        title=None,
        duration_seconds=None,
        file_size_bytes=None,
        metadata_source=MetadataSource.UNAVAILABLE,
        metadata_status=MetadataStatus.NEEDS_REVIEW,
        media_status=MediaStatus.INVALID,
        problems=problems,
    )

    (stored,) = import_tracks(engine, [metadata])
    retrieved = get_track_by_id(engine, stored.track_id)

    assert retrieved is not None
    assert retrieved.artist is None
    assert retrieved.title is None
    assert retrieved.duration_seconds is None
    assert retrieved.file_size_bytes is None
    assert retrieved.problems == problems
    assert retrieved.enabled is False
    assert retrieved.is_catalog_eligible is False


def test_incomplete_valid_record_starts_disabled(
    migrated_database: tuple[Path, Engine],
) -> None:
    _, engine = migrated_database
    metadata = TrackMetadata(
        relative_path=Path("Review/Artist - Missing Size.mp3"),
        artist="Artist",
        title="Missing Size",
        duration_seconds=180.5,
        file_size_bytes=None,
        metadata_source=MetadataSource.FILENAME,
        metadata_status=MetadataStatus.COMPLETE,
        media_status=MediaStatus.VALID,
    )

    (stored,) = import_tracks(engine, [metadata])

    assert stored.file_size_bytes is None
    assert stored.enabled is False
    assert stored.is_catalog_eligible is False


def test_generated_track_id_and_data_survive_database_reopen(
    migrated_database: tuple[Path, Engine],
) -> None:
    database_path, engine = migrated_database
    (imported,) = import_tracks(engine, [complete_track()])
    track_id = imported.track_id
    engine.dispose()

    reopened_engine = create_sqlite_engine(database_path)
    retrieved = get_track_by_id(reopened_engine, track_id)

    assert retrieved == imported
    assert retrieved is not None
    assert retrieved.track_id == track_id
    assert retrieved.created_at == imported.created_at
    assert retrieved.created_at.tzinfo is UTC
    reopened_engine.dispose()


def test_created_at_is_unambiguous_utc_with_microsecond_round_trip(
    migrated_database: tuple[Path, Engine],
) -> None:
    _, engine = migrated_database
    before_import = datetime.now(UTC)

    (stored,) = import_tracks(engine, [complete_track()])
    after_import = datetime.now(UTC)
    retrieved = get_track_by_id(engine, stored.track_id)

    assert before_import <= stored.created_at <= after_import
    assert stored.created_at.tzinfo is UTC
    assert retrieved is not None
    assert retrieved.created_at == stored.created_at
    assert retrieved.created_at.tzinfo is UTC
    with engine.connect() as connection:
        raw_created_at = connection.scalar(
            text("SELECT created_at FROM tracks WHERE track_id = :track_id"),
            {"track_id": stored.track_id},
        )
    assert raw_created_at == stored.created_at.isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def test_one_import_batch_shares_one_creation_timestamp(
    migrated_database: tuple[Path, Engine],
) -> None:
    _, engine = migrated_database

    imported = import_tracks(
        engine,
        [
            complete_track("Rock/A - First.mp3"),
            complete_track("Rock/B - Second.mp3"),
            complete_track("Rock/C - Third.mp3"),
        ],
    )

    assert [track.track_id for track in imported] == [1, 2, 3]
    assert len({track.created_at for track in imported}) == 1
    assert imported[0].created_at.tzinfo is UTC


def test_committed_track_ids_are_not_reused_after_deletion(
    migrated_database: tuple[Path, Engine],
) -> None:
    _, engine = migrated_database
    (deleted_track,) = import_tracks(engine, [complete_track("Rock/A - Deleted.mp3")])
    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM tracks WHERE track_id = :track_id"),
            {"track_id": deleted_track.track_id},
        )

    (replacement,) = import_tracks(engine, [complete_track("Rock/B - Replacement.mp3")])

    assert deleted_track.track_id == 1
    assert replacement.track_id == 2
    assert replacement.track_id > deleted_track.track_id


def test_relative_path_and_fractional_duration_are_preserved_exactly(
    migrated_database: tuple[Path, Engine],
) -> None:
    _, engine = migrated_database
    relative_path = (
        "Rock/#/Björk & 38 Special/"
        "38 Special - Against the Night (Live)!.mp3"
    )
    metadata = complete_track(relative_path, duration_seconds=201.123456789)

    (stored,) = import_tracks(engine, [metadata])
    retrieved = get_track_by_relative_path(engine, relative_path)

    assert stored.relative_path == relative_path
    assert "\\" not in stored.relative_path
    assert retrieved is not None
    assert retrieved.relative_path == relative_path
    assert retrieved.duration_seconds == pytest.approx(201.123456789)


def test_retrieval_does_not_require_source_music_files(
    migrated_database: tuple[Path, Engine], tmp_path: Path
) -> None:
    _, engine = migrated_database
    relative_path = "Unmounted NAS/Artist - Offline Song.mp3"
    source_path = tmp_path / relative_path
    assert not source_path.exists()
    (imported,) = import_tracks(engine, [complete_track(relative_path)])

    retrieved = get_track_by_id(engine, imported.track_id)

    assert retrieved == imported
    assert not source_path.exists()


def test_reimporting_existing_path_preserves_id_and_creation_timestamp(
    migrated_database: tuple[Path, Engine],
) -> None:
    _, engine = migrated_database
    metadata = complete_track()
    (original,) = import_tracks(engine, [metadata])
    original_created_at = original.created_at

    with pytest.raises(DuplicateTrackPathError) as raised:
        import_tracks(engine, [metadata])

    assert raised.value.relative_path == metadata.relative_path.as_posix()
    assert raised.value.existing_track_id == original.track_id
    (retrieved,) = list_tracks(engine)
    assert retrieved == original
    assert retrieved.track_id == original.track_id
    assert retrieved.created_at == original_created_at


def test_duplicate_within_one_import_rolls_back_the_whole_batch(
    migrated_database: tuple[Path, Engine],
) -> None:
    _, engine = migrated_database
    metadata = complete_track()

    with pytest.raises(DuplicateTrackPathError):
        import_tracks(engine, [metadata, metadata])

    assert list_tracks(engine) == ()


def test_database_constraint_failure_rolls_back_the_whole_import(
    migrated_database: tuple[Path, Engine],
) -> None:
    _, engine = migrated_database
    valid = complete_track("Rock/A - Valid.mp3")
    invalid = complete_track("Rock/B - Invalid Duration.mp3", duration_seconds=-1)

    with pytest.raises(IntegrityError):
        import_tracks(engine, [valid, invalid])

    assert list_tracks(engine) == ()
