from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from who_sings_that.domain.deck import TrackReference
from who_sings_that.domain.engine import (
    AudioPreferenceNotAllowedError,
    DomainInvariantError,
    GameEngine,
    NoValidAudioTargetError,
    PlaySongNotAllowedError,
    UnauthorizedAudioPreferenceError,
    UnauthorizedPlaySongError,
)
from who_sings_that.domain.models import (
    GameSettings,
    GameState,
    ResolvedPlaylist,
    SelectedPlaylistSnapshot,
)

CREATED_AT = datetime(2026, 11, 1, 12, 30, tzinfo=UTC)
STARTED_AT = datetime(2026, 11, 1, 12, 35, tzinfo=UTC)
PLAYLIST_CREATED_AT = datetime(2024, 5, 4, 9, 15, tzinfo=UTC)
PREPARATION_ID = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")


class SequenceClock:
    def __init__(self, *values: datetime) -> None:
        self._values = iter(values)

    def __call__(self) -> datetime:
        return next(self._values)


def unchanged(tracks: list[TrackReference]) -> None:
    """Leave build_deck's unique-track order unchanged."""


def resolved_playlist(*tracks: TrackReference) -> ResolvedPlaylist:
    return ResolvedPlaylist(
        snapshot=SelectedPlaylistSnapshot(
            playlist_id="playlist",
            playlist_name="Playlist",
            playlist_created_at=PLAYLIST_CREATED_AT,
            playlist_song_count=len(tracks),
        ),
        tracks=tracks,
    )


def make_engine(
    settings: GameSettings | None = None,
    *,
    preparation_id: UUID = PREPARATION_ID,
) -> GameEngine:
    return GameEngine(
        settings,
        clock=SequenceClock(CREATED_AT, STARTED_AT),
        preparation_id_factory=lambda: preparation_id,
        shuffle=unchanged,
    )


def start_game(
    engine: GameEngine, *tracks: TrackReference
):
    game = engine.create_game("songmaster-token", "Songmaster")
    engine.select_playlist("songmaster-token", "playlist")
    engine.start_game("songmaster-token", [resolved_playlist(*tracks)])
    return game


def enable_songmaster_audio(engine: GameEngine) -> None:
    engine.set_browser_audio_capability("songmaster-token", True)


def test_creator_audio_defaults_enabled_but_browser_capability_is_not_assumed() -> None:
    game = make_engine().create_game("songmaster-token", "Songmaster")

    assert game.songmaster.audio_enabled is True
    assert game.songmaster.browser_audio_usable is False
    assert game.songmaster.is_valid_audio_target is False


def test_new_members_default_to_audio_disabled_and_uninitialized() -> None:
    engine = make_engine()
    engine.create_game("songmaster-token", "Songmaster")

    member = engine.join_game("player-token", "Player")

    assert member.audio_enabled is False
    assert member.browser_audio_usable is False
    assert member.is_valid_audio_target is False


def test_each_member_changes_only_their_own_audio_preference_in_legal_states() -> None:
    engine = make_engine()
    game = engine.create_game("songmaster-token", "Songmaster")
    player = engine.join_game("player-token", "Player")

    returned = engine.set_audio_preference("player-token", True)

    assert returned is player
    assert player.audio_enabled is True
    assert game.songmaster.audio_enabled is True

    engine.select_playlist("songmaster-token", "playlist")
    engine.start_game(
        "songmaster-token",
        [resolved_playlist(TrackReference(1, 180))],
    )
    engine.set_audio_preference("player-token", False)

    assert player.audio_enabled is False
    assert engine.state is GameState.ROUND_COMPLETE


def test_unknown_member_cannot_change_any_audio_preference() -> None:
    engine = make_engine()
    game = engine.create_game("songmaster-token", "Songmaster")
    before = deepcopy(game)

    with pytest.raises(UnauthorizedAudioPreferenceError, match="only their own"):
        engine.set_audio_preference("other-token", False)

    assert game == before


def test_audio_preference_change_is_rejected_during_preparation() -> None:
    engine = make_engine()
    game = start_game(engine, TrackReference(1, 180))
    enable_songmaster_audio(engine)
    engine.play_song("songmaster-token")

    with pytest.raises(AudioPreferenceNotAllowedError, match="LOBBY or ROUND_COMPLETE"):
        engine.set_audio_preference("songmaster-token", False)

    assert game.songmaster.audio_enabled is True
    assert engine.state is GameState.AUDIO_PREPARING


def test_audio_preference_and_browser_capability_are_independent() -> None:
    engine = make_engine()
    game = engine.create_game("songmaster-token", "Songmaster")

    engine.set_audio_preference("songmaster-token", False)
    engine.set_browser_audio_capability("songmaster-token", True)

    assert game.songmaster.audio_enabled is False
    assert game.songmaster.browser_audio_usable is True
    assert game.songmaster.is_valid_audio_target is False

    engine.set_audio_preference("songmaster-token", True)

    assert game.songmaster.is_valid_audio_target is True


@pytest.mark.parametrize(
    ("connected", "enabled", "usable", "expected"),
    [
        (True, True, True, True),
        (True, False, True, False),
        (True, True, False, False),
        (False, True, False, False),
    ],
)
def test_valid_audio_target_requires_all_three_independent_conditions(
    connected: bool, enabled: bool, usable: bool, expected: bool
) -> None:
    engine = make_engine()
    engine.create_game("songmaster-token", "Songmaster")
    member = engine.join_game("player-token", "Player")
    member.connected = connected
    member.audio_enabled = enabled
    member.browser_audio_usable = usable

    assert member.is_valid_audio_target is expected
    engine.validate_invariants()


def test_reconnect_preserves_preference_but_clears_stale_audio_capability() -> None:
    engine = make_engine()
    engine.create_game("songmaster-token", "Songmaster")
    member = engine.join_game("player-token", "Player")
    engine.set_audio_preference("player-token", True)
    engine.set_browser_audio_capability("player-token", True)

    engine.disconnect_member("player-token")
    assert member.audio_enabled is True
    assert member.browser_audio_usable is False

    reconnected = engine.join_game("player-token")

    assert reconnected is member
    assert member.connected is True
    assert member.audio_enabled is True
    assert member.browser_audio_usable is False
    assert member.is_valid_audio_target is False


def test_successful_play_song_binds_candidate_and_enters_audio_preparing() -> None:
    engine = make_engine()
    game = start_game(
        engine,
        TrackReference(1, 10),
        TrackReference(2, 180),
    )
    enable_songmaster_audio(engine)
    pointer_before = game.deck_pointer
    assert game.deck is not None
    expected_entry = game.deck[pointer_before]

    pending = engine.play_song("songmaster-token")

    assert engine.state is GameState.AUDIO_PREPARING
    assert game.pending_audio_preparation is pending
    assert pending.deck_entry is expected_entry
    assert game.deck_pointer == pointer_before
    engine.validate_invariants()


def test_unauthorized_play_song_is_rejected_without_mutation() -> None:
    engine = make_engine()
    game = engine.create_game("songmaster-token", "Songmaster")
    engine.join_game("player-token", "Player")
    engine.select_playlist("songmaster-token", "playlist")
    engine.start_game(
        "songmaster-token",
        [resolved_playlist(TrackReference(1, 180))],
    )
    enable_songmaster_audio(engine)
    before = deepcopy(game)

    with pytest.raises(UnauthorizedPlaySongError, match="connected Songmaster"):
        engine.play_song("player-token")

    assert game == before
    assert engine.state is GameState.ROUND_COMPLETE


def test_play_song_is_rejected_in_the_wrong_state() -> None:
    engine = make_engine()
    game = engine.create_game("songmaster-token", "Songmaster")
    before = deepcopy(game)

    with pytest.raises(PlaySongNotAllowedError, match="ROUND_COMPLETE"):
        engine.play_song("songmaster-token")

    assert game == before
    assert engine.state is GameState.LOBBY


def test_no_valid_audio_target_rejection_preserves_authoritative_facts() -> None:
    engine = make_engine()
    game = start_game(engine, TrackReference(1, 180))
    game.displayed_deck_position = 1
    game.displayed_artist = "Previous Artist"
    game.displayed_title = "Previous Title"
    before = deepcopy(game)

    with pytest.raises(NoValidAudioTargetError, match="usable audio"):
        engine.play_song("songmaster-token")

    assert game == before
    assert game.pending_audio_preparation is None
    assert game.deck_pointer == 0
    assert engine.state is GameState.ROUND_COMPLETE
    engine.validate_invariants()


def test_pending_preparation_has_opaque_uuid_and_settings_snapshots() -> None:
    settings = GameSettings(
        playback_offset_seconds=30,
        playback_duration_seconds=30,
        speed_bonus_plateau_percent=Decimal("17.5"),
    )
    engine = make_engine(settings)
    game = start_game(engine, TrackReference(91, 35))
    enable_songmaster_audio(engine)

    pending = engine.play_song("songmaster-token")

    assert pending.preparation_id == PREPARATION_ID
    assert isinstance(pending.preparation_id, UUID)
    assert pending.preparation_id != pending.deck_entry.track.track_id
    assert pending.configured_playback_offset_seconds == 30
    assert pending.effective_playback_offset_seconds == Decimal(5)
    assert pending.playback_duration_seconds == 30
    assert pending.speed_bonus_plateau_percent == Decimal("17.5")
    assert pending is game.pending_audio_preparation
    with pytest.raises(FrozenInstanceError):
        pending.playback_duration_seconds = 5  # type: ignore[misc]


def test_default_preparation_id_generation_is_unique_between_attempts() -> None:
    def prepare_with_new_engine() -> UUID:
        engine = GameEngine(
            clock=SequenceClock(CREATED_AT, STARTED_AT),
            shuffle=unchanged,
        )
        start_game(engine, TrackReference(1, 180))
        enable_songmaster_audio(engine)
        return engine.play_song("songmaster-token").preparation_id

    assert prepare_with_new_engine() != prepare_with_new_engine()


def test_buzz_and_audio_target_sets_snapshot_different_membership_conditions() -> None:
    engine = make_engine()
    engine.create_game("songmaster-token", "Songmaster")
    non_audio = engine.join_game("non-audio-token", "Non Audio")
    valid_audio = engine.join_game("audio-token", "Audio")
    disabled_audio = engine.join_game("disabled-token", "Disabled")
    disconnected = engine.join_game("disconnected-token", "Disconnected")

    engine.set_audio_preference(valid_audio.browser_token, True)
    engine.set_browser_audio_capability(valid_audio.browser_token, True)
    engine.set_browser_audio_capability(disabled_audio.browser_token, True)
    engine.set_audio_preference(disconnected.browser_token, True)
    engine.set_browser_audio_capability(disconnected.browser_token, True)
    engine.disconnect_member(disconnected.browser_token)

    engine.select_playlist("songmaster-token", "playlist")
    engine.start_game(
        "songmaster-token",
        [resolved_playlist(TrackReference(1, 180))],
    )
    pending = engine.play_song("songmaster-token")

    assert non_audio.audio_enabled is False
    assert pending.buzz_eligible_browser_tokens == frozenset(
        {
            "songmaster-token",
            "non-audio-token",
            "audio-token",
            "disabled-token",
        }
    )
    assert pending.audio_target_browser_tokens == frozenset({"audio-token"})
    assert disconnected.browser_token not in pending.buzz_eligible_browser_tokens


def test_pending_snapshots_do_not_alias_settings_or_membership_collections() -> None:
    engine = make_engine()
    game = start_game(engine, TrackReference(1, 180))
    enable_songmaster_audio(engine)
    pending = engine.play_song("songmaster-token")

    game.settings.playback_offset_seconds = 4
    game.members["late-token"] = deepcopy(game.songmaster)
    game.members["late-token"].browser_token = "late-token"

    assert pending.configured_playback_offset_seconds == 30
    assert pending.buzz_eligible_browser_tokens == frozenset({"songmaster-token"})
    assert pending.audio_target_browser_tokens == frozenset({"songmaster-token"})


def test_transition_c_does_not_commit_round_score_or_display_changes() -> None:
    engine = make_engine()
    game = start_game(engine, TrackReference(1, 180))
    game.songmaster.committed_score = 75
    game.displayed_deck_position = 1
    game.displayed_artist = "Previous Artist"
    game.displayed_title = "Previous Title"
    enable_songmaster_audio(engine)
    pointer_before = game.deck_pointer

    engine.play_song("songmaster-token")

    assert game.deck_pointer == pointer_before
    assert game.active_round is None
    assert game.songmaster.committed_score == 75
    assert game.displayed_deck_position == 1
    assert game.displayed_artist == "Previous Artist"
    assert game.displayed_title == "Previous Title"
    assert game.buzzing_enabled is False


def test_audio_preparing_invariants_reject_a_committed_round_or_buzzing() -> None:
    engine = make_engine()
    game = start_game(engine, TrackReference(1, 180))
    enable_songmaster_audio(engine)
    engine.play_song("songmaster-token")

    game.active_round = object()
    with pytest.raises(DomainInvariantError, match="active played round"):
        engine.validate_invariants()

    game.active_round = None
    game.buzzing_enabled = True
    with pytest.raises(DomainInvariantError, match="buzzing must be disabled"):
        engine.validate_invariants()


def test_repeated_play_song_is_rejected_while_already_preparing() -> None:
    engine = make_engine()
    game = start_game(engine, TrackReference(1, 180))
    enable_songmaster_audio(engine)
    first = engine.play_song("songmaster-token")
    pointer_before = game.deck_pointer

    with pytest.raises(PlaySongNotAllowedError, match="ROUND_COMPLETE"):
        engine.play_song("songmaster-token")

    assert game.pending_audio_preparation is first
    assert game.deck_pointer == pointer_before
    assert engine.state is GameState.AUDIO_PREPARING

