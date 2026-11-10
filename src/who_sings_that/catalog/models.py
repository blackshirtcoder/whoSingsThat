"""Value objects returned by music catalog inspection."""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class MetadataSource(Enum):
    """The source selected for a track's artist and title."""

    FILENAME = "filename"
    ID3 = "id3"
    UNAVAILABLE = "unavailable"


class MetadataStatus(Enum):
    """Whether both required display fields were extracted."""

    COMPLETE = "complete"
    NEEDS_REVIEW = "needs_review"


class MediaStatus(Enum):
    """Result of inspecting the file as an MP3 container."""

    VALID = "valid"
    INVALID = "invalid"
    UNREADABLE = "unreadable"
    MISSING = "missing"


class CatalogProblemCode(Enum):
    """Machine-readable reasons an administrator should review a track."""

    METADATA_MISSING = "metadata_missing"
    METADATA_UNREADABLE = "metadata_unreadable"
    FILE_MISSING = "file_missing"
    FILE_UNREADABLE = "file_unreadable"
    INVALID_MP3 = "invalid_mp3"


@dataclass(frozen=True, slots=True)
class CatalogProblem:
    """One problem encountered while inspecting a discovered track."""

    code: CatalogProblemCode
    message: str


@dataclass(frozen=True, slots=True)
class TrackMetadata:
    """Catalog-ready facts about one discovered MP3 file.

    ``relative_path`` is always relative to the caller's configured music root.
    A ``VALID`` media status means Mutagen could inspect the MP3 structure and
    obtain a duration; it is not a guarantee that every audio frame will decode.
    """

    relative_path: Path
    artist: str | None
    title: str | None
    duration_seconds: float | None
    file_size_bytes: int | None
    metadata_source: MetadataSource
    metadata_status: MetadataStatus
    media_status: MediaStatus
    problems: tuple[CatalogProblem, ...] = ()
