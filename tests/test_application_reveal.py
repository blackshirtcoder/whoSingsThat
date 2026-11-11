from collections.abc import Iterator
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine

from who_sings_that.application import (
    GameController,
    PlaySongRequest,
    RevealRequest,
    StaleGameSessionError,
)
from who_sings_that.database import create_sqlite_engine, upgrade_database
from who_sings_that.domain import (
    GameEngine,
    GameFinalizationResult,
    GameSettings,
    GameState,
    ResolvedPlaylist,
    RevealNotAllowedError,
    SelectedPlaylistSnapshot,
    TrackReference,
    UnauthorizedRevealError,
)

GAME_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
PREPARATION_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
ROUND_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
CREATED_AT = datetime(2026, 11, 11, 10, 0, tzinfo=UTC)
STARTED_AT = datetime(2026, 11, 11, 10, 1, tzinfo=UTC)
PLAYBACK_AT = datetime(2026, 11, 11, 10, 2, tzinfo=UTC)
ENDED_AT = datetime(2026, 11, 11, 10, 3, tzinfo=UTC)
PLAYLIST_CREATED_AT = datetime(2025, 1, 2, 3, 4, tzinfo=UTC)
TRACK = TrackReference(10, 180, "Secret Artist", "Secret Title")
SECOND_TRACK = TrackReference(20, 180, "Second Artist", "Second Title")


class SequenceClock:
    def __init__(self) -> None:
        self._values = iter((CREATED_AT, STARTED_AT, PLAYBACK_AT, ENDED_AT))

    def __call__(self) -> datetime:
        return next(self._values)


class MutableMonotonicClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class ObservingController(GameController):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.reveal_dispatch_states: list[GameState] = []

    def _dispatch(self, message: object) -> object:
        try:
            return super()._dispatch(message)  # type: ignore[arg-type]
        finally:
            if isinstance(message, RevealRequest):
                self.reveal_dispatch_states.append(self.game_engine.state)


@pytest.fixture
def controller_storage(tmp_path: Path) -> Iterator[tuple[Engine, Path]]:
    database_path = tmp_path / "controller.sqlite3"
    upgrade_database(database_path)
    database_engine = create_sqlite_engine(database_path)
    music_root = tmp_path / "music"
    music_root.mkdir()
    yield database_engine, music_root
    database_engine.dispose()


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


def awaiting_engine(*tracks: TrackReference, buzz: bool):
    monotonic_clock = MutableMonotonicClock()
    engine = GameEngine(
        GameSettings(playback_duration_seconds=30),
        clock=SequenceClock(),
        monotonic_clock=monotonic_clock,
        game_id_factory=lambda: GAME_ID,
        preparation_id_factory=lambda: PREPARATION_ID,
        round_id_factory=lambda: ROUND_ID,
        shuffle=lambda values: None,
    )
    game = engine.create_game("songmaster-token", "Songmaster")
    engine.join_game("player-token", "Player")
    engine.set_browser_audio_capability("songmaster-token", True)
    engine.select_playlist("songmaster-token", "playlist")
    engine.start_game("songmaster-token", [resolved_playlist(*tracks)])
    pending = engine.play_song("songmaster-token")
    engine.record_audio_preparation_ready(
        GAME_ID,
        pending.preparation_id,
        "songmaster-token",
    )
    assert game.active_round is not None
    if buzz:
        engine.accept_buzz(
            GAME_ID,
            game.active_round.round_id,
            "player-token",
            102.0,
        )
    else:
        monotonic_clock.value = game.active_round.playback_deadline_monotonic
        engine.playback_timed_out(
            GAME_ID,
            game.active_round.round_id,
            monotonic_clock.value,
        )
    return engine, game


def make_controller(
    controller_storage: tuple[Engine, Path],
    engine: GameEngine,
) -> ObservingController:
    database_engine, music_root = controller_storage
    return ObservingController(database_engine, music_root, game_engine=engine)


def test_reveal_request_contains_identity_and_authorization_only() -> None:
    assert [field.name for field in fields(RevealRequest)] == [
        "game_session_id",
        "round_id",
        "requesting_browser_token",
    ]


@pytest.mark.asyncio
async def test_controller_dispatches_transition_h_and_returns_success(
    controller_storage: tuple[Engine, Path],
) -> None:
    engine, game = awaiting_engine(TRACK, SECOND_TRACK, buzz=True)
    controller = make_controller(controller_storage, engine)
    await controller.start()

    result = await controller.submit_request(
        RevealRequest(GAME_ID, ROUND_ID, "songmaster-token")
    )

    assert result is game
    assert controller.reveal_dispatch_states == [GameState.SCORING]
    assert engine.state is GameState.SCORING
    assert game.displayed_artist == "Secret Artist"
    assert game.displayed_title == "Secret Title"
    await controller.stop()


@pytest.mark.asyncio
async def test_invalid_reveal_is_rejected_without_answer_leak_or_consumer_failure(
    controller_storage: tuple[Engine, Path],
) -> None:
    engine, game = awaiting_engine(TRACK, buzz=True)
    controller = make_controller(controller_storage, engine)
    await controller.start()

    with pytest.raises(UnauthorizedRevealError) as raised:
        await controller.submit_request(
            RevealRequest(GAME_ID, ROUND_ID, "player-token")
        )

    assert "Secret Artist" not in str(raised.value)
    assert "Secret Title" not in str(raised.value)
    assert game.displayed_artist is None
    assert game.displayed_title is None
    assert controller.is_running is True

    result = await controller.submit_request(
        RevealRequest(GAME_ID, ROUND_ID, "songmaster-token")
    )
    assert result is game
    assert engine.state is GameState.SCORING
    assert controller.is_running is True
    await controller.stop()


@pytest.mark.asyncio
async def test_wrong_game_reveal_is_an_expected_application_rejection(
    controller_storage: tuple[Engine, Path],
) -> None:
    engine, game = awaiting_engine(TRACK, buzz=True)
    controller = make_controller(controller_storage, engine)
    await controller.start()

    with pytest.raises(StaleGameSessionError):
        await controller.submit_request(
            RevealRequest(uuid4(), ROUND_ID, "songmaster-token")
        )

    assert engine.state is GameState.AWAITING_REVEAL
    assert game.displayed_artist is None
    assert controller.is_running is True
    await controller.stop()


@pytest.mark.asyncio
async def test_controller_transition_i_reaches_round_complete_in_one_dispatch(
    controller_storage: tuple[Engine, Path],
) -> None:
    engine, game = awaiting_engine(TRACK, SECOND_TRACK, buzz=False)
    controller = make_controller(controller_storage, engine)
    await controller.start()

    result = await controller.submit_request(
        RevealRequest(GAME_ID, ROUND_ID, "songmaster-token")
    )

    assert result is game
    assert controller.reveal_dispatch_states == [GameState.ROUND_COMPLETE]
    assert engine.state is GameState.ROUND_COMPLETE
    assert game.deck_pointer == 1
    assert game.displayed_artist == "Secret Artist"
    assert game.displayed_title == "Secret Title"
    await controller.stop()


@pytest.mark.asyncio
async def test_controller_transition_i_waits_for_play_song_to_take_k(
    controller_storage: tuple[Engine, Path],
) -> None:
    engine, game = awaiting_engine(TRACK, buzz=False)
    controller = make_controller(controller_storage, engine)
    await controller.start()

    result = await controller.submit_request(
        RevealRequest(GAME_ID, ROUND_ID, "songmaster-token")
    )

    assert result is game
    assert controller.reveal_dispatch_states == [GameState.ROUND_COMPLETE]
    assert engine.state is GameState.ROUND_COMPLETE
    assert game.deck_pointer == 1
    assert game.deck_exhausted is True
    assert game.final_standings == ()
    assert game.displayed_artist == "Secret Artist"
    assert game.displayed_title == "Secret Title"

    with pytest.raises(RevealNotAllowedError):
        await controller.submit_request(
            RevealRequest(GAME_ID, ROUND_ID, "songmaster-token")
        )

    finalization = await controller.submit_request(
        PlaySongRequest(GAME_ID, "songmaster-token")
    )
    assert isinstance(finalization, GameFinalizationResult)
    assert engine.state is GameState.FINISHED
    assert game.displayed_artist == "Secret Artist"
    assert game.displayed_title == "Secret Title"
    assert controller.is_running is True
    await controller.stop()
