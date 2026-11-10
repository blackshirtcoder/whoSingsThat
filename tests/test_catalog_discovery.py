from pathlib import Path

from who_sings_that.catalog import discover_mp3_files


def test_discovers_nested_mp3s_as_relative_paths_with_case_insensitive_extensions(
    tmp_path: Path,
) -> None:
    music_root = tmp_path / "Music Root"
    nested = music_root / "Röck & Roll" / "Live (1991)"
    nested.mkdir(parents=True)
    (music_root / "top-level.Mp3").write_bytes(b"")
    (nested / "AC-DC - Song!.MP3").write_bytes(b"")
    (nested / "ignore.wav").write_bytes(b"")
    (nested / "also-ignore.mp3.txt").write_bytes(b"")

    discovered = discover_mp3_files(music_root)

    assert discovered == (
        Path("Röck & Roll/Live (1991)/AC-DC - Song!.MP3"),
        Path("top-level.Mp3"),
    )
    assert all(not path.is_absolute() for path in discovered)


def test_identical_library_layouts_have_identical_relative_paths(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "local" / "music"
    second_root = tmp_path / "nas" / "audio"
    relative_files = (
        Path("Rock/AC-DC - Beating Around the Bush.mp3"),
        Path("Rock/Björk - Jóga.mp3"),
    )
    for root in (first_root, second_root):
        for relative_file in relative_files:
            destination = root / relative_file
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"")

    assert discover_mp3_files(first_root) == discover_mp3_files(second_root)
    assert discover_mp3_files(first_root) == relative_files


def test_nonexistent_music_root_is_reported(tmp_path: Path) -> None:
    missing_root = tmp_path / "missing"

    try:
        discover_mp3_files(missing_root)
    except FileNotFoundError as error:
        assert str(missing_root) in str(error)
    else:
        raise AssertionError("a missing music root must not look like an empty library")
