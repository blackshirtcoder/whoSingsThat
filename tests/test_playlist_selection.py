from copy import deepcopy
from datetime import UTC, datetime

import pytest

from who_sings_that.domain.deck import TrackReference
from who_sings_that.domain.engine import (
    GameEngine,
    InvalidPlaylistIdError,
    PlaylistSelectionNotAllowedError,
    UnauthorizedPlaylistSelectionError,
)
from who_sings_that.domain.models import (
    GameState,
    ResolvedPlaylist,
    SelectedPlaylistSnapshot,
)

CREATED_AT = datetime(2026, 11, 1, 12, 30, tzinfo=UTC)
STARTED_AT = datetime(2026, 11, 1, 12, 35, tzinfo=UTC)
PLAYLIST_CREATED_AT = datetime(2024, 5, 4, 9, 15, tzinfo=UTC)


class SequenceClock:
    def __init__(self, *values: datetime) -> None:
        self._values = iter(values)

    def __call__(self) -> datetime:
        return next(self._values)


class EntryRecordingGameEngine(GameEngine):
    def __init__(self) -> None:
        self.entered_states: list[GameState] = []
        super().__init__(clock=SequenceClock(CREATED_AT, STARTED_AT))

    def _enter_state(self, state: GameState) -> None:
        self.entered_states.append(state)
        super()._enter_state(state)


def unchanged(tracks: list[TrackReference]) -> None:
    """Leave build_deck's unique-track order unchanged."""


def resolved_playlist(playlist_id: str) -> ResolvedPlaylist:
    return ResolvedPlaylist(
        snapshot=SelectedPlaylistSnapshot(
            playlist_id=playlist_id,
            playlist_name=f"Playlist {playlist_id}",
            playlist_created_at=PLAYLIST_CREATED_AT,
            playlist_song_count=1,
        ),
        tracks=(TrackReference(1, 180),),
    )


def test_songmaster_selects_and_deselects_multiple_playlists_in_lobby() -> None:
    engine = EntryRecordingGameEngine()
    game = engine.create_game("songmaster-token", "Songmaster")

    first_result = engine.select_playlist("songmaster-token", "rock")
    second_result = engine.select_playlist("songmaster-token", "favorites")

    assert first_result is game
    assert second_result is game
    assert engine.active_game is game
    assert engine.state is GameState.LOBBY
    assert game.selected_playlist_ids == {"rock", "favorites"}

    deselected = engine.deselect_playlist("songmaster-token", "rock")

    assert deselected is game
    assert game.selected_playlist_ids == {"favorites"}
    assert engine.entered_states == [GameState.NO_GAME, GameState.LOBBY]
    engine.validate_invariants()


def test_repeated_selection_and_deselection_are_idempotent() -> None:
    engine = GameEngine(clock=lambda: CREATED_AT)
    game = engine.create_game("songmaster-token", "Songmaster")

    engine.select_playlist("songmaster-token", "rock")
    engine.select_playlist("songmaster-token", "rock")
    assert game.selected_playlist_ids == {"rock"}

    engine.deselect_playlist("songmaster-token", "missing")
    engine.deselect_playlist("songmaster-token", "rock")
    engine.deselect_playlist("songmaster-token", "rock")
    assert game.selected_playlist_ids == set()
    assert engine.active_game is game
    assert engine.state is GameState.LOBBY
    engine.validate_invariants()


@pytest.mark.parametrize(
    "operation",
    [
        lambda engine: engine.select_playlist("player-token", "rock"),
        lambda engine: engine.deselect_playlist("player-token", "rock"),
        lambda engine: engine.select_playlist("", "rock"),
        lambda engine: engine.deselect_playlist("unknown-token", "rock"),
    ],
)
def test_non_songmaster_playlist_operations_are_rejected_unchanged(operation) -> None:
    engine = GameEngine(clock=lambda: CREATED_AT)
    game = engine.create_game("songmaster-token", "Songmaster")
    engine.join_game("player-token", "Player")
    engine.select_playlist("songmaster-token", "existing")
    before = deepcopy(game)

    with pytest.raises(UnauthorizedPlaylistSelectionError):
        operation(engine)

    assert engine.active_game is game
    assert game == before
    assert engine.state is GameState.LOBBY
    engine.validate_invariants()


@pytest.mark.parametrize("playlist_id", ["", " ", "\t\n", None, 42])
@pytest.mark.parametrize("method_name", ["select_playlist", "deselect_playlist"])
def test_invalid_playlist_ids_are_rejected_unchanged(
    method_name: str, playlist_id: object
) -> None:
    engine = GameEngine(clock=lambda: CREATED_AT)
    game = engine.create_game("songmaster-token", "Songmaster")
    engine.select_playlist("songmaster-token", "existing")
    before = deepcopy(game)

    operation = getattr(engine, method_name)
    with pytest.raises(InvalidPlaylistIdError, match="playlist_id"):
        operation("songmaster-token", playlist_id)

    assert engine.active_game is game
    assert game == before
    assert engine.state is GameState.LOBBY
    engine.validate_invariants()


@pytest.mark.parametrize("method_name", ["select_playlist", "deselect_playlist"])
def test_playlist_operations_are_rejected_after_start_game(method_name: str) -> None:
    engine = GameEngine(
        clock=SequenceClock(CREATED_AT, STARTED_AT),
        shuffle=unchanged,
    )
    game = engine.create_game("songmaster-token", "Songmaster")
    engine.select_playlist("songmaster-token", "rock")
    engine.start_game("songmaster-token", [resolved_playlist("rock")])
    before = deepcopy(game)

    operation = getattr(engine, method_name)
    with pytest.raises(PlaylistSelectionNotAllowedError, match="only allowed in LOBBY"):
        operation("songmaster-token", "rock")

    assert engine.active_game is game
    assert game == before
    assert game.selected_playlist_ids == frozenset({"rock"})
    assert game.playlist_selection_editable is False
    assert engine.state is GameState.ROUND_COMPLETE
    engine.validate_invariants()


@pytest.mark.parametrize("method_name", ["select_playlist", "deselect_playlist"])
def test_playlist_operations_are_rejected_without_a_lobby(method_name: str) -> None:
    engine = GameEngine(clock=lambda: CREATED_AT)

    operation = getattr(engine, method_name)
    with pytest.raises(PlaylistSelectionNotAllowedError, match="only allowed in LOBBY"):
        operation("songmaster-token", "rock")

    assert engine.active_game is None
    assert engine.state is GameState.NO_GAME


def test_start_game_uses_playlists_selected_through_domain_operations() -> None:
    engine = GameEngine(
        clock=SequenceClock(CREATED_AT, STARTED_AT),
        shuffle=unchanged,
    )
    game = engine.create_game("songmaster-token", "Songmaster")
    engine.select_playlist("songmaster-token", "rock")
    engine.select_playlist("songmaster-token", "favorites")
    engine.deselect_playlist("songmaster-token", "favorites")

    started = engine.start_game(
        "songmaster-token",
        [resolved_playlist("rock")],
    )

    assert started is game
    assert engine.active_game is game
    assert engine.state is GameState.ROUND_COMPLETE
    assert game.selected_playlist_ids == frozenset({"rock"})
    assert game.selected_playlist_snapshots[0].playlist_id == "rock"
    assert game.playlist_selection_editable is False
    engine.validate_invariants()
