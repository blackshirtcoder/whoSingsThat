"""Transactional persistence and retrieval of named catalog playlists."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from unicodedata import category, normalize
from uuid import uuid4

from sqlalchemy import Engine, delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .catalog import StoredTrack, _stored_track_from_row
from .models import PlaylistRow, PlaylistTrackRow, TrackRow

MAX_PLAYLIST_NAME_LENGTH: Final = 32
_ALLOWED_PLAYLIST_NAME_PUNCTUATION: Final = frozenset(" -_'.")


class InvalidPlaylistNameError(ValueError):
    """Raised when a playlist display name does not satisfy the V1 rules."""


class DuplicatePlaylistNameError(ValueError):
    """Raised when another playlist already reserves the normalized name."""

    def __init__(self, name: str):
        super().__init__(f"playlist name is already in use: {name}")
        self.name = name


class PlaylistNotFoundError(LookupError):
    """Raised when a playlist mutation targets an unknown identity."""

    def __init__(self, playlist_id: str):
        super().__init__(f"playlist does not exist: {playlist_id}")
        self.playlist_id = playlist_id


class TrackNotFoundError(LookupError):
    """Raised when a membership operation names unknown catalog tracks."""

    def __init__(self, track_ids: Iterable[int]):
        self.track_ids = tuple(track_ids)
        joined_ids = ", ".join(str(track_id) for track_id in self.track_ids)
        super().__init__(f"catalog tracks do not exist: {joined_ids}")


@dataclass(frozen=True, slots=True)
class StoredPlaylist:
    """A detached immutable view of one playlist and its current song count."""

    playlist_id: str
    name: str
    description: str | None
    created_at: datetime
    song_count: int


class _NotProvided:
    pass


_NOT_PROVIDED = _NotProvided()


def normalize_playlist_name(name: str) -> str:
    """Normalize and validate a playlist name while preserving capitalization."""

    if not isinstance(name, str):
        raise TypeError("name must be a string")

    normalized = " ".join(normalize("NFKC", name).split())
    if not normalized:
        raise InvalidPlaylistNameError("name must not be empty")
    if len(normalized) > MAX_PLAYLIST_NAME_LENGTH:
        raise InvalidPlaylistNameError(
            f"name must be at most {MAX_PLAYLIST_NAME_LENGTH} characters"
        )

    invalid_character = next(
        (
            character
            for character in normalized
            if category(character)[0] not in {"L", "N"}
            and character not in _ALLOWED_PLAYLIST_NAME_PUNCTUATION
        ),
        None,
    )
    if invalid_character is not None:
        raise InvalidPlaylistNameError(
            f"name contains an invalid character: {invalid_character!r}"
        )
    return normalized


def playlist_name_uniqueness_key(name: str) -> str:
    """Return the Unicode-normalized, case-folded playlist uniqueness key."""

    validated_name = normalize_playlist_name(name)
    return normalize("NFKC", validated_name.casefold())


def create_playlist(
    engine: Engine,
    name: str,
    description: str | None = None,
) -> StoredPlaylist:
    """Create and return an empty playlist with a generated stable identity."""

    normalized_name = normalize_playlist_name(name)
    name_key = playlist_name_uniqueness_key(normalized_name)
    _validate_description(description)
    row = PlaylistRow(
        playlist_id=str(uuid4()),
        name=normalized_name,
        name_key=name_key,
        description=description,
        created_at=datetime.now(UTC),
    )

    try:
        with Session(engine) as session, session.begin():
            if _playlist_with_name_key_exists(session, name_key):
                raise DuplicatePlaylistNameError(normalized_name)
            session.add(row)
            session.flush()
            stored = _stored_playlist_from_row(row, song_count=0)
    except IntegrityError as error:
        raise DuplicatePlaylistNameError(normalized_name) from error
    return stored


def get_playlist_by_id(engine: Engine, playlist_id: str) -> StoredPlaylist | None:
    """Retrieve a playlist by stable identity without accessing music files."""

    with Session(engine) as session:
        row = session.get(PlaylistRow, playlist_id)
        if row is None:
            return None
        return _stored_playlist_from_row(row, _playlist_song_count(session, playlist_id))


def list_playlists(engine: Engine) -> tuple[StoredPlaylist, ...]:
    """Return all playlists in normalized-name order with current song counts."""

    song_count = (
        select(func.count(PlaylistTrackRow.track_id))
        .where(PlaylistTrackRow.playlist_id == PlaylistRow.playlist_id)
        .correlate(PlaylistRow)
        .scalar_subquery()
    )
    with Session(engine) as session:
        rows = session.execute(
            select(PlaylistRow, song_count)
            .order_by(PlaylistRow.name_key, PlaylistRow.playlist_id)
        ).all()
        return tuple(
            _stored_playlist_from_row(row, count) for row, count in rows
        )


def update_playlist(
    engine: Engine,
    playlist_id: str,
    *,
    name: str | _NotProvided = _NOT_PROVIDED,
    description: str | None | _NotProvided = _NOT_PROVIDED,
) -> StoredPlaylist:
    """Update supplied playlist fields while preserving identity and creation time."""

    normalized_name: str | _NotProvided = _NOT_PROVIDED
    name_key: str | _NotProvided = _NOT_PROVIDED
    if name is not _NOT_PROVIDED:
        normalized_name = normalize_playlist_name(name)
        name_key = playlist_name_uniqueness_key(normalized_name)
    if description is not _NOT_PROVIDED:
        _validate_description(description)

    try:
        with Session(engine) as session, session.begin():
            row = session.get(PlaylistRow, playlist_id)
            if row is None:
                raise PlaylistNotFoundError(playlist_id)

            if name_key is not _NOT_PROVIDED:
                conflicting_id = session.scalar(
                    select(PlaylistRow.playlist_id).where(
                        PlaylistRow.name_key == name_key,
                        PlaylistRow.playlist_id != playlist_id,
                    )
                )
                if conflicting_id is not None:
                    raise DuplicatePlaylistNameError(normalized_name)
                row.name = normalized_name
                row.name_key = name_key
            if description is not _NOT_PROVIDED:
                row.description = description

            session.flush()
            stored = _stored_playlist_from_row(
                row,
                _playlist_song_count(session, playlist_id),
            )
    except IntegrityError as error:
        duplicate_name = normalized_name if isinstance(normalized_name, str) else name
        raise DuplicatePlaylistNameError(str(duplicate_name)) from error
    return stored


def delete_playlist(engine: Engine, playlist_id: str) -> None:
    """Delete a playlist; database cascading removes only its memberships."""

    with Session(engine) as session, session.begin():
        row = session.get(PlaylistRow, playlist_id)
        if row is None:
            raise PlaylistNotFoundError(playlist_id)
        session.delete(row)


def add_tracks_to_playlist(
    engine: Engine,
    playlist_id: str,
    track_ids: Iterable[int],
) -> StoredPlaylist:
    """Atomically add existing tracks, ignoring memberships already present."""

    requested_ids = _validated_track_ids(track_ids)
    with Session(engine) as session, session.begin():
        playlist = _require_playlist(session, playlist_id)
        _require_tracks(session, requested_ids)
        existing_ids = set(
            session.scalars(
                select(PlaylistTrackRow.track_id).where(
                    PlaylistTrackRow.playlist_id == playlist_id,
                    PlaylistTrackRow.track_id.in_(requested_ids),
                )
            ).all()
        )
        session.add_all(
            PlaylistTrackRow(playlist_id=playlist_id, track_id=track_id)
            for track_id in requested_ids
            if track_id not in existing_ids
        )
        session.flush()
        return _stored_playlist_from_row(
            playlist,
            _playlist_song_count(session, playlist_id),
        )


def remove_tracks_from_playlist(
    engine: Engine,
    playlist_id: str,
    track_ids: Iterable[int],
) -> StoredPlaylist:
    """Atomically remove memberships, ignoring existing-track nonmembers."""

    requested_ids = _validated_track_ids(track_ids)
    with Session(engine) as session, session.begin():
        playlist = _require_playlist(session, playlist_id)
        _require_tracks(session, requested_ids)
        if requested_ids:
            session.execute(
                delete(PlaylistTrackRow).where(
                    PlaylistTrackRow.playlist_id == playlist_id,
                    PlaylistTrackRow.track_id.in_(requested_ids),
                )
            )
        return _stored_playlist_from_row(
            playlist,
            _playlist_song_count(session, playlist_id),
        )


def get_playlist_tracks(engine: Engine, playlist_id: str) -> tuple[StoredTrack, ...]:
    """Return detached member tracks, including their current catalog statuses."""

    with Session(engine) as session:
        _require_playlist(session, playlist_id)
        rows = session.scalars(
            select(TrackRow)
            .join(
                PlaylistTrackRow,
                PlaylistTrackRow.track_id == TrackRow.track_id,
            )
            .where(PlaylistTrackRow.playlist_id == playlist_id)
            .order_by(TrackRow.track_id)
        ).all()
        return tuple(_stored_track_from_row(row) for row in rows)


def _validate_description(description: str | None | _NotProvided) -> None:
    if description is not None and not isinstance(description, str):
        raise TypeError("description must be a string or None")


def _playlist_with_name_key_exists(session: Session, name_key: str) -> bool:
    return session.scalar(
        select(PlaylistRow.playlist_id).where(PlaylistRow.name_key == name_key)
    ) is not None


def _playlist_song_count(session: Session, playlist_id: str) -> int:
    return session.scalar(
        select(func.count()).select_from(PlaylistTrackRow).where(
            PlaylistTrackRow.playlist_id == playlist_id
        )
    ) or 0


def _require_playlist(session: Session, playlist_id: str) -> PlaylistRow:
    row = session.get(PlaylistRow, playlist_id)
    if row is None:
        raise PlaylistNotFoundError(playlist_id)
    return row


def _validated_track_ids(track_ids: Iterable[int]) -> tuple[int, ...]:
    unique_ids: dict[int, None] = {}
    for track_id in track_ids:
        if isinstance(track_id, bool) or not isinstance(track_id, int):
            raise TypeError("track IDs must be integers")
        if track_id <= 0:
            raise ValueError("track IDs must be positive integers")
        unique_ids[track_id] = None
    return tuple(unique_ids)


def _require_tracks(session: Session, track_ids: tuple[int, ...]) -> None:
    if not track_ids:
        return
    stored_ids = set(
        session.scalars(
            select(TrackRow.track_id).where(TrackRow.track_id.in_(track_ids))
        ).all()
    )
    missing_ids = tuple(track_id for track_id in track_ids if track_id not in stored_ids)
    if missing_ids:
        raise TrackNotFoundError(missing_ids)


def _stored_playlist_from_row(row: PlaylistRow, song_count: int) -> StoredPlaylist:
    return StoredPlaylist(
        playlist_id=row.playlist_id,
        name=row.name,
        description=row.description,
        created_at=row.created_at,
        song_count=song_count,
    )
