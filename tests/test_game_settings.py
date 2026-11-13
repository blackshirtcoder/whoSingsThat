from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from who_sings_that.domain import (
    GameEngine,
    GameSettings,
    GameSettingsConfirmationStaleError,
    GameSettingsNotAllowedError,
    GameState,
    InvalidGameSettingsError,
    ResolvedPlaylist,
    SelectedPlaylistSnapshot,
    TrackReference,
    UnauthorizedGameSettingsError,
)


def settings(
    *, offset: int = 30, duration: int = 30, plateau: Decimal = Decimal(10)
) -> GameSettings:
    return GameSettings(offset, duration, plateau)


def resolved_playlist(*durations: int) -> ResolvedPlaylist:
    tracks = tuple(
        TrackReference(index, duration, f"Artist {index}", f"Title {index}")
        for index, duration in enumerate(durations, start=1)
    )
    return ResolvedPlaylist(
        snapshot=SelectedPlaylistSnapshot(
            playlist_id="playlist",
            playlist_name="Playlist",
            playlist_created_at=datetime(2026, 1, 1, tzinfo=UTC),
            playlist_song_count=len(tracks),
        ),
        tracks=tracks,
    )


def lobby_engine() -> tuple[GameEngine, object]:
    engine = GameEngine(shuffle=lambda tracks: None)
    game = engine.create_game("songmaster-token", "Songmaster")
    return engine, game


def started_engine(*durations: int) -> tuple[GameEngine, object]:
    engine, game = lobby_engine()
    engine.select_playlist("songmaster-token", "playlist")
    engine.start_game(
        "songmaster-token",
        [resolved_playlist(*durations)],
    )
    return engine, game


def test_songmaster_can_change_valid_settings_in_lobby() -> None:
    engine, game = lobby_engine()

    result = engine.set_game_settings(
        "songmaster-token", settings(offset=12, duration=45, plateau=Decimal("25.5"))
    )

    assert result.applied is True
    assert result.warning is None
    assert game.settings == settings(offset=12, duration=45, plateau=Decimal("25.5"))
    assert engine.state is GameState.LOBBY


def test_offset_at_warning_threshold_applies_without_confirmation() -> None:
    engine, game = lobby_engine()

    result = engine.set_game_settings("songmaster-token", settings(offset=120))

    assert result.applied is True
    assert result.warning is None
    assert game.settings.playback_offset_seconds == 120


def test_offset_above_warning_threshold_does_not_apply() -> None:
    engine, game = lobby_engine()

    result = engine.set_game_settings("songmaster-token", settings(offset=121))

    assert result.applied is False
    assert result.warning is not None
    assert result.warning.warning_codes == ("large_playback_offset",)
    assert result.warning.playback_offset_seconds == 121
    assert result.warning.playback_offset_warning_threshold_seconds == 120
    assert game.settings == settings()


def test_contestant_cannot_change_settings() -> None:
    engine, game = lobby_engine()
    engine.join_game("contestant-token", "Contestant")

    with pytest.raises(UnauthorizedGameSettingsError):
        engine.set_game_settings("contestant-token", settings(duration=20))

    assert game.settings == settings()


@pytest.mark.parametrize(
    "proposal",
    [
        GameSettings(-1, 30, Decimal(10)),
        GameSettings(30, 0, Decimal(10)),
        GameSettings(30, 30, Decimal(-1)),
        GameSettings(30, 30, Decimal(101)),
        GameSettings(True, 30, Decimal(10)),
    ],
)
def test_invalid_settings_are_rejected_atomically(proposal: GameSettings) -> None:
    engine, game = lobby_engine()
    before = settings()

    with pytest.raises(InvalidGameSettingsError):
        engine.set_game_settings("songmaster-token", proposal)

    assert game.settings == before
    assert game.pending_settings_confirmation is None


def test_settings_change_is_rejected_during_an_active_round_phase() -> None:
    engine, game = started_engine(120)
    engine.set_browser_audio_capability("songmaster-token", True)
    pending = engine.play_song("songmaster-token")
    assert game.pending_audio_preparation is pending

    with pytest.raises(GameSettingsNotAllowedError):
        engine.set_game_settings("songmaster-token", settings(duration=20))

    assert game.settings == settings()
    assert pending.playback_duration_seconds == 30


@pytest.mark.parametrize(
    "prohibited_state",
    [
        GameState.NO_GAME,
        GameState.AUDIO_PREPARING,
        GameState.PLAYING,
        GameState.AWAITING_REVEAL,
        GameState.SCORING,
        GameState.FINISHED,
    ],
)
def test_settings_guard_rejects_every_prohibited_state(
    prohibited_state: GameState,
) -> None:
    engine, game = lobby_engine()
    engine._state = prohibited_state  # isolate the state guard

    with pytest.raises(GameSettingsNotAllowedError):
        engine.set_game_settings("songmaster-token", settings(duration=20))

    assert game.settings == settings()


def test_exactly_half_ineligible_applies_without_warning() -> None:
    engine, game = started_engine(100, 100, 10, 10)

    result = engine.set_game_settings("songmaster-token", settings(duration=50))

    assert result.applied is True
    assert result.warning is None
    assert game.settings.playback_duration_seconds == 50
    assert game.deck_pointer == 0
    assert game.skipped_deck_entries == ()


def test_more_than_half_warning_is_safe_and_does_not_apply() -> None:
    engine, game = started_engine(40, 100, 10)

    result = engine.set_game_settings("songmaster-token", settings(duration=50))

    assert result.applied is False
    assert result.settings.playback_duration_seconds == 30
    assert result.warning is not None
    assert result.warning.remaining_song_count == 3
    assert result.warning.ineligible_song_count == 2
    assert isinstance(result.warning.confirmation_token, UUID)
    assert game.settings.playback_duration_seconds == 30
    assert game.deck_pointer == 0
    assert game.skipped_deck_entries == ()
    assert not hasattr(result.warning, "track_id")


def test_confirmation_rechecks_then_applies_and_rescans_candidate() -> None:
    engine, game = started_engine(40, 100, 10)
    warning = engine.set_game_settings(
        "songmaster-token", settings(duration=50)
    ).warning
    assert warning is not None

    result = engine.set_game_settings(
        "songmaster-token",
        settings(duration=50),
        duration_warning_confirmation_token=warning.confirmation_token,
    )

    assert result.applied is True
    assert game.settings.playback_duration_seconds == 50
    assert game.deck_pointer == 1
    assert [skip.original_position for skip in game.skipped_deck_entries] == [1]
    assert game.deck is not None
    assert game.deck[game.deck_pointer].track.track_id == 2


def test_confirmation_is_stale_after_intervening_settings_change() -> None:
    engine, game = started_engine(40, 100, 10)
    warning = engine.set_game_settings(
        "songmaster-token", settings(duration=50)
    ).warning
    assert warning is not None
    engine.set_game_settings("songmaster-token", settings(duration=20))

    with pytest.raises(GameSettingsConfirmationStaleError):
        engine.set_game_settings(
            "songmaster-token",
            settings(duration=50),
            duration_warning_confirmation_token=warning.confirmation_token,
        )

    assert game.settings.playback_duration_seconds == 20
    assert game.deck_pointer == 0


def test_large_offset_confirmation_applies_without_an_upper_limit() -> None:
    engine, game = lobby_engine()
    warning = engine.set_game_settings("songmaster-token", settings(offset=300)).warning
    assert warning is not None

    result = engine.set_game_settings(
        "songmaster-token",
        settings(offset=300),
        duration_warning_confirmation_token=warning.confirmation_token,
    )

    assert result.applied is True
    assert result.warning is None
    assert game.settings.playback_offset_seconds == 300


def test_one_confirmation_covers_large_offset_and_duration_warning() -> None:
    engine, game = started_engine(40, 100, 10)

    proposed = settings(offset=300, duration=50)
    first = engine.set_game_settings("songmaster-token", proposed)

    assert first.applied is False
    assert first.warning is not None
    assert first.warning.warning_codes == (
        "duration_ineligible_majority",
        "large_playback_offset",
    )
    assert game.settings == settings()
    assert game.deck_pointer == 0
    assert game.skipped_deck_entries == ()

    confirmed = engine.set_game_settings(
        "songmaster-token",
        proposed,
        duration_warning_confirmation_token=first.warning.confirmation_token,
    )

    assert confirmed.applied is True
    assert game.settings == proposed
    assert game.deck_pointer == 1


def test_rejected_large_offset_confirmation_does_not_mutate_settings() -> None:
    engine, game = lobby_engine()
    warning = engine.set_game_settings("songmaster-token", settings(offset=300)).warning
    assert warning is not None

    with pytest.raises(GameSettingsConfirmationStaleError):
        engine.set_game_settings(
            "songmaster-token",
            settings(offset=301),
            duration_warning_confirmation_token=warning.confirmation_token,
        )

    assert game.settings == settings()


def test_only_connected_songmaster_can_confirm_large_offset() -> None:
    engine, game = lobby_engine()
    engine.join_game("contestant-token", "Contestant")
    warning = engine.set_game_settings("songmaster-token", settings(offset=300)).warning
    assert warning is not None

    with pytest.raises(UnauthorizedGameSettingsError):
        engine.set_game_settings(
            "contestant-token",
            settings(offset=300),
            duration_warning_confirmation_token=warning.confirmation_token,
        )

    assert game.settings == settings()


def test_offset_and_plateau_change_preserve_immutable_deck() -> None:
    engine, game = started_engine(120, 100, 80)
    assert game.deck is not None
    deck_before = game.deck

    proposed = settings(offset=500, duration=30, plateau=Decimal(100))
    warning = engine.set_game_settings(
        "songmaster-token",
        proposed,
    ).warning
    assert warning is not None

    result = engine.set_game_settings(
        "songmaster-token",
        proposed,
        duration_warning_confirmation_token=warning.confirmation_token,
    )

    assert result.applied is True
    assert game.deck is deck_before
    assert [entry.track.track_id for entry in game.deck] == [1, 2, 3]
