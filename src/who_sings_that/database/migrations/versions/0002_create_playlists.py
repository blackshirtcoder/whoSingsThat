"""Create persistent playlists and playlist membership.

Revision ID: 0002_create_playlists
Revises: 0001_create_tracks
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_create_playlists"
down_revision: str | None = "0001_create_tracks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create playlist metadata and unordered track membership storage."""

    op.create_table(
        "playlists",
        sa.Column("playlist_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("name_key", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "length(playlist_id) = 36",
            name="ck_playlists_id_uuid_length",
        ),
        sa.CheckConstraint(
            "length(name) BETWEEN 1 AND 32",
            name="ck_playlists_name_length",
        ),
        sa.PrimaryKeyConstraint("playlist_id"),
        sa.UniqueConstraint("name_key", name="uq_playlists_name_key"),
    )
    op.create_table(
        "playlist_tracks",
        sa.Column("playlist_id", sa.String(length=36), nullable=False),
        sa.Column("track_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["playlist_id"],
            ["playlists.playlist_id"],
            name="fk_playlist_tracks_playlist_id_playlists",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["track_id"],
            ["tracks.track_id"],
            name="fk_playlist_tracks_track_id_tracks",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("playlist_id", "track_id"),
    )
    op.create_index(
        "ix_playlist_tracks_track_id",
        "playlist_tracks",
        ["track_id"],
        unique=False,
    )


def downgrade() -> None:
    """Remove playlists and their membership records without changing tracks."""

    op.drop_index("ix_playlist_tracks_track_id", table_name="playlist_tracks")
    op.drop_table("playlist_tracks")
    op.drop_table("playlists")
