"""Music discovery and metadata extraction, separate from game-domain logic."""

from .discovery import discover_mp3_files
from .metadata import extract_track_metadata, parse_filename_metadata
from .models import (
    CatalogProblem,
    CatalogProblemCode,
    MediaStatus,
    MetadataSource,
    MetadataStatus,
    TrackMetadata,
)
from .scan import scan_music_library

__all__ = [
    "CatalogProblem",
    "CatalogProblemCode",
    "MediaStatus",
    "MetadataSource",
    "MetadataStatus",
    "TrackMetadata",
    "discover_mp3_files",
    "extract_track_metadata",
    "parse_filename_metadata",
    "scan_music_library",
]
