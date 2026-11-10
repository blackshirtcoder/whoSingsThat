"""SQLAlchemy mappings for the persistent music catalog."""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .types import UTCDateTimeText


class TrackRow(Base):
    """One persistent track and its last imported inspection results."""

    __tablename__ = "tracks"
    __table_args__ = (
        UniqueConstraint("relative_path", name="uq_tracks_relative_path"),
        CheckConstraint(
            "metadata_source IN ('filename', 'id3', 'unavailable')",
            name="ck_tracks_metadata_source",
        ),
        CheckConstraint(
            "metadata_status IN ('complete', 'needs_review')",
            name="ck_tracks_metadata_status",
        ),
        CheckConstraint(
            "media_status IN ('valid', 'invalid', 'unreadable', 'missing')",
            name="ck_tracks_media_status",
        ),
        CheckConstraint(
            "duration_seconds IS NULL OR duration_seconds >= 0",
            name="ck_tracks_duration_nonnegative",
        ),
        CheckConstraint(
            "file_size_bytes IS NULL OR file_size_bytes >= 0",
            name="ck_tracks_file_size_nonnegative",
        ),
        {"sqlite_autoincrement": True},
    )

    track_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTimeText(), nullable=False)
    relative_path: Mapped[str] = mapped_column(Text, nullable=False)
    artist: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    metadata_source: Mapped[str] = mapped_column(String(32), nullable=False)
    metadata_status: Mapped[str] = mapped_column(String(32), nullable=False)
    media_status: Mapped[str] = mapped_column(String(32), nullable=False)
    problems: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=False,
        default=list,
        server_default=text("'[]'"),
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("0"),
    )


class PlaylistRow(Base):
    """One persistent named set of catalog track references."""

    __tablename__ = "playlists"
    __table_args__ = (
        UniqueConstraint("name_key", name="uq_playlists_name_key"),
        CheckConstraint(
            "length(playlist_id) = 36",
            name="ck_playlists_id_uuid_length",
        ),
        CheckConstraint(
            "length(name) BETWEEN 1 AND 32",
            name="ck_playlists_name_length",
        ),
    )

    playlist_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(32), nullable=False)
    name_key: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTimeText(), nullable=False)


class PlaylistTrackRow(Base):
    """An unordered, unique association between one playlist and one track."""

    __tablename__ = "playlist_tracks"
    __table_args__ = (Index("ix_playlist_tracks_track_id", "track_id"),)

    playlist_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey(
            "playlists.playlist_id",
            name="fk_playlist_tracks_playlist_id_playlists",
            ondelete="CASCADE",
        ),
        primary_key=True,
    )
    track_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "tracks.track_id",
            name="fk_playlist_tracks_track_id_tracks",
            ondelete="RESTRICT",
        ),
        primary_key=True,
    )
