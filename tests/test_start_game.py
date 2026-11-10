from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from typing import NoReturn
from uuid import UUID

import pytest

from who_sings_that.domain.deck import EmptyDeckError, TrackReference
from who_sings_that.domain.engine import (
    DomainInvariantError,
    GameEngine,
    InvalidResolvedPlaylistError,
    NoPlaylistSelectedError,
    StartGameNotAllowedError,
    UnauthorizedStartGameError,
)
from who_sings_that.domain.models import (
    DeckSkipReason,
    FinalStanding,
    GameEndReason,
    GameSession,
    GameState,
    ResolvedPlaylist,
    SelectedPlaylistSnapshot,
    SkippedDeckEntry,
)

CREATED_AT = datetime(2026, 11, 1, 12, 30, tzinfo=UTC)
STARTED_AT = datetime(2026, 11, 1, 12, 35, tzinfo=UTC)
ENDED_AT = datetime(2026, 11, 1, 12, 36, tzinfo=UTC)
PLAYLIST_CREATED_AT = datetime(2024, 5, 4, 9, 15, tzinfo=UTC)
GAME_ID = UUID("12345678-1234-5678-1234-567812345678")


class SequenceClock:
    def __init__(self, *values: datetime) -> None:
        self._values = iter(values)

    def __call__(self) -> datetime:
        return next(self._values)


class FailingStartClock:
    def __init__(self) -> None:
        self._calls = 0

    def __call__(self) -> datetime:
        self._calls += 1
        if self._calls == 1:
            return CREATED_AT
        raise RuntimeError("clock failed")


def unchanged(tracks: list[TrackReference]) -> None:
    """Leave build_deck's unique-track order unchanged."""


def failing_shuffle(tracks: list[TrackReference]) -> NoReturn:
    raise RuntimeError("shuffle failed")


def make_engine(*, shuffle=unchanged) -> GameEngine:
    return GameEngine(
        clock=SequenceClock(CREATED_AT, STARTED_AT, ENDED_AT),
        game_id_factory=lambda: GAME_ID,
        shuffle=shuffle,
    )


def resolved_playlist(
    playlist_id: str,
    *tracks: TrackReference,
    name: str | None = None,
    song_count: int | None = None,
) -> ResolvedPlaylist:
    return ResolvedPlaylist(
        snapshot=SelectedPlaylistSnapshot(
            playlist_id=playlist_id,
            playlist_name=name or f"Playlist {playlist_id}",
            playlist_created_at=PLAYLIST_CREATED_AT,
            playlist_song_count=(len(tracks) if song_count is None else song_count),
        ),
        tracks=tracks,
    )


def create_selected_lobby(
    engine: GameEngine, *playlist_ids: str
) -> GameSession:
    game = engine.create_game("songmaster-token", "Songmaster")
    for playlist_id in playlist_ids:
        returned = engine.select_playlist("songmaster-token", playlist_id)
        assert returned is game
    engine.validate_invariants()
    return game


def test_songmaster_start_game_establishes_transition_b_facts() -> None:
    engine = make_engine()
    lobby = create_selected_lobby(engine, "rock")
    songmaster = lobby.songmaster
    player = engine.join_game("player-token", "Player")
    metadata = resolved_playlist(
        "rock",
        TrackReference(1, 180),
        TrackReference(2, 200),
        name="Road Trip",
    )

    game = engine.start_game("songmaster-token", [metadata])

    assert engine.state is GameState.ROUND_COMPLETE
    assert game is lobby
    assert engine.active_game is game
    assert game.songmaster is songmaster
    assert game.members["player-token"] is player
    assert game.selected_playlist_snapshots == (metadata.snapshot,)
    assert game.selected_playlist_ids == frozenset({"rock"})
    assert isinstance(game.selected_playlist_ids, frozenset)
    assert game.playlist_selection_editable is False
    assert game.original_deck_size == 2
    assert game.deck_pointer == 0
    assert game.deck_exhausted is False
    assert game.started_at == STARTED_AT
    assert game.ended_at is None
    assert game.end_reason is None
    assert game.final_standings == ()
    assert game.active_round is None
    assert game.displayed_deck_position is None
    assert game.displayed_artist is None
    assert game.displayed_title is None
    engine.validate_invariants()


def test_start_game_rejects_non_songmaster_without_touching_lobby() -> None:
    engine = make_engine()
    lobby = create_selected_lobby(engine, "rock")
    engine.join_game("player-token", "Player")
    before = deepcopy(lobby)

    with pytest.raises(UnauthorizedStartGameError, match="current Songmaster"):
        engine.start_game(
            "player-token",
            [resolved_playlist("rock", TrackReference(1, 180))],
        )

    assert engine.state is GameState.LOBBY
    assert engine.active_game is lobby
    assert lobby == before
    engine.validate_invariants()


def test_start_game_rejects_request_outside_lobby() -> None:
    engine = make_engine()

    with pytest.raises(StartGameNotAllowedError, match="only allowed in LOBBY"):
        engine.start_game("songmaster-token", [])

    create_selected_lobby(engine, "rock")
    payload = [resolved_playlist("rock", TrackReference(1, 180))]
    engine.start_game("songmaster-token", payload)

    with pytest.raises(StartGameNotAllowedError, match="only allowed in LOBBY"):
        engine.start_game("songmaster-token", payload)


def test_start_game_requires_selected_playlist() -> None:
    engine = make_engine()
    lobby = engine.create_game("songmaster-token", "Songmaster")

    with pytest.raises(NoPlaylistSelectedError, match="at least one playlist"):
        engine.start_game("songmaster-token", [])

    assert engine.active_game is lobby
    assert engine.state is GameState.LOBBY
    engine.validate_invariants()


def test_start_game_rejects_empty_structural_deck_atomically() -> None:
    engine = make_engine()
    lobby = create_selected_lobby(engine, "empty")
    before = deepcopy(lobby)

    with pytest.raises(EmptyDeckError, match="without tracks"):
        engine.start_game("songmaster-token", [resolved_playlist("empty")])

    assert engine.active_game is lobby
    assert lobby == before
    assert engine.state is GameState.LOBBY
    engine.validate_invariants()


def test_selected_playlist_payload_must_match_lobby_selection_exactly() -> None:
    engine = make_engine()
    lobby = create_selected_lobby(engine, "rock", "pop")
    before = deepcopy(lobby)

    with pytest.raises(InvalidResolvedPlaylistError, match="exactly match"):
        engine.start_game(
            "songmaster-token",
            [resolved_playlist("rock", TrackReference(1, 180))],
        )

    assert engine.active_game is lobby
    assert lobby == before
    engine.validate_invariants()


def test_resolved_playlist_metadata_rejects_duplicate_and_impossible_counts() -> None:
    duplicate_engine = make_engine()
    duplicate_lobby = create_selected_lobby(duplicate_engine, "rock")
    track = TrackReference(1, 180)

    with pytest.raises(InvalidResolvedPlaylistError, match="exactly once"):
        duplicate_engine.start_game(
            "songmaster-token",
            [resolved_playlist("rock", track), resolved_playlist("rock", track)],
        )

    assert duplicate_engine.active_game is duplicate_lobby
    duplicate_engine.validate_invariants()

    count_engine = make_engine()
    count_lobby = create_selected_lobby(count_engine, "rock")
    with pytest.raises(InvalidResolvedPlaylistError, match="song count"):
        count_engine.start_game(
            "songmaster-token",
            [resolved_playlist("rock", track, song_count=0)],
        )

    assert count_engine.active_game is count_lobby
    count_engine.validate_invariants()


@pytest.mark.parametrize("track_id", ["1", 0, -1, True])
def test_resolved_tracks_require_positive_integer_catalog_ids(
    track_id: object,
) -> None:
    engine = make_engine()
    lobby = create_selected_lobby(engine, "rock")
    before = deepcopy(lobby)

    with pytest.raises(InvalidResolvedPlaylistError, match="positive integers"):
        engine.start_game(
            "songmaster-token",
            [
                resolved_playlist(
                    "rock",
                    TrackReference(track_id, 180),  # type: ignore[arg-type]
                )
            ],
        )

    assert engine.active_game is lobby
    assert lobby == before
    engine.validate_invariants()


def test_start_game_deduplicates_then_shuffles_exactly_once() -> None:
    shuffle_calls: list[list[int]] = []

    def recording_shuffle(tracks: list[TrackReference]) -> None:
        shuffle_calls.append([track.track_id for track in tracks])
        tracks.reverse()

    engine = make_engine(shuffle=recording_shuffle)
    create_selected_lobby(engine, "rock", "favorites")
    shared_first = TrackReference(1, 180)

    game = engine.start_game(
        "songmaster-token",
        [
            resolved_playlist(
                "rock",
                shared_first,
                TrackReference(2, 190),
            ),
            resolved_playlist(
                "favorites",
                TrackReference(1, 999),
                TrackReference(3, 200),
            ),
        ],
    )

    assert shuffle_calls == [[1, 2, 3]]
    assert game.deck is not None
    assert [entry.track.track_id for entry in game.deck] == [
        3,
        2,
        1,
    ]
    assert game.deck[2].track is shared_first


def test_started_deck_and_permanent_positions_are_immutable() -> None:
    engine = make_engine()
    create_selected_lobby(engine, "rock")
    game = engine.start_game(
        "songmaster-token",
        [
            resolved_playlist(
                "rock",
                TrackReference(1, 180),
                TrackReference(2, 180),
            )
        ],
    )

    assert isinstance(game.deck, tuple)
    assert game.deck is not None
    assert [entry.original_position for entry in game.deck] == [1, 2]
    with pytest.raises(FrozenInstanceError):
        game.deck[0].original_position = 99  # type: ignore[misc]


def test_round_complete_entry_skips_consecutive_short_tracks_and_advances_pointer(
) -> None:
    engine = make_engine()
    create_selected_lobby(engine, "mixed")

    game = engine.start_game(
        "songmaster-token",
        [
            resolved_playlist(
                "mixed",
                TrackReference(1, 10),
                TrackReference(2, 29),
                TrackReference(3, 30),
                TrackReference(4, 180),
            )
        ],
    )

    assert game.deck_pointer == 2
    assert game.deck_exhausted is False
    assert game.deck is not None
    assert game.deck[game.deck_pointer].track.track_id == 3
    assert game.skipped_deck_entries == (
        SkippedDeckEntry(1, 1, DeckSkipReason.DURATION_INELIGIBLE),
        SkippedDeckEntry(2, 2, DeckSkipReason.DURATION_INELIGIBLE),
    )
    engine.validate_invariants()


def test_round_complete_entry_preserves_player_facing_display_on_every_entry() -> None:
    engine = make_engine()
    create_selected_lobby(engine, "rock")
    game = engine.start_game(
        "songmaster-token",
        [
            resolved_playlist(
                "rock",
                TrackReference(1, 180),
                TrackReference(2, 10),
                TrackReference(3, 180),
            )
        ],
    )
    game.deck_pointer = 1
    game.displayed_deck_position = 1
    game.displayed_artist = "The Police"
    game.displayed_title = "Every Breath You Take"

    engine._enter_state(GameState.ROUND_COMPLETE)

    assert game.deck_pointer == 2
    assert game.skipped_deck_entries == (
        SkippedDeckEntry(2, 2, DeckSkipReason.DURATION_INELIGIBLE),
    )
    assert game.displayed_deck_position == 1
    assert game.displayed_artist == "The Police"
    assert game.displayed_title == "Every Breath You Take"
    engine.validate_invariants()


def test_all_short_tracks_finish_with_zero_played_rounds_and_zero_score() -> None:
    engine = make_engine()
    game = create_selected_lobby(engine, "short")

    returned = engine.start_game(
        "songmaster-token",
        [
            resolved_playlist(
                "short",
                TrackReference(1, 10),
                TrackReference(2, 20),
            )
        ],
    )

    assert returned is game
    assert engine.state is GameState.FINISHED
    assert game.deck is not None
    assert game.deck_pointer == len(game.deck)
    assert game.deck_exhausted is True
    assert len(game.skipped_deck_entries) == len(game.deck)
    assert game.active_round is None
    assert game.displayed_deck_position is None
    assert game.end_reason is GameEndReason.DECK_EXHAUSTED
    assert game.ended_at == ENDED_AT
    assert game.final_standings == (
        FinalStanding(1, "songmaster-token", "Songmaster", 0),
    )
    engine.validate_invariants()


def test_finished_standings_are_frozen_sorted_and_preserve_ties() -> None:
    engine = make_engine()
    lobby = create_selected_lobby(engine, "short")
    second = engine.join_game("second-token", "Second")
    third = engine.join_game("third-token", "Third")
    lobby.songmaster.committed_score = 40
    second.committed_score = 90
    third.committed_score = 40
    original_members = dict(lobby.members)

    game = engine.start_game(
        "songmaster-token",
        [
            resolved_playlist(
                "short",
                TrackReference(1, 10),
                TrackReference(2, 20),
            )
        ],
    )

    assert engine.state is GameState.FINISHED
    assert engine.active_game is lobby
    assert game is lobby
    assert all(game.members[token] is member for token, member in original_members.items())
    assert game.deck is not None
    assert game.deck_pointer == len(game.deck)
    assert game.deck_exhausted is True
    assert game.skipped_deck_entries == (
        SkippedDeckEntry(1, 1, DeckSkipReason.DURATION_INELIGIBLE),
        SkippedDeckEntry(2, 2, DeckSkipReason.DURATION_INELIGIBLE),
    )
    assert game.active_round is None
    assert game.displayed_deck_position is None
    assert game.end_reason is GameEndReason.DECK_EXHAUSTED
    assert game.ended_at == ENDED_AT
    assert game.final_standings == (
        FinalStanding(1, "second-token", "Second", 90),
        FinalStanding(2, "songmaster-token", "Songmaster", 40),
        FinalStanding(2, "third-token", "Third", 40),
    )
    assert game.songmaster is original_members["songmaster-token"]
    with pytest.raises(FrozenInstanceError):
        game.final_standings[0].score = 0  # type: ignore[misc]
    engine.validate_invariants()
    second.committed_score = 91
    with pytest.raises(DomainInvariantError, match="final standings"):
        engine.validate_invariants()


class CorruptingRoundCompleteEntryEngine(GameEngine):
    def _enter_round_complete(self) -> None:
        super()._enter_round_complete()
        assert self.active_game is not None
        self.active_game.original_deck_size = 0


@pytest.mark.parametrize(
    ("engine_factory", "error_type", "message"),
    [
        (
            lambda: GameEngine(
                clock=SequenceClock(CREATED_AT, STARTED_AT),
                game_id_factory=lambda: GAME_ID,
                shuffle=failing_shuffle,
            ),
            RuntimeError,
            "shuffle failed",
        ),
        (
            lambda: GameEngine(
                clock=FailingStartClock(),
                game_id_factory=lambda: GAME_ID,
                shuffle=unchanged,
            ),
            RuntimeError,
            "clock failed",
        ),
    ],
)
def test_dependency_preparation_failures_leave_authoritative_lobby_untouched(
    engine_factory,
    error_type: type[Exception],
    message: str | None,
) -> None:
    engine = engine_factory()
    lobby = create_selected_lobby(engine, "rock")
    before = deepcopy(lobby)

    with pytest.raises(error_type, match=message):
        engine.start_game(
            "songmaster-token",
            [resolved_playlist("rock", TrackReference(1, 180))],
        )

    assert engine.state is GameState.LOBBY
    assert engine.active_game is lobby
    assert lobby == before
    engine.validate_invariants()


def test_unexpected_committed_invariant_violation_is_raised_without_copy_rollback(
) -> None:
    engine = CorruptingRoundCompleteEntryEngine(
        clock=SequenceClock(CREATED_AT, STARTED_AT),
        game_id_factory=lambda: GAME_ID,
        shuffle=unchanged,
    )
    game = create_selected_lobby(engine, "rock")
    songmaster = game.songmaster

    with pytest.raises(DomainInvariantError, match="original deck size"):
        engine.start_game(
            "songmaster-token",
            [resolved_playlist("rock", TrackReference(1, 180))],
        )

    assert engine.state is GameState.ROUND_COMPLETE
    assert engine.active_game is game
    assert game.songmaster is songmaster
    assert game.deck is not None
    assert game.playlist_selection_editable is False


def test_resolver_iteration_failure_leaves_authoritative_lobby_untouched() -> None:
    engine = make_engine()
    lobby = create_selected_lobby(engine, "rock")
    before = deepcopy(lobby)

    def failing_result():
        yield resolved_playlist("rock", TrackReference(1, 180))
        raise RuntimeError("resolver result failed")

    with pytest.raises(RuntimeError, match="resolver result failed"):
        engine.start_game("songmaster-token", failing_result())

    assert engine.state is GameState.LOBBY
    assert engine.active_game is lobby
    assert lobby == before
    engine.validate_invariants()


def test_round_complete_invariant_rejects_unscanned_pointer() -> None:
    engine = make_engine()
    create_selected_lobby(engine, "mixed")
    game = engine.start_game(
        "songmaster-token",
        [
            resolved_playlist(
                "mixed",
                TrackReference(1, 10),
                TrackReference(2, 60),
            )
        ],
    )
    assert game.deck_pointer == 1
    game.deck_pointer = 0

    with pytest.raises(
        DomainInvariantError,
        match="skipped entries must precede|already identify",
    ):
        engine.validate_invariants()
