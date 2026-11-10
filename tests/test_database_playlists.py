from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, inspect, text
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
    DuplicatePlaylistNameError,
    InvalidPlaylistNameError,
    PlaylistNotFoundError,
    TrackNotFoundError,
    add_tracks_to_playlist,
    create_playlist,
    create_sqlite_engine,
    delete_playlist,
    get_playlist_by_id,
    get_playlist_tracks,
    get_track_by_id,
    import_tracks,
    list_playlists,
    list_tracks,
    normalize_playlist_name,
    playlist_name_uniqueness_key,
    remove_tracks_from_playlist,
    update_playlist,
    upgrade_database,
)
from who_sings_that.database import migration as migration_module
from who_sings_that.database.engine import sqlite_url
from who_sings_that.database.migration import _alembic_config_value


@pytest.fixture
def migrated_database(tmp_path: Path) -> Iterator[tuple[Path, Engine]]:
    database_path = tmp_path / "playlists.sqlite3"
    upgrade_database(database_path)
    engine = create_sqlite_engine(database_path)
    yield database_path, engine
    engine.dispose()


def track_metadata(
    relative_path: str,
    *,
    artist: str | None = "Artist",
    title: str | None = "Title",
    media_status: MediaStatus = MediaStatus.VALID,
) -> TrackMetadata:
    complete = artist is not None and title is not None
    return TrackMetadata(
        relative_path=Path(relative_path),
        artist=artist,
        title=title,
        duration_seconds=180.5 if complete else None,
        file_size_bytes=1_234 if complete else None,
        metadata_source=(
            MetadataSource.FILENAME if complete else MetadataSource.UNAVAILABLE
        ),
        metadata_status=(
            MetadataStatus.COMPLETE if complete else MetadataStatus.NEEDS_REVIEW
        ),
        media_status=media_status,
        problems=(
            ()
            if complete
            else (
                CatalogProblem(
                    CatalogProblemCode.METADATA_MISSING,
                    "metadata unavailable",
                ),
            )
        ),
    )


def import_three_tracks(engine: Engine) -> tuple[int, int, int]:
    tracks = import_tracks(
        engine,
        [
            track_metadata("Music/A - One.mp3", artist="A", title="One"),
            track_metadata("Music/B - Two.mp3", artist="B", title="Two"),
            track_metadata("Music/C - Three.mp3", artist="C", title="Three"),
        ],
    )
    return tuple(track.track_id for track in tracks)  # type: ignore[return-value]


def upgrade_to_revision(database_path: Path, revision: str) -> None:
    migrations_path = Path(migration_module.__file__).with_name("migrations")
    config = Config()
    config.set_main_option("script_location", str(migrations_path))
    config.set_main_option(
        "sqlalchemy.url",
        _alembic_config_value(
            sqlite_url(database_path).render_as_string(hide_password=False)
        ),
    )
    command.upgrade(config, revision)


def test_playlist_migration_schema_constraints_and_indexes(tmp_path: Path) -> None:
    database_path = tmp_path / "fresh.sqlite3"
    upgrade_database(database_path)
    engine = create_sqlite_engine(database_path)
    database_inspector = inspect(engine)

    assert set(database_inspector.get_table_names()) == {
        "alembic_version",
        "playlist_tracks",
        "playlists",
        "tracks",
    }
    assert {column["name"] for column in database_inspector.get_columns("playlists")} == {
        "created_at",
        "description",
        "name",
        "name_key",
        "playlist_id",
    }
    assert database_inspector.get_pk_constraint("playlists")["constrained_columns"] == [
        "playlist_id"
    ]
    assert database_inspector.get_pk_constraint("playlist_tracks")[
        "constrained_columns"
    ] == ["playlist_id", "track_id"]
    assert {constraint["name"] for constraint in database_inspector.get_unique_constraints(
        "playlists"
    )} == {"uq_playlists_name_key"}
    assert {index["name"] for index in database_inspector.get_indexes(
        "playlist_tracks"
    )} == {"ix_playlist_tracks_track_id"}
    foreign_keys = {
        foreign_key["constrained_columns"][0]: foreign_key
        for foreign_key in database_inspector.get_foreign_keys("playlist_tracks")
    }
    assert foreign_keys["playlist_id"]["referred_table"] == "playlists"
    assert foreign_keys["playlist_id"]["options"] == {"ondelete": "CASCADE"}
    assert foreign_keys["track_id"]["referred_table"] == "tracks"
    assert foreign_keys["track_id"]["options"] == {"ondelete": "RESTRICT"}
    engine.dispose()


def test_upgrade_from_track_only_schema_preserves_tracks_exactly(tmp_path: Path) -> None:
    database_path = tmp_path / "upgrade.sqlite3"
    upgrade_to_revision(database_path, "0001_create_tracks")
    engine = create_sqlite_engine(database_path)
    imported = import_tracks(
        engine,
        [
            track_metadata("Old/A - One.mp3", artist="A", title="One"),
            track_metadata("Old/B - Two.mp3", artist="B", title="Two"),
        ],
    )
    with engine.connect() as connection:
        original_rows = connection.execute(
            text("SELECT * FROM tracks ORDER BY track_id")
        ).mappings().all()
    engine.dispose()

    upgrade_database(database_path)
    upgraded_engine = create_sqlite_engine(database_path)

    assert list_tracks(upgraded_engine) == imported
    with upgraded_engine.connect() as connection:
        upgraded_rows = connection.execute(
            text("SELECT * FROM tracks ORDER BY track_id")
        ).mappings().all()
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0002_create_playlists"
        )
    assert upgraded_rows == original_rows
    assert list_playlists(upgraded_engine) == ()
    upgraded_engine.dispose()


def test_create_retrieve_and_list_empty_playlists(
    migrated_database: tuple[Path, Engine],
) -> None:
    _, engine = migrated_database
    before = datetime.now(UTC)

    road_trip = create_playlist(engine, "  Road   Trip  ", "Long drives")
    after = datetime.now(UTC)
    acoustic = create_playlist(engine, "Acoustic")

    assert UUID(road_trip.playlist_id).version == 4
    assert before <= road_trip.created_at <= after
    assert road_trip.created_at.tzinfo is UTC
    assert road_trip.name == "Road Trip"
    assert road_trip.description == "Long drives"
    assert road_trip.song_count == 0
    assert get_playlist_by_id(engine, road_trip.playlist_id) == road_trip
    assert get_playlist_tracks(engine, road_trip.playlist_id) == ()
    assert list_playlists(engine) == (acoustic, road_trip)
    assert get_playlist_by_id(engine, "missing") is None
    with pytest.raises(FrozenInstanceError):
        road_trip.name = "Changed"  # type: ignore[misc]


def test_playlist_created_at_round_trips_as_utc_text(
    migrated_database: tuple[Path, Engine],
) -> None:
    _, engine = migrated_database
    playlist = create_playlist(engine, "Road Trip")

    retrieved = get_playlist_by_id(engine, playlist.playlist_id)
    with engine.connect() as connection:
        raw_created_at = connection.scalar(
            text("SELECT created_at FROM playlists WHERE playlist_id = :playlist_id"),
            {"playlist_id": playlist.playlist_id},
        )

    assert retrieved is not None
    assert retrieved.created_at == playlist.created_at
    assert retrieved.created_at.tzinfo is UTC
    assert raw_created_at == playlist.created_at.isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


@pytest.mark.parametrize(
    ("entered", "displayed"),
    [
        ("  Road   Trip  ", "Road Trip"),
        ("Été", "Été"),
        ("李雷 2", "李雷 2"),
        ("DJ's-Hits_2.0", "DJ's-Hits_2.0"),
        ("Ｒｏｃｋ", "Rock"),
        ("Road\t\nTrip", "Road Trip"),
    ],
)
def test_playlist_name_display_normalization(entered: str, displayed: str) -> None:
    assert normalize_playlist_name(entered) == displayed


def test_playlist_name_key_is_unicode_normalized_and_case_folded() -> None:
    assert playlist_name_uniqueness_key(" STRASSE ") == (
        playlist_name_uniqueness_key("Straße")
    )
    assert playlist_name_uniqueness_key("Cafe\N{COMBINING ACUTE ACCENT}") == (
        playlist_name_uniqueness_key("CAFÉ")
    )


@pytest.mark.parametrize(
    "name",
    ["", "   ", "Bad!", "Bad/Name", "Bad🙂", "a" * 33],
)
def test_invalid_playlist_names_are_rejected(
    migrated_database: tuple[Path, Engine], name: str
) -> None:
    _, engine = migrated_database

    with pytest.raises(InvalidPlaylistNameError):
        create_playlist(engine, name)


def test_playlist_name_limit_is_applied_after_normalization(
    migrated_database: tuple[Path, Engine],
) -> None:
    _, engine = migrated_database
    name = f"{'a' * 16}   {'b' * 15}"

    playlist = create_playlist(engine, name)

    assert playlist.name == f"{'a' * 16} {'b' * 15}"
    assert len(playlist.name) == 32


def test_names_are_unique_by_normalized_key_but_own_key_can_be_retained(
    migrated_database: tuple[Path, Engine],
) -> None:
    _, engine = migrated_database
    first = create_playlist(engine, "Straße")
    second = create_playlist(engine, "Other")

    with pytest.raises(DuplicatePlaylistNameError):
        create_playlist(engine, " STRASSE ")
    unchanged = update_playlist(engine, first.playlist_id, name="STRASSE")
    with pytest.raises(DuplicatePlaylistNameError):
        update_playlist(engine, second.playlist_id, name="straße")

    assert unchanged.playlist_id == first.playlist_id
    assert unchanged.created_at == first.created_at
    assert unchanged.name == "STRASSE"
    assert get_playlist_by_id(engine, second.playlist_id) == second


def test_edit_preserves_identity_timestamp_and_membership_and_can_clear_description(
    migrated_database: tuple[Path, Engine],
) -> None:
    _, engine = migrated_database
    (track_id, _, _) = import_three_tracks(engine)
    original = create_playlist(engine, "Original", "Before")
    add_tracks_to_playlist(engine, original.playlist_id, [track_id])

    renamed = update_playlist(engine, original.playlist_id, name="Renamed")
    cleared = update_playlist(engine, original.playlist_id, description=None)

    assert renamed.playlist_id == original.playlist_id
    assert renamed.created_at == original.created_at
    assert renamed.name == "Renamed"
    assert renamed.description == "Before"
    assert renamed.song_count == 1
    assert cleared.playlist_id == original.playlist_id
    assert cleared.created_at == original.created_at
    assert cleared.name == "Renamed"
    assert cleared.description is None
    assert cleared.song_count == 1


def test_deleted_name_can_be_reused_with_new_identity_and_timestamp(
    migrated_database: tuple[Path, Engine],
) -> None:
    _, engine = migrated_database
    original = create_playlist(engine, "Reusable")
    delete_playlist(engine, original.playlist_id)

    replacement = create_playlist(engine, "REUSABLE")

    assert replacement.playlist_id != original.playlist_id
    assert replacement.created_at >= original.created_at
    assert get_playlist_by_id(engine, original.playlist_id) is None


def test_add_remove_multiple_tracks_is_idempotent_and_updates_counts(
    migrated_database: tuple[Path, Engine],
) -> None:
    _, engine = migrated_database
    first_id, second_id, third_id = import_three_tracks(engine)
    playlist = create_playlist(engine, "Set Semantics")

    added = add_tracks_to_playlist(
        engine,
        playlist.playlist_id,
        [first_id, second_id, first_id],
    )
    added_again = add_tracks_to_playlist(
        engine,
        playlist.playlist_id,
        [first_id, second_id],
    )
    removed = remove_tracks_from_playlist(
        engine,
        playlist.playlist_id,
        [second_id, third_id, second_id],
    )
    removed_again = remove_tracks_from_playlist(
        engine,
        playlist.playlist_id,
        [second_id],
    )

    assert added.song_count == 2
    assert added_again.song_count == 2
    assert removed.song_count == 1
    assert removed_again.song_count == 1
    assert [track.track_id for track in get_playlist_tracks(engine, playlist.playlist_id)] == [
        first_id
    ]
    assert get_playlist_by_id(engine, playlist.playlist_id) == removed_again


def test_track_can_belong_to_multiple_playlists(
    migrated_database: tuple[Path, Engine],
) -> None:
    _, engine = migrated_database
    (track_id, _, _) = import_three_tracks(engine)
    first = create_playlist(engine, "First")
    second = create_playlist(engine, "Second")

    add_tracks_to_playlist(engine, first.playlist_id, [track_id])
    add_tracks_to_playlist(engine, second.playlist_id, [track_id])

    assert get_playlist_tracks(engine, first.playlist_id)[0].track_id == track_id
    assert get_playlist_tracks(engine, second.playlist_id)[0].track_id == track_id
    assert [playlist.song_count for playlist in list_playlists(engine)] == [1, 1]


@pytest.mark.parametrize(
    "operation",
    [
        lambda engine, playlist_id: update_playlist(engine, playlist_id, name="Name"),
        lambda engine, playlist_id: delete_playlist(engine, playlist_id),
        lambda engine, playlist_id: add_tracks_to_playlist(engine, playlist_id, []),
        lambda engine, playlist_id: remove_tracks_from_playlist(engine, playlist_id, []),
        lambda engine, playlist_id: get_playlist_tracks(engine, playlist_id),
    ],
)
def test_mutations_and_member_retrieval_reject_nonexistent_playlist(
    migrated_database: tuple[Path, Engine], operation: object
) -> None:
    _, engine = migrated_database

    with pytest.raises(PlaylistNotFoundError):
        operation(engine, "missing")  # type: ignore[operator]


@pytest.mark.parametrize("operation", [add_tracks_to_playlist, remove_tracks_from_playlist])
def test_membership_operations_reject_unknown_tracks_and_roll_back_atomically(
    migrated_database: tuple[Path, Engine], operation: object
) -> None:
    _, engine = migrated_database
    first_id, second_id, _ = import_three_tracks(engine)
    playlist = create_playlist(engine, "Atomic")
    add_tracks_to_playlist(engine, playlist.playlist_id, [first_id])

    with pytest.raises(TrackNotFoundError) as raised:
        operation(  # type: ignore[operator]
            engine,
            playlist.playlist_id,
            [first_id, second_id, 999_999],
        )

    assert raised.value.track_ids == (999_999,)
    assert [track.track_id for track in get_playlist_tracks(engine, playlist.playlist_id)] == [
        first_id
    ]


def test_application_connections_enable_and_enforce_sqlite_foreign_keys(
    migrated_database: tuple[Path, Engine],
) -> None:
    _, engine = migrated_database
    playlist = create_playlist(engine, "Foreign Keys")

    with engine.connect() as connection:
        assert connection.scalar(text("PRAGMA foreign_keys")) == 1
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO playlist_tracks (playlist_id, track_id) "
                "VALUES (:playlist_id, :track_id)"
            ),
            {"playlist_id": playlist.playlist_id, "track_id": 123_456},
        )


def test_deleting_playlist_cascades_memberships_but_preserves_catalog_and_others(
    migrated_database: tuple[Path, Engine],
) -> None:
    _, engine = migrated_database
    first_id, second_id, third_id = import_three_tracks(engine)
    deleted = create_playlist(engine, "Delete Me")
    retained = create_playlist(engine, "Keep Me")
    add_tracks_to_playlist(engine, deleted.playlist_id, [first_id, second_id])
    add_tracks_to_playlist(engine, retained.playlist_id, [first_id])

    delete_playlist(engine, deleted.playlist_id)

    assert get_playlist_by_id(engine, deleted.playlist_id) is None
    assert [track.track_id for track in get_playlist_tracks(engine, retained.playlist_id)] == [
        first_id
    ]
    assert [track.track_id for track in list_tracks(engine)] == [
        first_id,
        second_id,
        third_id,
    ]
    with engine.connect() as connection:
        assert connection.scalar(
            text(
                "SELECT count(*) FROM playlist_tracks WHERE playlist_id = :playlist_id"
            ),
            {"playlist_id": deleted.playlist_id},
        ) == 0


def test_track_deletion_is_restricted_while_membership_exists(
    migrated_database: tuple[Path, Engine],
) -> None:
    _, engine = migrated_database
    (track_id, _, _) = import_three_tracks(engine)
    playlist = create_playlist(engine, "Protected Track")
    add_tracks_to_playlist(engine, playlist.playlist_id, [track_id])

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            text("DELETE FROM tracks WHERE track_id = :track_id"),
            {"track_id": track_id},
        )

    assert get_track_by_id(engine, track_id) is not None


def test_playlist_and_memberships_survive_database_reopen(
    migrated_database: tuple[Path, Engine],
) -> None:
    database_path, engine = migrated_database
    first_id, second_id, _ = import_three_tracks(engine)
    playlist = create_playlist(engine, "Persistent", "After reopen")
    expected = add_tracks_to_playlist(engine, playlist.playlist_id, [first_id, second_id])
    engine.dispose()

    reopened_engine = create_sqlite_engine(database_path)

    assert get_playlist_by_id(reopened_engine, playlist.playlist_id) == expected
    assert [track.track_id for track in get_playlist_tracks(
        reopened_engine, playlist.playlist_id
    )] == [first_id, second_id]
    reopened_engine.dispose()


def test_member_retrieval_uses_stored_metadata_and_preserves_ineligible_membership(
    migrated_database: tuple[Path, Engine], tmp_path: Path
) -> None:
    _, engine = migrated_database
    relative_path = "Offline/Unknown.mp3"
    source_path = tmp_path / relative_path
    assert not source_path.exists()
    (stored_track,) = import_tracks(engine, [track_metadata(relative_path)])
    playlist = create_playlist(engine, "Needs Review")
    add_tracks_to_playlist(engine, playlist.playlist_id, [stored_track.track_id])
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE tracks SET enabled = 0, media_status = 'missing' "
                "WHERE track_id = :track_id"
            ),
            {"track_id": stored_track.track_id},
        )

    (member,) = get_playlist_tracks(engine, playlist.playlist_id)

    assert member.track_id == stored_track.track_id
    assert member.relative_path == relative_path
    assert member.artist == stored_track.artist
    assert member.title == stored_track.title
    assert member.enabled is False
    assert member.media_status is MediaStatus.MISSING
    assert member.is_catalog_eligible is False
    assert get_playlist_by_id(engine, playlist.playlist_id).song_count == 1  # type: ignore[union-attr]
    assert not source_path.exists()
