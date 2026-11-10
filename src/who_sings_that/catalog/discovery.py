"""Recursive discovery of MP3 files beneath a configured music root."""

import os
from collections.abc import Sequence
from os import PathLike
from pathlib import Path


def discover_mp3_files(music_root: str | PathLike[str]) -> tuple[Path, ...]:
    """Return deterministic root-relative paths for recursively found MP3s.

    Filename extensions are matched case-insensitively. Directory symlinks are
    not followed, preventing a scan from unexpectedly leaving or cycling beneath
    the configured root.
    """

    root = Path(music_root)
    if not root.exists():
        raise FileNotFoundError(f"music root does not exist: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"music root is not a directory: {root}")

    relative_paths: list[Path] = []
    for directory, _, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        for filename in filenames:
            if Path(filename).suffix.casefold() == ".mp3":
                relative_paths.append((directory_path / filename).relative_to(root))

    return tuple(sorted(relative_paths, key=_path_sort_key))


def _path_sort_key(path: Path) -> Sequence[str]:
    portable_path = path.as_posix()
    return (portable_path.casefold(), portable_path)
