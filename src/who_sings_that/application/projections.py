"""Explicit immutable, answer-safe projections captured at FIFO boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from who_sings_that.domain import (
    GameSession,
    GameState,
    SongmasterRecoveryStatus,
    calculate_points,
)

from .connections import ConnectionRegistry

PROJECTION_SCHEMA_VERSION: Final = 1


@dataclass(frozen=True, slots=True)
class PublicParticipantProjection:
    public_participant_id: UUID
    display_name: str
    committed_score: int
    provisional_score: int
    effective_score: int
    connected: bool
    is_songmaster: bool
    join_order: int

    def to_payload(self) -> dict[str, Any]:
        return {
            "public_participant_id": str(self.public_participant_id),
            "display_name": self.display_name,
            "committed_score": self.committed_score,
            "provisional_score": self.provisional_score,
            "effective_score": self.effective_score,
            "connected": self.connected,
            "is_songmaster": self.is_songmaster,
        }


@dataclass(frozen=True, slots=True)
class RecipientProjection:
    public_participant_id: UUID
    is_songmaster: bool
    eligible_to_buzz: bool
    has_authoritative_connection: bool
    has_control_privileges: bool
    audio_enabled: bool
    browser_audio_usable: bool
    can_act: bool
    available_songmaster_operations: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "public_participant_id": str(self.public_participant_id),
            "is_songmaster": self.is_songmaster,
            "eligible_to_buzz": self.eligible_to_buzz,
            "has_authoritative_connection": self.has_authoritative_connection,
            "has_control_privileges": self.has_control_privileges,
            "audio_enabled": self.audio_enabled,
            "browser_audio_usable": self.browser_audio_usable,
            "can_act": self.can_act,
            "available_songmaster_operations": list(
                self.available_songmaster_operations
            ),
        }


@dataclass(frozen=True, slots=True)
class PublicGameProjection:
    game_session_id: UUID
    state: GameState
    suspended_for_songmaster_recovery: bool
    songmaster_public_participant_id: UUID
    participants: tuple[PublicParticipantProjection, ...]
    scoreboard_order: tuple[UUID, ...]
    started: bool
    finished: bool
    deck_exhausted: bool
    original_deck_size: int
    next_deck_position: int | None
    displayed_deck_position: int | None
    displayed_artist: str | None
    displayed_title: str | None
    selected_playlist_ids: tuple[str, ...]
    selected_playlists: tuple[dict[str, Any], ...]
    settings: dict[str, Any]
    round_status: dict[str, Any]
    recovery: dict[str, Any] | None
    last_recovery_outcome: dict[str, Any] | None
    end_reason: str | None
    final_standings: tuple[dict[str, Any], ...]

    def to_payload(self) -> dict[str, Any]:
        displayed_song = None
        if self.displayed_deck_position is not None:
            displayed_song = {
                "deck_position": self.displayed_deck_position,
                "artist": self.displayed_artist,
                "title": self.displayed_title,
            }
        return {
            "schema_version": PROJECTION_SCHEMA_VERSION,
            "game_session_id": str(self.game_session_id),
            "state": self.state.value,
            "suspended_for_songmaster_recovery": (
                self.suspended_for_songmaster_recovery
            ),
            "songmaster_public_participant_id": str(
                self.songmaster_public_participant_id
            ),
            "participants": [item.to_payload() for item in self.participants],
            "scoreboard_order": [str(value) for value in self.scoreboard_order],
            "deck": {
                "started": self.started,
                "original_size": self.original_deck_size,
                "next_position": self.next_deck_position,
                "exhausted": self.deck_exhausted,
            },
            "started": self.started,
            "finished": self.finished,
            "selected_playlist_ids": list(self.selected_playlist_ids),
            "selected_playlists": list(self.selected_playlists),
            "settings": dict(self.settings),
            "displayed_song": displayed_song,
            "current_round": dict(self.round_status),
            "recovery": None if self.recovery is None else dict(self.recovery),
            "last_recovery_outcome": (
                None
                if self.last_recovery_outcome is None
                else dict(self.last_recovery_outcome)
            ),
            "end_reason": self.end_reason,
            "final_standings": list(self.final_standings),
        }


@dataclass(frozen=True, slots=True)
class CapturedRecipientProjection:
    """One recipient payload plus internal routing facts never serialized."""

    browser_token: str
    connection_id: str
    recipient: RecipientProjection


@dataclass(frozen=True, slots=True)
class GameEventNotification:
    event: str
    payload: dict[str, Any]

    def to_payload(self, game_id: UUID, revision: int) -> dict[str, Any]:
        return {
            "game_session_id": str(game_id),
            "revision": revision,
            "event": self.event,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True, slots=True)
class AuthoritativeProjectionUpdate:
    """Immutable post-event capture routed without reading mutable game state."""

    game_session_id: UUID
    revision: int
    captured_at: datetime
    public: PublicGameProjection | None
    recipients: tuple[CapturedRecipientProjection, ...]
    notifications: tuple[GameEventNotification, ...] = ()
    invalidated: bool = False

    def state_payload_for(self, recipient: CapturedRecipientProjection) -> dict[str, Any]:
        if self.public is None:
            return {
                "schema_version": PROJECTION_SCHEMA_VERSION,
                "game_session_id": str(self.game_session_id),
                "revision": self.revision,
                "server_time": _iso_utc(self.captured_at),
                "active": False,
                "state": GameState.NO_GAME.value,
            }
        payload = self.public.to_payload()
        payload.update(
            {
                "revision": self.revision,
                "server_time": _iso_utc(self.captured_at),
                "active": True,
                "recipient": recipient.recipient.to_payload(),
            }
        )
        return payload

    def recipient_for_token(
        self,
        browser_token: str,
    ) -> CapturedRecipientProjection | None:
        return next(
            (
                recipient
                for recipient in self.recipients
                if recipient.browser_token == browser_token
            ),
            None,
        )


@dataclass(frozen=True, slots=True)
class MembershipSynchronization:
    """Membership command result and its exact post-dispatch initial view."""

    result: object
    update: AuthoritativeProjectionUpdate
    recipient: CapturedRecipientProjection


def build_authoritative_projection(
    game: GameSession,
    state: GameState,
    registry: ConnectionRegistry,
    *,
    recovery_deadline_utc: datetime | None,
) -> tuple[PublicGameProjection, tuple[CapturedRecipientProjection, ...]]:
    """Build all recipient views synchronously from one authoritative boundary."""

    public_ids = {
        token: member.public_participant_id for token, member in game.members.items()
    }
    provisional_winner: str | None = None
    provisional_points = 0
    if state is GameState.SCORING and game.provisional_scoring_choice is not None:
        termination = game.round_termination
        active_round = game.active_round
        if (
            termination is not None
            and active_round is not None
            and termination.winning_browser_token is not None
            and termination.buzz_elapsed_seconds is not None
        ):
            provisional_winner = termination.winning_browser_token
            provisional_points = calculate_points(
                game.provisional_scoring_choice,
                active_round.playback_duration_seconds,
                active_round.speed_bonus_plateau_percent / 100,
                termination.buzz_elapsed_seconds,
            )

    participants = tuple(
        PublicParticipantProjection(
            public_participant_id=member.public_participant_id,
            display_name=member.display_name,
            committed_score=member.committed_score,
            provisional_score=(
                provisional_points if token == provisional_winner else 0
            ),
            effective_score=(
                member.committed_score
                + (provisional_points if token == provisional_winner else 0)
            ),
            connected=member.connected,
            is_songmaster=token == game.songmaster_browser_token,
            join_order=member.join_order,
        )
        for token, member in sorted(
            game.members.items(), key=lambda item: item[1].join_order
        )
    )
    scoreboard = tuple(
        participant.public_participant_id
        for participant in sorted(
            participants,
            key=lambda item: (-item.effective_score, item.join_order),
        )
    )
    selected_playlists = tuple(
        {
            "playlist_id": item.playlist_id,
            "playlist_name": item.playlist_name,
            "playlist_created_at": _iso_utc(item.playlist_created_at),
            "playlist_song_count": item.playlist_song_count,
        }
        for item in game.selected_playlist_snapshots
    )
    recovery = None
    if game.songmaster_recovery is not None:
        item = game.songmaster_recovery
        recovery = {
            "recovery_id": str(item.recovery_id),
            "status": item.status.value,
            "original_songmaster_public_participant_id": str(
                public_ids[item.original_songmaster_browser_token]
            ),
            "interrupted_state": item.interrupted_state.value,
            "normalized_state": item.normalized_state.value,
            "grace_deadline": (
                _iso_utc(recovery_deadline_utc)
                if item.status is SongmasterRecoveryStatus.RECONNECT_GRACE
                and recovery_deadline_utc is not None
                else None
            ),
            "preparation_discarded": item.preparation_discarded,
            "round_voided": item.voided_round_id is not None,
        }
    last_recovery_outcome = None
    if game.last_songmaster_recovery_outcome is not None:
        item = game.last_songmaster_recovery_outcome
        last_recovery_outcome = {
            "recovery_id": str(item.recovery_id),
            "resolution": item.resolution.value,
            "original_songmaster_public_participant_id": str(
                public_ids[item.original_songmaster_browser_token]
            ),
            "resulting_songmaster_public_participant_id": str(
                public_ids[item.resulting_songmaster_browser_token]
            ),
            "interrupted_state": item.interrupted_state.value,
            "resumed_state": item.resumed_state.value,
            "round_voided": item.voided_round_id is not None,
        }

    round_status = _round_status(game, state, public_ids, provisional_points)
    standings = tuple(
        {
            "rank": item.rank,
            "public_participant_id": str(public_ids[item.browser_token]),
            "display_name": item.display_name,
            "score": item.score,
        }
        for item in game.final_standings
    )
    next_position = None
    if game.started_at is not None and not game.deck_exhausted:
        next_position = game.deck_pointer + 1
    public = PublicGameProjection(
        game_session_id=game.game_session_id,
        state=state,
        suspended_for_songmaster_recovery=game.songmaster_recovery is not None,
        songmaster_public_participant_id=public_ids[game.songmaster_browser_token],
        participants=participants,
        scoreboard_order=scoreboard,
        started=game.started_at is not None,
        finished=state is GameState.FINISHED,
        deck_exhausted=game.deck_exhausted,
        original_deck_size=game.original_deck_size,
        next_deck_position=next_position,
        displayed_deck_position=game.displayed_deck_position,
        displayed_artist=game.displayed_artist,
        displayed_title=game.displayed_title,
        selected_playlist_ids=tuple(sorted(game.selected_playlist_ids)),
        selected_playlists=selected_playlists,
        settings={
            "playback_offset_seconds": game.settings.playback_offset_seconds,
            "playback_duration_seconds": game.settings.playback_duration_seconds,
            "speed_bonus_plateau_percent": str(
                game.settings.speed_bonus_plateau_percent
            ),
        },
        round_status=round_status,
        recovery=recovery,
        last_recovery_outcome=last_recovery_outcome,
        end_reason=None if game.end_reason is None else game.end_reason.value,
        final_standings=standings,
    )

    recipients: list[CapturedRecipientProjection] = []
    for token, member in game.members.items():
        connection_id = registry.current_connection_id(game.game_session_id, token)
        if connection_id is None:
            continue
        is_songmaster = token == game.songmaster_browser_token
        suspended = game.songmaster_recovery is not None
        eligible = bool(
            state is GameState.PLAYING
            and game.buzzing_enabled
            and game.active_round is not None
            and token in game.active_round.buzz_eligible_browser_tokens
            and member.connected
        )
        songmaster_operations = _songmaster_operations(
            state,
            game,
            is_songmaster=is_songmaster,
            suspended=suspended,
        )
        recipients.append(
            CapturedRecipientProjection(
                browser_token=token,
                connection_id=connection_id,
                recipient=RecipientProjection(
                    public_participant_id=member.public_participant_id,
                    is_songmaster=is_songmaster,
                    eligible_to_buzz=eligible,
                    has_authoritative_connection=True,
                    has_control_privileges=(
                        is_songmaster and member.connected and not suspended
                    ),
                    audio_enabled=member.audio_enabled,
                    browser_audio_usable=member.browser_audio_usable,
                    can_act=(
                        member.connected
                        and not suspended
                        and (
                            eligible
                            or state in {GameState.LOBBY, GameState.ROUND_COMPLETE}
                            or bool(songmaster_operations)
                        )
                    ),
                    available_songmaster_operations=songmaster_operations,
                ),
            )
        )
    return public, tuple(recipients)


def _round_status(
    game: GameSession,
    state: GameState,
    public_ids: dict[str, UUID],
    provisional_points: int,
) -> dict[str, Any]:
    active_round = game.active_round
    termination = game.round_termination
    pending = game.pending_audio_preparation
    responses = game.audio_preparation_responses
    buzzer = None
    if termination is not None and termination.winning_browser_token is not None:
        buzzer = str(public_ids[termination.winning_browser_token])
    result: dict[str, Any] = {
        "status": state.value,
        "round_id": None if active_round is None else str(active_round.round_id),
        "preparation_id": (
            None if pending is None else str(pending.preparation_id)
        ),
        "buzzer_public_participant_id": buzzer,
        "termination_reason": (
            None if termination is None else termination.reason.value
        ),
        "buzz_elapsed_seconds": (
            None if termination is None else termination.buzz_elapsed_seconds
        ),
        "answer_revealed": state is GameState.SCORING,
        "provisional_scoring_choice": (
            None
            if game.provisional_scoring_choice is None
            else game.provisional_scoring_choice.value
        ),
        "provisional_points": provisional_points,
    }
    if responses is not None:
        result["audio_preparation"] = {
            "pending_targets": len(responses.pending_browser_tokens),
            "ready_targets": len(responses.ready_browser_tokens),
            "failed_targets": len(responses.failed_browser_tokens),
            "outcome_determined": responses.outcome_determined,
            "failure_acknowledgement_pending": (
                responses.failure_acknowledgement_pending
            ),
        }
    else:
        result["audio_preparation"] = None
    if state is GameState.SCORING:
        # These fields are copied only after the authoritative reveal transition.
        result["artist"] = game.displayed_artist
        result["title"] = game.displayed_title
    if game.last_voided_round is not None:
        voided = game.last_voided_round
        lost = voided.lost_playback_client_browser_token
        result["last_voided_round"] = {
            "round_id": str(voided.active_round.round_id),
            "reason": voided.reason.value,
            "lost_playback_client_public_participant_id": (
                None if lost is None else str(public_ids[lost])
            ),
            "answer_revealed": voided.answer_revealed,
        }
    else:
        result["last_voided_round"] = None
    return result


def _songmaster_operations(
    state: GameState,
    game: GameSession,
    *,
    is_songmaster: bool,
    suspended: bool,
) -> tuple[str, ...]:
    if not is_songmaster or suspended or not game.songmaster.connected:
        return ()
    operations: list[str] = []
    if state is GameState.LOBBY:
        operations.extend(
            (
                "select_playlist",
                "deselect_playlist",
                "start_game",
                "cancel_game",
                "transfer_songmaster",
            )
        )
    if state is GameState.ROUND_COMPLETE:
        operations.extend(("play_song", "transfer_songmaster"))
        if not game.deck_exhausted:
            operations.append("end_game")
        if game.scoring_correction_round_id is not None:
            operations.append("correct_score")
    if state is GameState.AUDIO_PREPARING:
        operations.append("end_game")
        responses = game.audio_preparation_responses
        if (
            responses is not None
            and responses.failure_acknowledgement_pending
        ):
            operations.append("acknowledge_audio_preparation_failure")
    if state is GameState.AWAITING_REVEAL:
        operations.extend(("reveal", "end_game"))
    if state is GameState.SCORING:
        operations.extend(("select_score", "accept_score", "end_game"))
    if state is GameState.PLAYING:
        operations.append("end_game")
    if state is GameState.FINISHED:
        operations.append("close_game")
    return tuple(operations)


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "PROJECTION_SCHEMA_VERSION",
    "AuthoritativeProjectionUpdate",
    "CapturedRecipientProjection",
    "GameEventNotification",
    "MembershipSynchronization",
    "PublicGameProjection",
    "PublicParticipantProjection",
    "RecipientProjection",
    "build_authoritative_projection",
]
