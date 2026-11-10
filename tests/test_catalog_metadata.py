from pathlib import Path

import pytest
from mutagen.id3 import ID3, TIT2, TPE1

import who_sings_that.catalog.metadata as metadata_module
from who_sings_that.catalog import (
    CatalogProblemCode,
    MediaStatus,
    MetadataSource,
    MetadataStatus,
    extract_track_metadata,
    parse_filename_metadata,
    scan_music_library,
)


def write_test_mp3(
    path: Path,
    *,
    artist: str | None = None,
    title: str | None = None,
) -> None:
    """Write deterministic MPEG frames, optionally prefixed with ID3 metadata."""

    path.parent.mkdir(parents=True, exist_ok=True)
    mpeg1_layer3_128k_44100_header = bytes.fromhex("fffb9064")
    frame = mpeg1_layer3_128k_44100_header + bytes(
        417 - len(mpeg1_layer3_128k_44100_header)
    )
    path.write_bytes(frame * 20)

    if artist is not None or title is not None:
        tags = ID3()
        if artist is not None:
            tags.add(TPE1(encoding=3, text=[artist]))
        if title is not None:
            tags.add(TIT2(encoding=3, text=[title]))
        tags.save(path)


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        (
            "AC-DC - Beating Around the Bush.mp3",
            ("AC-DC", "Beating Around the Bush"),
        ),
        ("Artist - Title - Live - 1999.MP3", ("Artist", "Title - Live - 1999")),
        ("Artist-Title.mp3", None),
        ("Artist – Title.mp3", None),
        (" - Title.mp3", None),
        ("Artist - .mp3", None),
    ],
)
def test_filename_parser_uses_first_exact_delimiter_only(
    filename: str,
    expected: tuple[str, str] | None,
) -> None:
    assert parse_filename_metadata(filename) == expected


def test_filename_metadata_takes_precedence_over_id3(tmp_path: Path) -> None:
    relative_path = Path("Rock/AC-DC - Filename Title.mp3")
    path = tmp_path / relative_path
    write_test_mp3(path, artist="Tag Artist", title="Tag Title")

    result = extract_track_metadata(tmp_path, relative_path)

    assert (result.artist, result.title) == ("AC-DC", "Filename Title")
    assert result.metadata_source is MetadataSource.FILENAME
    assert result.metadata_status is MetadataStatus.COMPLETE


def test_id3_is_used_when_filename_is_not_usable(tmp_path: Path) -> None:
    relative_path = Path("Rock/not in preferred format.mp3")
    path = tmp_path / relative_path
    write_test_mp3(path, artist="ID3 Artist", title="ID3 Title")

    result = extract_track_metadata(tmp_path, relative_path)

    assert (result.artist, result.title) == ("ID3 Artist", "ID3 Title")
    assert result.metadata_source is MetadataSource.ID3
    assert result.metadata_status is MetadataStatus.COMPLETE


@pytest.mark.parametrize(
    ("artist", "title", "expected_source"),
    [
        ("Only Artist", None, MetadataSource.ID3),
        (None, None, MetadataSource.UNAVAILABLE),
        ("   ", "Tag Title", MetadataSource.ID3),
    ],
)
def test_missing_or_unusable_metadata_is_flagged_for_review(
    tmp_path: Path,
    artist: str | None,
    title: str | None,
    expected_source: MetadataSource,
) -> None:
    relative_path = Path("Rock/malformed.mp3")
    write_test_mp3(tmp_path / relative_path, artist=artist, title=title)

    result = extract_track_metadata(tmp_path, relative_path)

    assert result.metadata_status is MetadataStatus.NEEDS_REVIEW
    assert result.metadata_source is expected_source
    assert CatalogProblemCode.METADATA_MISSING in {
        problem.code for problem in result.problems
    }


def test_duration_and_file_size_are_extracted_from_small_synthetic_mp3(
    tmp_path: Path,
) -> None:
    relative_path = Path("Rock/Test Artist - Test Title.mp3")
    path = tmp_path / relative_path
    write_test_mp3(path)

    result = extract_track_metadata(tmp_path, relative_path)

    assert result.duration_seconds == pytest.approx(0.52125)
    assert result.file_size_bytes == path.stat().st_size
    assert result.media_status is MediaStatus.VALID
    assert result.problems == ()


def test_invalid_mp3_is_reported_without_discarding_filename_metadata(
    tmp_path: Path,
) -> None:
    relative_path = Path("Rock/Good Artist - Good Title.mp3")
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True)
    path.write_bytes(b"this is not MPEG audio")

    result = extract_track_metadata(tmp_path, relative_path)

    assert (result.artist, result.title) == ("Good Artist", "Good Title")
    assert result.metadata_status is MetadataStatus.COMPLETE
    assert result.duration_seconds is None
    assert result.media_status is MediaStatus.INVALID
    assert CatalogProblemCode.INVALID_MP3 in {
        problem.code for problem in result.problems
    }


def test_unreadable_mp3_data_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    relative_path = Path("Rock/Artist - Title.mp3")
    path = tmp_path / relative_path
    write_test_mp3(path)

    def deny_access(path: Path) -> None:
        raise PermissionError("test fixture denies media access")

    monkeypatch.setattr(metadata_module, "MP3", deny_access)

    result = extract_track_metadata(tmp_path, relative_path)

    assert result.file_size_bytes == path.stat().st_size
    assert result.duration_seconds is None
    assert result.media_status is MediaStatus.UNREADABLE
    assert CatalogProblemCode.FILE_UNREADABLE in {
        problem.code for problem in result.problems
    }


def test_file_that_disappears_after_discovery_is_reported(tmp_path: Path) -> None:
    relative_path = Path("Rock/Artist - Vanished.mp3")
    path = tmp_path / relative_path
    write_test_mp3(path)
    path.unlink()

    result = extract_track_metadata(tmp_path, relative_path)

    assert result.relative_path == relative_path
    assert result.media_status is MediaStatus.MISSING
    assert result.file_size_bytes is None
    assert CatalogProblemCode.FILE_MISSING in {
        problem.code for problem in result.problems
    }


def test_library_scan_continues_after_an_individual_invalid_track(
    tmp_path: Path,
) -> None:
    bad_relative_path = Path("Rock/A Bad - Track.mp3")
    bad_path = tmp_path / bad_relative_path
    bad_path.parent.mkdir(parents=True)
    bad_path.write_bytes(b"invalid")
    good_relative_path = Path("Rock/Z Good - Track.mp3")
    write_test_mp3(tmp_path / good_relative_path)

    results = scan_music_library(tmp_path)

    assert [result.relative_path for result in results] == [
        bad_relative_path,
        good_relative_path,
    ]
    assert [result.media_status for result in results] == [
        MediaStatus.INVALID,
        MediaStatus.VALID,
    ]
