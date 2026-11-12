"""Atomic persistence and detached reads for immutable completed-game history."""

import json
from collections.abc import Callable
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from who_sings_that.domain import (
    CompletedGameHistory,
    DeckSkipReason,
    GameEndReason,
    HistoricalDeckEntry,
    HistoricalDeckOutcome,
    HistoricalParticipant,
    HistoricalSongmasterEvent,
    RoundTerminationReason,
    RoundVoidReason,
    ScoringChoice,
    SelectedPlaylistSnapshot,
    SongmasterEventReason,
)

from .models import (
    CompletedGameDeckEntryRow,
    CompletedGameParticipantRow,
    CompletedGamePlaylistRow,
    CompletedGameRow,
    CompletedGameSongmasterEventRow,
)


class CompletedGameHistoryError(RuntimeError):
    """Base class for completed-history persistence failures."""


class CompletedGameHistoryConflictError(CompletedGameHistoryError):
    """A game UUID is already bound to contradictory immutable content."""


class CompletedGameHistoryPersistenceError(CompletedGameHistoryError):
    """SQLite could not atomically commit a completed-game snapshot."""


@dataclass(frozen=True, slots=True)
class CompletedGameSummary:
    """Small detached list item for a future history-selection UI."""

    game_session_id: UUID
    created_at: datetime
    started_at: datetime
    ended_at: datetime
    end_reason: GameEndReason
    participant_count: int


def save_completed_game_history(
    engine: Engine,
    history: CompletedGameHistory,
    *,
    _after_participants: Callable[[], None] | None = None,
) -> bool:
    """Atomically insert history, returning false for an identical retry.

    ``_after_participants`` is a narrow deterministic fault-injection seam used
    to prove transaction rollback; application code must leave it unset.
    """

    if not isinstance(history, CompletedGameHistory):
        raise TypeError("history must be a CompletedGameHistory")
    game_id = str(history.game_session_id)
    history_hash = _history_hash(history)
    try:
        with Session(engine) as session, session.begin():
            existing = session.get(CompletedGameRow, game_id)
            if existing is not None:
                if existing.history_hash == history_hash:
                    return False
                raise CompletedGameHistoryConflictError(
                    f"completed game {game_id} already has different history"
                )

            session.add(
                CompletedGameRow(
                    game_id=game_id,
                    created_at=history.created_at,
                    started_at=history.started_at,
                    ended_at=history.ended_at,
                    end_reason=history.end_reason.value,
                    playback_offset_seconds=history.playback_offset_seconds,
                    playback_duration_seconds=history.playback_duration_seconds,
                    speed_bonus_plateau_percent=_decimal_text(
                        history.speed_bonus_plateau_percent
                    ),
                    history_hash=history_hash,
                )
            )
            session.flush()
            session.add_all(
                CompletedGameParticipantRow(
                    game_id=game_id,
                    participant_id=item.participant_id,
                    join_order=item.join_order,
                    display_name=item.display_name,
                    final_rank=item.final_rank,
                    final_score=item.final_score,
                )
                for item in history.participants
            )
            session.flush()
            if _after_participants is not None:
                _after_participants()

            session.add_all(
                CompletedGamePlaylistRow(
                    game_id=game_id,
                    selection_order=selection_order,
                    playlist_id=item.playlist_id,
                    playlist_name=item.playlist_name,
                    playlist_created_at=item.playlist_created_at,
                    playlist_song_count=item.playlist_song_count,
                )
                for selection_order, item in enumerate(
                    history.selected_playlists,
                    start=1,
                )
            )
            session.add_all(
                CompletedGameSongmasterEventRow(
                    game_id=game_id,
                    sequence=item.sequence,
                    occurred_at=item.occurred_at,
                    reason=item.reason.value,
                    previous_participant_id=item.previous_participant_id,
                    resulting_participant_id=item.resulting_participant_id,
                )
                for item in history.songmaster_events
            )
            session.add_all(
                _deck_row_from_history(game_id, item)
                for item in history.deck_entries
            )
    except CompletedGameHistoryConflictError:
        raise
    except SQLAlchemyError as error:
        raise CompletedGameHistoryPersistenceError(
            f"could not persist completed game {game_id}"
        ) from error
    return True


def list_completed_games(engine: Engine) -> tuple[CompletedGameSummary, ...]:
    """List completed games newest-first with a stable UUID tie-breaker."""

    with Session(engine) as session:
        roots = session.scalars(
            select(CompletedGameRow).order_by(
                CompletedGameRow.ended_at.desc(),
                CompletedGameRow.game_id,
            )
        ).all()
        summaries: list[CompletedGameSummary] = []
        for root in roots:
            participant_count = len(
                session.scalars(
                    select(CompletedGameParticipantRow.participant_id).where(
                        CompletedGameParticipantRow.game_id == root.game_id
                    )
                ).all()
            )
            summaries.append(
                CompletedGameSummary(
                    game_session_id=UUID(root.game_id),
                    created_at=root.created_at,
                    started_at=root.started_at,
                    ended_at=root.ended_at,
                    end_reason=GameEndReason(root.end_reason),
                    participant_count=participant_count,
                )
            )
        return tuple(summaries)


def get_completed_game_history(
    engine: Engine,
    game_session_id: UUID,
) -> CompletedGameHistory | None:
    """Load one complete detached immutable history snapshot by game UUID."""

    game_id = str(game_session_id)
    with Session(engine) as session:
        root = session.get(CompletedGameRow, game_id)
        if root is None:
            return None
        participant_rows = session.scalars(
            select(CompletedGameParticipantRow)
            .where(CompletedGameParticipantRow.game_id == game_id)
            .order_by(CompletedGameParticipantRow.join_order)
        ).all()
        playlist_rows = session.scalars(
            select(CompletedGamePlaylistRow)
            .where(CompletedGamePlaylistRow.game_id == game_id)
            .order_by(CompletedGamePlaylistRow.selection_order)
        ).all()
        event_rows = session.scalars(
            select(CompletedGameSongmasterEventRow)
            .where(CompletedGameSongmasterEventRow.game_id == game_id)
            .order_by(CompletedGameSongmasterEventRow.sequence)
        ).all()
        deck_rows = session.scalars(
            select(CompletedGameDeckEntryRow)
            .where(CompletedGameDeckEntryRow.game_id == game_id)
            .order_by(CompletedGameDeckEntryRow.original_position)
        ).all()

        return CompletedGameHistory(
            game_session_id=UUID(root.game_id),
            created_at=root.created_at,
            started_at=root.started_at,
            ended_at=root.ended_at,
            end_reason=GameEndReason(root.end_reason),
            playback_offset_seconds=root.playback_offset_seconds,
            playback_duration_seconds=root.playback_duration_seconds,
            speed_bonus_plateau_percent=Decimal(
                root.speed_bonus_plateau_percent
            ),
            participants=tuple(
                HistoricalParticipant(
                    participant_id=row.participant_id,
                    join_order=row.join_order,
                    display_name=row.display_name,
                    final_rank=row.final_rank,
                    final_score=row.final_score,
                )
                for row in participant_rows
            ),
            selected_playlists=tuple(
                SelectedPlaylistSnapshot(
                    playlist_id=row.playlist_id,
                    playlist_name=row.playlist_name,
                    playlist_created_at=row.playlist_created_at,
                    playlist_song_count=row.playlist_song_count,
                )
                for row in playlist_rows
            ),
            songmaster_events=tuple(
                HistoricalSongmasterEvent(
                    sequence=row.sequence,
                    occurred_at=row.occurred_at,
                    reason=SongmasterEventReason(row.reason),
                    previous_participant_id=row.previous_participant_id,
                    resulting_participant_id=row.resulting_participant_id,
                )
                for row in event_rows
            ),
            deck_entries=tuple(_history_deck_entry_from_row(row) for row in deck_rows),
        )


def _deck_row_from_history(
    game_id: str,
    item: HistoricalDeckEntry,
) -> CompletedGameDeckEntryRow:
    return CompletedGameDeckEntryRow(
        game_id=game_id,
        original_position=item.original_position,
        track_id=item.track_id,
        artist=item.artist,
        title=item.title,
        track_duration_seconds=_decimal_text(item.track_duration_seconds),
        outcome=item.outcome.value,
        skip_reason=None if item.skip_reason is None else item.skip_reason.value,
        round_id=None if item.round_id is None else str(item.round_id),
        configured_playback_offset_seconds=(
            item.configured_playback_offset_seconds
        ),
        effective_playback_offset_seconds=_optional_decimal_text(
            item.effective_playback_offset_seconds
        ),
        playback_duration_seconds=item.playback_duration_seconds,
        speed_bonus_plateau_percent=_optional_decimal_text(
            item.speed_bonus_plateau_percent
        ),
        playback_started_at=item.playback_started_at,
        round_termination_reason=(
            None
            if item.round_termination_reason is None
            else item.round_termination_reason.value
        ),
        buzzing_participant_id=item.buzzing_participant_id,
        buzz_elapsed_seconds=_optional_decimal_text(item.buzz_elapsed_seconds),
        scoring_multiplier=_optional_decimal_text(item.scoring_multiplier),
        scoring_choice=(
            None if item.scoring_choice is None else item.scoring_choice.value
        ),
        base_score=item.base_score,
        awarded_points=item.awarded_points,
        void_reason=None if item.void_reason is None else item.void_reason.value,
        lost_playback_client_participant_id=(
            item.lost_playback_client_participant_id
        ),
        answer_revealed=item.answer_revealed,
    )


def _history_deck_entry_from_row(
    row: CompletedGameDeckEntryRow,
) -> HistoricalDeckEntry:
    return HistoricalDeckEntry(
        original_position=row.original_position,
        track_id=row.track_id,
        artist=row.artist,
        title=row.title,
        track_duration_seconds=Decimal(row.track_duration_seconds),
        outcome=HistoricalDeckOutcome(row.outcome),
        skip_reason=(
            None if row.skip_reason is None else DeckSkipReason(row.skip_reason)
        ),
        round_id=None if row.round_id is None else UUID(row.round_id),
        configured_playback_offset_seconds=(
            row.configured_playback_offset_seconds
        ),
        effective_playback_offset_seconds=_optional_decimal(
            row.effective_playback_offset_seconds
        ),
        playback_duration_seconds=row.playback_duration_seconds,
        speed_bonus_plateau_percent=_optional_decimal(
            row.speed_bonus_plateau_percent
        ),
        playback_started_at=row.playback_started_at,
        round_termination_reason=(
            None
            if row.round_termination_reason is None
            else RoundTerminationReason(row.round_termination_reason)
        ),
        buzzing_participant_id=row.buzzing_participant_id,
        buzz_elapsed_seconds=_optional_decimal(row.buzz_elapsed_seconds),
        scoring_multiplier=_optional_decimal(row.scoring_multiplier),
        scoring_choice=(
            None if row.scoring_choice is None else ScoringChoice(row.scoring_choice)
        ),
        base_score=row.base_score,
        awarded_points=row.awarded_points,
        void_reason=(
            None if row.void_reason is None else RoundVoidReason(row.void_reason)
        ),
        lost_playback_client_participant_id=(
            row.lost_playback_client_participant_id
        ),
        answer_revealed=row.answer_revealed,
    )


def _history_hash(history: CompletedGameHistory) -> str:
    payload = json.dumps(
        _canonical_value(history),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def _canonical_value(value: Any) -> Any:
    if is_dataclass(value):
        return {
            item.name: _canonical_value(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if isinstance(value, tuple):
        return [_canonical_value(item) for item in value]
    return value


def _decimal_text(value: Decimal) -> str:
    return str(value)


def _optional_decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else _decimal_text(value)


def _optional_decimal(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)
