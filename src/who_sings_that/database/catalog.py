"""Transactional persistence and retrieval of catalog tracks."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from os import PathLike
from pathlib import Path

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from who_sings_that.catalog import (
    CatalogProblem,
    CatalogProblemCode,
    MediaStatus,
    MetadataSource,
    MetadataStatus,
    TrackMetadata,
)

from .models import TrackRow


class DuplicateTrackPathError(ValueError):
    """Raised when an initial import encounters an already-stored path."""

    def __init__(self, relative_path: str, existing_track_id: int | None = None):
        message = f"relative path is already present in the catalog: {relative_path}"
        if existing_track_id is not None:
            message += f" (track_id={existing_track_id})"
        super().__init__(message)
        self.relative_path = relative_path
        self.existing_track_id = existing_track_id


@dataclass(frozen=True, slots=True)
class StoredTrack:
    """A detached, filesystem-independent view of one stored track."""

    track_id: int
    created_at: datetime
    relative_path: str
    artist: str | None
    title: str | None
    duration_seconds: float | None
    file_size_bytes: int | None
    metadata_source: MetadataSource
    metadata_status: MetadataStatus
    media_status: MediaStatus
    problems: tuple[CatalogProblem, ...]
    enabled: bool

    @property
    def is_catalog_eligible(self) -> bool:
        """Return whether stored facts permit later live gameplay validation.

        This does not prove the source file remains present or playable. Start
        Game must still perform its required live filesystem/media validation.
        """

        return (
            self.enabled
            and self.metadata_status is MetadataStatus.COMPLETE
            and self.media_status is MediaStatus.VALID
            and self.artist is not None
            and self.title is not None
            and self.duration_seconds is not None
            and self.file_size_bytes is not None
        )


def import_tracks(
    engine: Engine,
    tracks: Iterable[TrackMetadata],
) -> tuple[StoredTrack, ...]:
    """Atomically insert new track metadata, rejecting every duplicate path.

    This initial-import operation never updates or replaces an existing row. A
    duplicate already in the database or repeated within the input raises
    ``DuplicateTrackPathError`` and rolls back every row from this call.
    """

    batch_created_at = datetime.now(UTC)
    imported_rows: list[TrackRow] = []
    with Session(engine) as session, session.begin():
        existing_paths = dict(
            session.execute(select(TrackRow.relative_path, TrackRow.track_id)).all()
        )
        batch_paths: set[str] = set()

        for metadata in tracks:
            relative_path = _portable_relative_path(metadata.relative_path)
            if relative_path in existing_paths:
                raise DuplicateTrackPathError(
                    relative_path,
                    existing_paths[relative_path],
                )
            if relative_path in batch_paths:
                raise DuplicateTrackPathError(relative_path)

            batch_paths.add(relative_path)
            enabled = _initial_enabled_status(metadata)
            problems_payload = [
                {"code": problem.code.value, "message": problem.message}
                for problem in metadata.problems
            ]
            row = TrackRow(
                created_at=batch_created_at,
                relative_path=relative_path,
                artist=metadata.artist,
                title=metadata.title,
                duration_seconds=metadata.duration_seconds,
                file_size_bytes=metadata.file_size_bytes,
                metadata_source=metadata.metadata_source.value,
                metadata_status=metadata.metadata_status.value,
                media_status=metadata.media_status.value,
                problems=problems_payload,
                enabled=enabled,
            )
            session.add(row)
            imported_rows.append(row)

        session.flush()
        imported = tuple(_stored_track_from_row(row) for row in imported_rows)

    return imported


def get_track_by_id(engine: Engine, track_id: int) -> StoredTrack | None:
    """Retrieve one stored track by stable identity without touching music files."""

    with Session(engine) as session:
        row = session.get(TrackRow, track_id)
        return None if row is None else _stored_track_from_row(row)


def get_track_by_relative_path(
    engine: Engine,
    relative_path: str | PathLike[str],
) -> StoredTrack | None:
    """Retrieve one stored track by its portable relative path."""

    portable_path = _portable_relative_path(relative_path)
    with Session(engine) as session:
        row = session.scalar(
            select(TrackRow).where(TrackRow.relative_path == portable_path)
        )
        return None if row is None else _stored_track_from_row(row)


def list_tracks(engine: Engine) -> tuple[StoredTrack, ...]:
    """Return all stored tracks in relative-path order without filesystem access."""

    with Session(engine) as session:
        rows = session.scalars(select(TrackRow).order_by(TrackRow.relative_path)).all()
        return tuple(_stored_track_from_row(row) for row in rows)


def _portable_relative_path(relative_path: str | PathLike[str]) -> str:
    path = Path(relative_path)
    if path.is_absolute() or ".." in path.parts or path == Path("."):
        raise ValueError("relative_path must identify a file beneath the music root")
    return path.as_posix()


def _initial_enabled_status(metadata: TrackMetadata) -> bool:
    return (
        metadata.metadata_status is MetadataStatus.COMPLETE
        and metadata.media_status is MediaStatus.VALID
        and metadata.artist is not None
        and metadata.title is not None
        and metadata.duration_seconds is not None
        and metadata.file_size_bytes is not None
    )


def _stored_track_from_row(row: TrackRow) -> StoredTrack:
    return StoredTrack(
        track_id=row.track_id,
        created_at=row.created_at,
        relative_path=row.relative_path,
        artist=row.artist,
        title=row.title,
        duration_seconds=row.duration_seconds,
        file_size_bytes=row.file_size_bytes,
        metadata_source=MetadataSource(row.metadata_source),
        metadata_status=MetadataStatus(row.metadata_status),
        media_status=MediaStatus(row.media_status),
        problems=tuple(
            CatalogProblem(
                code=CatalogProblemCode(problem["code"]),
                message=problem["message"],
            )
            for problem in row.problems
        ),
        enabled=row.enabled,
    )
