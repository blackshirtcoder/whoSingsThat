"""Create immutable completed-game history storage.

Revision ID: 0003_create_completed_game_history
Revises: 0002_create_playlists
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_create_completed_game_history"
down_revision: str | None = "0002_create_playlists"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the normalized completed-game archive tables."""

    op.create_table(
        "completed_games",
        sa.Column("game_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("started_at", sa.Text(), nullable=False),
        sa.Column("ended_at", sa.Text(), nullable=False),
        sa.Column("end_reason", sa.String(length=32), nullable=False),
        sa.Column("playback_offset_seconds", sa.Integer(), nullable=False),
        sa.Column("playback_duration_seconds", sa.Integer(), nullable=False),
        sa.Column("speed_bonus_plateau_percent", sa.Text(), nullable=False),
        sa.Column("history_hash", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "length(game_id) = 36",
            name="ck_completed_games_id_length",
        ),
        sa.CheckConstraint(
            "end_reason IN ('deck_exhausted', 'songmaster_ended_game')",
            name="ck_completed_games_end_reason",
        ),
        sa.PrimaryKeyConstraint("game_id"),
    )
    op.create_table(
        "completed_game_participants",
        sa.Column("game_id", sa.String(length=36), nullable=False),
        sa.Column("participant_id", sa.Integer(), nullable=False),
        sa.Column("join_order", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.String(length=24), nullable=False),
        sa.Column("final_rank", sa.Integer(), nullable=False),
        sa.Column("final_score", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["completed_games.game_id"],
            name="fk_history_participants_game",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("game_id", "participant_id"),
        sa.UniqueConstraint(
            "game_id",
            "join_order",
            name="uq_history_participants_join_order",
        ),
    )
    op.create_table(
        "completed_game_playlists",
        sa.Column("game_id", sa.String(length=36), nullable=False),
        sa.Column("selection_order", sa.Integer(), nullable=False),
        sa.Column("playlist_id", sa.String(length=36), nullable=False),
        sa.Column("playlist_name", sa.String(length=32), nullable=False),
        sa.Column("playlist_created_at", sa.Text(), nullable=False),
        sa.Column("playlist_song_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["completed_games.game_id"],
            name="fk_history_playlists_game",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("game_id", "selection_order"),
    )
    op.create_table(
        "completed_game_songmaster_events",
        sa.Column("game_id", sa.String(length=36), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.Text(), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("previous_participant_id", sa.Integer(), nullable=True),
        sa.Column("resulting_participant_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["completed_games.game_id"],
            name="fk_history_songmaster_events_game",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["game_id", "previous_participant_id"],
            [
                "completed_game_participants.game_id",
                "completed_game_participants.participant_id",
            ],
            name="fk_history_songmaster_events_previous_participant",
        ),
        sa.ForeignKeyConstraint(
            ["game_id", "resulting_participant_id"],
            [
                "completed_game_participants.game_id",
                "completed_game_participants.participant_id",
            ],
            name="fk_history_songmaster_events_resulting_participant",
        ),
        sa.PrimaryKeyConstraint("game_id", "sequence"),
    )
    op.create_table(
        "completed_game_deck_entries",
        sa.Column("game_id", sa.String(length=36), nullable=False),
        sa.Column("original_position", sa.Integer(), nullable=False),
        sa.Column("track_id", sa.Integer(), nullable=False),
        sa.Column("artist", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("track_duration_seconds", sa.Text(), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("skip_reason", sa.String(length=32), nullable=True),
        sa.Column("round_id", sa.String(length=36), nullable=True),
        sa.Column(
            "configured_playback_offset_seconds",
            sa.Integer(),
            nullable=True,
        ),
        sa.Column("effective_playback_offset_seconds", sa.Text(), nullable=True),
        sa.Column("playback_duration_seconds", sa.Integer(), nullable=True),
        sa.Column("speed_bonus_plateau_percent", sa.Text(), nullable=True),
        sa.Column("playback_started_at", sa.Text(), nullable=True),
        sa.Column("round_termination_reason", sa.String(length=32), nullable=True),
        sa.Column("buzzing_participant_id", sa.Integer(), nullable=True),
        sa.Column("buzz_elapsed_seconds", sa.Text(), nullable=True),
        sa.Column("scoring_multiplier", sa.Text(), nullable=True),
        sa.Column("scoring_choice", sa.String(length=16), nullable=True),
        sa.Column("base_score", sa.Integer(), nullable=True),
        sa.Column("awarded_points", sa.Integer(), nullable=True),
        sa.Column("void_reason", sa.String(length=32), nullable=True),
        sa.Column(
            "lost_playback_client_participant_id",
            sa.Integer(),
            nullable=True,
        ),
        sa.Column("answer_revealed", sa.Boolean(), nullable=True),
        sa.CheckConstraint(
            "outcome IN ('completed', 'voided', 'skipped', 'unplayed')",
            name="ck_history_deck_entry_outcome",
        ),
        sa.ForeignKeyConstraint(
            ["game_id"],
            ["completed_games.game_id"],
            name="fk_history_deck_entries_game",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["game_id", "buzzing_participant_id"],
            [
                "completed_game_participants.game_id",
                "completed_game_participants.participant_id",
            ],
            name="fk_history_deck_entries_buzzer",
        ),
        sa.ForeignKeyConstraint(
            ["game_id", "lost_playback_client_participant_id"],
            [
                "completed_game_participants.game_id",
                "completed_game_participants.participant_id",
            ],
            name="fk_history_deck_entries_lost_client",
        ),
        sa.PrimaryKeyConstraint("game_id", "original_position"),
        sa.UniqueConstraint("game_id", "round_id", name="uq_history_round_id"),
    )


def downgrade() -> None:
    """Remove completed history without changing catalog or playlists."""

    op.drop_table("completed_game_deck_entries")
    op.drop_table("completed_game_songmaster_events")
    op.drop_table("completed_game_playlists")
    op.drop_table("completed_game_participants")
    op.drop_table("completed_games")
