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
    ForeignKeyConstraint,
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


class CompletedGameRow(Base):
    """One immutable, atomically committed completed-game archive root."""

    __tablename__ = "completed_games"
    __table_args__ = (
        CheckConstraint("length(game_id) = 36", name="ck_completed_games_id_length"),
        CheckConstraint(
            "end_reason IN ('deck_exhausted', 'songmaster_ended_game')",
            name="ck_completed_games_end_reason",
        ),
    )

    game_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTimeText(), nullable=False)
    started_at: Mapped[datetime] = mapped_column(UTCDateTimeText(), nullable=False)
    ended_at: Mapped[datetime] = mapped_column(UTCDateTimeText(), nullable=False)
    end_reason: Mapped[str] = mapped_column(String(32), nullable=False)
    playback_offset_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    playback_duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    speed_bonus_plateau_percent: Mapped[str] = mapped_column(Text, nullable=False)
    history_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class CompletedGameParticipantRow(Base):
    """One game-local participant and frozen final standing."""

    __tablename__ = "completed_game_participants"
    __table_args__ = (
        ForeignKeyConstraint(
            ["game_id"],
            ["completed_games.game_id"],
            name="fk_history_participants_game",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "game_id",
            "join_order",
            name="uq_history_participants_join_order",
        ),
    )

    game_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    participant_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    join_order: Mapped[int] = mapped_column(Integer, nullable=False)
    display_name: Mapped[str] = mapped_column(String(24), nullable=False)
    final_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    final_score: Mapped[int] = mapped_column(Integer, nullable=False)


class CompletedGamePlaylistRow(Base):
    """Selected-playlist metadata frozen at Start Game."""

    __tablename__ = "completed_game_playlists"
    __table_args__ = (
        ForeignKeyConstraint(
            ["game_id"],
            ["completed_games.game_id"],
            name="fk_history_playlists_game",
            ondelete="CASCADE",
        ),
    )

    game_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    selection_order: Mapped[int] = mapped_column(Integer, primary_key=True)
    playlist_id: Mapped[str] = mapped_column(String(36), nullable=False)
    playlist_name: Mapped[str] = mapped_column(String(32), nullable=False)
    playlist_created_at: Mapped[datetime] = mapped_column(
        UTCDateTimeText(),
        nullable=False,
    )
    playlist_song_count: Mapped[int] = mapped_column(Integer, nullable=False)


class CompletedGameSongmasterEventRow(Base):
    """One ordered Songmaster assignment, transfer, or recovery fact."""

    __tablename__ = "completed_game_songmaster_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["game_id"],
            ["completed_games.game_id"],
            name="fk_history_songmaster_events_game",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["game_id", "previous_participant_id"],
            [
                "completed_game_participants.game_id",
                "completed_game_participants.participant_id",
            ],
            name="fk_history_songmaster_events_previous_participant",
        ),
        ForeignKeyConstraint(
            ["game_id", "resulting_participant_id"],
            [
                "completed_game_participants.game_id",
                "completed_game_participants.participant_id",
            ],
            name="fk_history_songmaster_events_resulting_participant",
        ),
    )

    game_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTimeText(), nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_participant_id: Mapped[int | None] = mapped_column(Integer)
    resulting_participant_id: Mapped[int] = mapped_column(Integer, nullable=False)


class CompletedGameDeckEntryRow(Base):
    """One original deck entry and its final outcome/round facts."""

    __tablename__ = "completed_game_deck_entries"
    __table_args__ = (
        ForeignKeyConstraint(
            ["game_id"],
            ["completed_games.game_id"],
            name="fk_history_deck_entries_game",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["game_id", "buzzing_participant_id"],
            [
                "completed_game_participants.game_id",
                "completed_game_participants.participant_id",
            ],
            name="fk_history_deck_entries_buzzer",
        ),
        ForeignKeyConstraint(
            ["game_id", "lost_playback_client_participant_id"],
            [
                "completed_game_participants.game_id",
                "completed_game_participants.participant_id",
            ],
            name="fk_history_deck_entries_lost_client",
        ),
        UniqueConstraint("game_id", "round_id", name="uq_history_round_id"),
        CheckConstraint(
            "outcome IN ('completed', 'voided', 'skipped', 'unplayed')",
            name="ck_history_deck_entry_outcome",
        ),
    )

    game_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    original_position: Mapped[int] = mapped_column(Integer, primary_key=True)
    track_id: Mapped[int] = mapped_column(Integer, nullable=False)
    artist: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    track_duration_seconds: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    skip_reason: Mapped[str | None] = mapped_column(String(32))
    round_id: Mapped[str | None] = mapped_column(String(36))
    configured_playback_offset_seconds: Mapped[int | None] = mapped_column(Integer)
    effective_playback_offset_seconds: Mapped[str | None] = mapped_column(Text)
    playback_duration_seconds: Mapped[int | None] = mapped_column(Integer)
    speed_bonus_plateau_percent: Mapped[str | None] = mapped_column(Text)
    playback_started_at: Mapped[datetime | None] = mapped_column(UTCDateTimeText())
    round_termination_reason: Mapped[str | None] = mapped_column(String(32))
    buzzing_participant_id: Mapped[int | None] = mapped_column(Integer)
    buzz_elapsed_seconds: Mapped[str | None] = mapped_column(Text)
    scoring_multiplier: Mapped[str | None] = mapped_column(Text)
    scoring_choice: Mapped[str | None] = mapped_column(String(16))
    base_score: Mapped[int | None] = mapped_column(Integer)
    awarded_points: Mapped[int | None] = mapped_column(Integer)
    void_reason: Mapped[str | None] = mapped_column(String(32))
    lost_playback_client_participant_id: Mapped[int | None] = mapped_column(Integer)
    answer_revealed: Mapped[bool | None] = mapped_column(Boolean)
