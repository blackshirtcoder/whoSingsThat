"""Private HTTP delivery of catalog media authorized by opaque capabilities."""

import os
from pathlib import Path, PurePosixPath, PureWindowsPath
from stat import S_ISREG

from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from starlette.responses import (
    FileResponse,
    MalformedRangeHeader,
    RangeNotSatisfiable,
)

from who_sings_that.application import MediaCapabilityUnavailable
from who_sings_that.database import get_track_by_id

_PROTECTED_HEADERS = {
    "Cache-Control": "private, no-store, max-age=0",
    "Pragma": "no-cache",
    "X-Content-Type-Options": "nosniff",
}


async def media_response(request: Request, opaque_capability: str):
    """Authorize, resolve, and stream one protected media resource."""

    controller = request.app.state.game_controller
    try:
        capability = controller.authorize_media_capability(opaque_capability)
    except MediaCapabilityUnavailable, ValueError, TypeError:
        return _unavailable_response()

    database_engine = request.app.state.database_engine
    music_root = request.app.state.settings.music_root
    track = await run_in_threadpool(
        get_track_by_id,
        database_engine,
        capability.track_id,
    )
    if track is None:
        return _unavailable_response()
    try:
        path, stat_result = await run_in_threadpool(
            _resolve_protected_media,
            music_root,
            track.relative_path,
        )
    except OSError, ValueError:
        return _unavailable_response()

    range_header = request.headers.get("range")
    if range_header is not None:
        try:
            FileResponse._parse_range_header(range_header, stat_result.st_size)
        except MalformedRangeHeader:
            return JSONResponse(
                {"detail": "The byte range is malformed."},
                status_code=400,
                headers=_PROTECTED_HEADERS,
            )
        except RangeNotSatisfiable:
            return JSONResponse(
                {"detail": "The byte range is not satisfiable."},
                status_code=416,
                headers={
                    **_PROTECTED_HEADERS,
                    "Content-Range": f"bytes */{stat_result.st_size}",
                },
            )

    return FileResponse(
        path,
        media_type="audio/mpeg",
        headers=_PROTECTED_HEADERS,
        stat_result=stat_result,
    )


def _resolve_protected_media(
    music_root: str | os.PathLike[str],
    relative_path: str,
) -> tuple[Path, os.stat_result]:
    """Resolve a catalog path without permitting traversal or symlink escape."""

    if (
        not isinstance(relative_path, str)
        or not relative_path
        or "\x00" in relative_path
    ):
        raise ValueError("invalid catalog media path")
    posix = PurePosixPath(relative_path)
    windows = PureWindowsPath(relative_path)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or ".." in posix.parts
        or ".." in windows.parts
        or posix == PurePosixPath(".")
    ):
        raise ValueError("invalid catalog media path")

    root = Path(music_root).resolve(strict=True)
    candidate = (root / Path(*posix.parts)).resolve(strict=True)
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ValueError("catalog media leaves the configured root") from error
    stat_result = candidate.stat()
    if not S_ISREG(stat_result.st_mode) or not os.access(candidate, os.R_OK):
        raise OSError("catalog media is not a readable regular file")
    with candidate.open("rb"):
        pass
    return candidate, stat_result


def _unavailable_response() -> JSONResponse:
    return JSONResponse(
        {"detail": "Media is unavailable."},
        status_code=404,
        headers=_PROTECTED_HEADERS,
    )


__all__ = ["media_response"]
