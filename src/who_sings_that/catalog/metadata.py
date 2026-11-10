"""Filename-first metadata and structural MP3 inspection."""

from os import PathLike
from pathlib import Path

from mutagen import MutagenError
from mutagen.id3 import ID3, ID3NoHeaderError
from mutagen.mp3 import MP3, HeaderNotFoundError

from .models import (
    CatalogProblem,
    CatalogProblemCode,
    MediaStatus,
    MetadataSource,
    MetadataStatus,
    TrackMetadata,
)


def parse_filename_metadata(filename: str | Path) -> tuple[str, str] | None:
    """Parse ``Artist - Title`` using only the first exact delimiter.

    The final extension is removed before parsing. Both trimmed fields must be
    non-empty for filename metadata to be usable.
    """

    stem = Path(filename).stem
    if " - " not in stem:
        return None

    artist, title = stem.split(" - ", 1)
    artist = artist.strip()
    title = title.strip()
    if not artist or not title:
        return None
    return artist, title


def extract_track_metadata(
    music_root: str | PathLike[str],
    relative_path: str | PathLike[str],
) -> TrackMetadata:
    """Inspect one root-relative MP3 path without allowing per-track failures out.

    File access and Mutagen errors are represented in the returned record. The
    caller can therefore continue inspecting other discovered paths.
    """

    root = Path(music_root)
    relative = _validated_relative_path(relative_path)
    absolute_path = root / relative
    problems: list[CatalogProblem] = []

    filename_metadata = parse_filename_metadata(relative.name)
    artist: str | None = None
    title: str | None = None
    metadata_source = MetadataSource.UNAVAILABLE
    if filename_metadata is not None:
        artist, title = filename_metadata
        metadata_source = MetadataSource.FILENAME

    try:
        file_size_bytes = absolute_path.stat().st_size
    except FileNotFoundError:
        return _unavailable_file_result(
            relative,
            artist,
            title,
            metadata_source,
            MediaStatus.MISSING,
            CatalogProblemCode.FILE_MISSING,
            "file disappeared before it could be inspected",
        )
    except OSError as error:
        return _unavailable_file_result(
            relative,
            artist,
            title,
            metadata_source,
            MediaStatus.UNREADABLE,
            CatalogProblemCode.FILE_UNREADABLE,
            f"file attributes could not be read ({type(error).__name__})",
        )

    if filename_metadata is None:
        artist, title, id3_problem = _read_id3_metadata(absolute_path)
        if artist is not None or title is not None:
            metadata_source = MetadataSource.ID3
        if id3_problem is not None:
            problems.append(id3_problem)

    metadata_status = (
        MetadataStatus.COMPLETE
        if _usable_text(artist) and _usable_text(title)
        else MetadataStatus.NEEDS_REVIEW
    )
    if metadata_status is MetadataStatus.NEEDS_REVIEW:
        problems.append(
            CatalogProblem(
                CatalogProblemCode.METADATA_MISSING,
                "a usable artist and title were not available from filename or ID3",
            )
        )

    try:
        duration_seconds = float(MP3(absolute_path).info.length)
        media_status = MediaStatus.VALID
    except FileNotFoundError:
        duration_seconds = None
        file_size_bytes = None
        media_status = MediaStatus.MISSING
        problems.append(
            CatalogProblem(
                CatalogProblemCode.FILE_MISSING,
                "file disappeared during MP3 inspection",
            )
        )
    except PermissionError as error:
        duration_seconds = None
        media_status = MediaStatus.UNREADABLE
        problems.append(
            CatalogProblem(
                CatalogProblemCode.FILE_UNREADABLE,
                f"MP3 data could not be read ({type(error).__name__})",
            )
        )
    except OSError as error:
        duration_seconds = None
        media_status = MediaStatus.UNREADABLE
        problems.append(
            CatalogProblem(
                CatalogProblemCode.FILE_UNREADABLE,
                f"MP3 data could not be read ({type(error).__name__})",
            )
        )
    except (HeaderNotFoundError, MutagenError) as error:
        duration_seconds = None
        media_status = MediaStatus.INVALID
        problems.append(
            CatalogProblem(
                CatalogProblemCode.INVALID_MP3,
                f"MP3 structure could not be inspected ({type(error).__name__})",
            )
        )

    return TrackMetadata(
        relative_path=relative,
        artist=artist,
        title=title,
        duration_seconds=duration_seconds,
        file_size_bytes=file_size_bytes,
        metadata_source=metadata_source,
        metadata_status=metadata_status,
        media_status=media_status,
        problems=tuple(problems),
    )


def _read_id3_metadata(
    path: Path,
) -> tuple[str | None, str | None, CatalogProblem | None]:
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        return None, None, None
    except FileNotFoundError:
        return (
            None,
            None,
            CatalogProblem(
                CatalogProblemCode.METADATA_UNREADABLE,
                "file disappeared while ID3 metadata was being read",
            ),
        )
    except (OSError, MutagenError) as error:
        return (
            None,
            None,
            CatalogProblem(
                CatalogProblemCode.METADATA_UNREADABLE,
                f"ID3 metadata could not be read ({type(error).__name__})",
            ),
        )

    return _tag_text(tags.get("TPE1")), _tag_text(tags.get("TIT2")), None


def _tag_text(frame: object | None) -> str | None:
    if frame is None:
        return None

    values = getattr(frame, "text", ())
    for value in values:
        text = str(value).strip()
        if text:
            return text
    return None


def _usable_text(value: str | None) -> bool:
    return value is not None and bool(value.strip())


def _validated_relative_path(relative_path: str | PathLike[str]) -> Path:
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("relative_path must stay beneath the music root")
    if relative == Path("."):
        raise ValueError("relative_path must identify a file")
    return relative


def _unavailable_file_result(
    relative_path: Path,
    artist: str | None,
    title: str | None,
    metadata_source: MetadataSource,
    media_status: MediaStatus,
    problem_code: CatalogProblemCode,
    problem_message: str,
) -> TrackMetadata:
    metadata_status = (
        MetadataStatus.COMPLETE
        if _usable_text(artist) and _usable_text(title)
        else MetadataStatus.NEEDS_REVIEW
    )
    problems = [CatalogProblem(problem_code, problem_message)]
    if metadata_status is MetadataStatus.NEEDS_REVIEW:
        problems.append(
            CatalogProblem(
                CatalogProblemCode.METADATA_MISSING,
                "a usable artist and title were not available from the filename",
            )
        )

    return TrackMetadata(
        relative_path=relative_path,
        artist=artist,
        title=title,
        duration_seconds=None,
        file_size_bytes=None,
        metadata_source=metadata_source,
        metadata_status=metadata_status,
        media_status=media_status,
        problems=tuple(problems),
    )
