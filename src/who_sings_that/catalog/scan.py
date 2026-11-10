"""Library-level orchestration for discovery and metadata extraction."""

from os import PathLike

from .discovery import discover_mp3_files
from .metadata import extract_track_metadata
from .models import TrackMetadata


def scan_music_library(music_root: str | PathLike[str]) -> tuple[TrackMetadata, ...]:
    """Discover and inspect every MP3 beneath ``music_root`` in stable order."""

    return tuple(
        extract_track_metadata(music_root, relative_path)
        for relative_path in discover_mp3_files(music_root)
    )
