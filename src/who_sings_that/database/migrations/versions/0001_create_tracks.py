"""Create the initial persistent tracks table.

Revision ID: 0001_create_tracks
Revises: None
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_create_tracks"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create catalog track storage."""

    op.create_table(
        "tracks",
        sa.Column("track_id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("relative_path", sa.Text(), nullable=False),
        sa.Column("artist", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=True),
        sa.Column("file_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("metadata_source", sa.String(length=32), nullable=False),
        sa.Column("metadata_status", sa.String(length=32), nullable=False),
        sa.Column("media_status", sa.String(length=32), nullable=False),
        sa.Column(
            "problems",
            sa.JSON(),
            server_default=sa.text("'[]'"),
            nullable=False,
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "duration_seconds IS NULL OR duration_seconds >= 0",
            name="ck_tracks_duration_nonnegative",
        ),
        sa.CheckConstraint(
            "file_size_bytes IS NULL OR file_size_bytes >= 0",
            name="ck_tracks_file_size_nonnegative",
        ),
        sa.CheckConstraint(
            "media_status IN ('valid', 'invalid', 'unreadable', 'missing')",
            name="ck_tracks_media_status",
        ),
        sa.CheckConstraint(
            "metadata_source IN ('filename', 'id3', 'unavailable')",
            name="ck_tracks_metadata_source",
        ),
        sa.CheckConstraint(
            "metadata_status IN ('complete', 'needs_review')",
            name="ck_tracks_metadata_status",
        ),
        sa.PrimaryKeyConstraint("track_id"),
        sa.UniqueConstraint("relative_path", name="uq_tracks_relative_path"),
        sqlite_autoincrement=True,
    )


def downgrade() -> None:
    """Remove catalog track storage."""

    op.drop_table("tracks")
