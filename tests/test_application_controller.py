import asyncio
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine

from who_sings_that.application import (
    BrowserAudioCapabilityChangedEvent,
    CancelGameRequest,
    ClientRequest,
    ConsumerAlreadyRunningError,
    ConsumerSelfRequestError,
    ControllerFailedError,
    ControllerNotRunningError,
    CreateGameRequest,
    DeselectPlaylistRequest,
    GameController,
    JoinGameRequest,
    MemberDisconnectedEvent,
    PlaySongRequest,
    SelectPlaylistRequest,
    SetAudioPreferenceRequest,
    StaleGameSessionError,
    StartGameRequest,
    SystemEvent,
)
from who_sings_that.catalog import (
    MediaStatus,
    MetadataSource,
    MetadataStatus,
    TrackMetadata,
)
from who_sings_that.database import (
    add_tracks_to_playlist,
    create_playlist,
    create_sqlite_engine,
    import_tracks,
    upgrade_database,
)
from who_sings_that.domain import (
    GameEndReason,
    GameEngine,
    GameFinalizationResult,
    GameSession,
    GameState,
    UnauthorizedPlaylistSelectionError,
)
from who_sings_that.domain.engine import UnauthorizedPlaySongError

GAME_ID = UUID("12345678-1234-5678-1234-567812345678")
CREATED_AT = datetime(2026, 11, 10, 10, 0, tzinfo=UTC)
STARTED_AT = datetime(2026, 11, 10, 10, 1, tzinfo=UTC)
ENDED_AT = datetime(2026, 11, 10, 10, 2, tzinfo=UTC)


class SequenceClock:
    def __init__(self, *values: datetime) -> None:
        self._values = iter(values)

    def __call__(self) -> datetime:
        return next(self._values)


@pytest.fixture
def controller_storage(tmp_path: Path) -> Iterator[tuple[Engine, Path]]:
    database_path = tmp_path / "controller.sqlite3"
    upgrade_database(database_path)
    database_engine = create_sqlite_engine(database_path)
    music_root = tmp_path / "music"
    music_root.mkdir()
    yield database_engine, music_root
    database_engine.dispose()


def make_game_engine() -> GameEngine:
    return GameEngine(
        clock=SequenceClock(CREATED_AT, STARTED_AT, ENDED_AT),
        game_id_factory=lambda: GAME_ID,
        shuffle=lambda tracks: None,
    )


def make_controller(
    controller_storage: tuple[Engine, Path],
    *,
    game_engine: GameEngine | None = None,
) -> GameController:
    database_engine, music_root = controller_storage
    return GameController(
        database_engine,
        music_root,
        game_engine=game_engine if game_engine is not None else make_game_engine(),
    )


def write_test_mp3(path: Path, *, frame_count: int = 1600) -> None:
    """Write deterministic MPEG frames that Mutagen can inspect."""

    path.parent.mkdir(parents=True, exist_ok=True)
    header = bytes.fromhex("fffb9064")
    frame = header + bytes(417 - len(header))
    path.write_bytes(frame * frame_count)


def create_persistent_playlist(
    database_engine: Engine,
    music_root: Path,
    *,
    relative_path: str = "Rock/Artist - Song.mp3",
    frame_count: int = 1600,
) -> str:
    write_test_mp3(music_root / relative_path, frame_count=frame_count)
    (track,) = import_tracks(
        database_engine,
        [
            TrackMetadata(
                relative_path=Path(relative_path),
                artist="Artist",
                title="Song",
                duration_seconds=180.0,
                file_size_bytes=10_000,
                metadata_source=MetadataSource.FILENAME,
                metadata_status=MetadataStatus.COMPLETE,
                media_status=MediaStatus.VALID,
            )
        ],
    )
    playlist = create_playlist(database_engine, "Controller Playlist")
    add_tracks_to_playlist(database_engine, playlist.playlist_id, [track.track_id])
    return playlist.playlist_id


async def create_game(controller: GameController) -> GameSession:
    result = await controller.submit_request(
        CreateGameRequest(
            "songmaster-token", "Songmaster", "songmaster-connection"
        )
    )
    assert isinstance(result, GameSession)
    return result


@pytest.mark.asyncio
async def test_controller_start_stop_and_multiple_consumer_prevention(
    controller_storage: tuple[Engine, Path],
) -> None:
    controller = make_controller(controller_storage)

    with pytest.raises(ControllerNotRunningError):
        await controller.submit_request(
            CreateGameRequest("token", "Player", "connection")
        )

    await controller.start()
    assert controller.is_running is True
    with pytest.raises(ConsumerAlreadyRunningError):
        await controller.start()

    await controller.stop()
    assert controller.is_running is False

    await controller.start()
    assert controller.is_running is True
    await controller.stop()


@pytest.mark.asyncio
async def test_waiting_empty_consumer_wakes_for_a_successful_request(
    controller_storage: tuple[Engine, Path],
) -> None:
    controller = make_controller(controller_storage)
    await controller.start()

    game = await create_game(controller)

    assert game is controller.game_engine.active_game
    assert controller.game_engine.state is GameState.LOBBY
    await controller.stop()


@pytest.mark.asyncio
async def test_membership_sync_retains_old_game_invalidation_across_new_game(
    controller_storage: tuple[Engine, Path],
) -> None:
    game_ids = iter((uuid4(), uuid4()))
    engine = GameEngine(
        clock=lambda: CREATED_AT,
        game_id_factory=lambda: next(game_ids),
    )
    controller = make_controller(controller_storage, game_engine=engine)
    await controller.start()

    first = await controller.submit_membership_request(
        CreateGameRequest("browser-token", "First", "first-connection")
    )
    first_game_id = first.update.game_session_id
    first_public_id = first.recipient.recipient.public_participant_id
    assert first.update.revision == 1

    await controller.submit_request(
        CancelGameRequest(first_game_id, "browser-token")
    )
    invalidated, invalidated_recipient = controller.projection_for_game_member(
        first_game_id,
        "browser-token",
    )
    assert invalidated.invalidated is True
    assert invalidated_recipient.connection_id == "first-connection"

    second = await controller.submit_membership_request(
        CreateGameRequest("browser-token", "Second", "second-connection")
    )
    assert second.update.game_session_id != first_game_id
    assert second.update.revision == 1
    assert second.recipient.recipient.public_participant_id != first_public_id

    retained, _ = controller.projection_for_game_member(
        first_game_id,
        "browser-token",
    )
    assert retained is invalidated
    await controller.stop()


@pytest.mark.asyncio
async def test_expected_request_rejection_does_not_stop_later_processing(
    controller_storage: tuple[Engine, Path],
) -> None:
    controller = make_controller(controller_storage)
    await controller.start()
    game = await create_game(controller)

    with pytest.raises(UnauthorizedPlaylistSelectionError):
        await controller.submit_request(
            SelectPlaylistRequest(game.game_session_id, "intruder", "playlist")
        )

    member = await controller.submit_request(
        JoinGameRequest(
            game.game_session_id,
            "player-token",
            "player-connection",
            "Player",
        )
    )

    assert member is game.members["player-token"]
    assert controller.is_running is True
    await controller.stop()


@pytest.mark.asyncio
async def test_stale_system_event_has_no_response_and_is_observably_rejected(
    controller_storage: tuple[Engine, Path],
) -> None:
    controller = make_controller(controller_storage)
    await controller.start()
    game = await create_game(controller)

    returned = controller.post_system_event(
        BrowserAudioCapabilityChangedEvent(
            uuid4(),
            "songmaster-token",
            "connection-1",
            True,
        )
    )
    # The following request is a FIFO barrier behind the event.
    member = await controller.submit_request(
        SetAudioPreferenceRequest(game.game_session_id, "songmaster-token", True)
    )

    assert returned is None
    assert member is game.songmaster
    assert len(controller.system_event_rejections) == 1
    rejection = controller.system_event_rejections[0]
    assert isinstance(rejection.error, StaleGameSessionError)
    assert controller.is_running is True
    await controller.stop()


@dataclass(frozen=True, slots=True)
class ProbeRequest(ClientRequest):
    name: str


@dataclass(frozen=True, slots=True)
class ProbeEvent(SystemEvent):
    name: str


class ProbeController(GameController):
    def __init__(self, database_engine: Engine, music_root: Path) -> None:
        super().__init__(database_engine, music_root)
        self.calls: list[str] = []

    def _dispatch(self, message: object) -> object:
        if isinstance(message, (ProbeRequest, ProbeEvent)):
            self.calls.append(f"start:{message.name}")
            if message.name == "A":
                self.post_system_event(ProbeEvent("C"))
            self.calls.append(f"end:{message.name}")
            return message.name
        return super()._dispatch(message)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_fifo_serialization_and_internal_posting_preserve_tail_order(
    controller_storage: tuple[Engine, Path],
) -> None:
    database_engine, music_root = controller_storage
    controller = ProbeController(database_engine, music_root)
    await controller.start()

    # These independent producer tasks are scheduled in insertion order. Both
    # enqueue before the woken consumer runs, so A's internal C follows B.
    first = asyncio.create_task(controller.submit_request(ProbeRequest("A")))
    second = asyncio.create_task(controller.submit_request(ProbeRequest("B")))
    assert await asyncio.gather(first, second) == ["A", "B"]

    # A FIFO request acts as a barrier behind response-free event C.
    assert await controller.submit_request(ProbeRequest("D")) == "D"
    assert controller.calls == [
        "start:A",
        "end:A",
        "start:B",
        "end:B",
        "start:C",
        "end:C",
        "start:D",
        "end:D",
    ]
    await controller.stop()


class SelfRequestProbeController(ProbeController):
    def _dispatch(self, message: object) -> object:
        if isinstance(message, ProbeRequest) and message.name == "outer":
            nested = self.submit_request(ProbeRequest("nested"))
            try:
                nested.send(None)
            except ConsumerSelfRequestError as error:
                self.self_request_error = error
            else:  # pragma: no cover - the guard is the behavior under test
                raise AssertionError("consumer self-request was not rejected")
            finally:
                nested.close()
            return "guarded"
        return super()._dispatch(message)


@pytest.mark.asyncio
async def test_consumer_self_request_is_rejected_instead_of_deadlocking(
    controller_storage: tuple[Engine, Path],
) -> None:
    database_engine, music_root = controller_storage
    controller = SelfRequestProbeController(database_engine, music_root)
    await controller.start()

    result = await controller.submit_request(ProbeRequest("outer"))

    assert result == "guarded"
    assert isinstance(controller.self_request_error, ConsumerSelfRequestError)
    assert controller.is_running is True
    await controller.stop()


class GatedController(GameController):
    def __init__(self, database_engine: Engine, music_root: Path) -> None:
        super().__init__(database_engine, music_root, game_engine=make_game_engine())
        self.release_consumer = asyncio.Event()
        self.request_started = asyncio.Event()

    async def submit_request(self, request: ClientRequest) -> object:
        self.request_started.set()
        return await super().submit_request(request)

    async def _consume_messages(self) -> None:
        assert self._consumer_ready is not None
        self._consumer_ready.set()
        await self.release_consumer.wait()
        await super()._consume_messages()


class DrainingProbeController(ProbeController):
    def __init__(self, database_engine: Engine, music_root: Path) -> None:
        super().__init__(database_engine, music_root)
        self.release_consumer = asyncio.Event()
        self.request_enqueued = {
            "first": asyncio.Event(),
            "second": asyncio.Event(),
        }
        self.stop_started = asyncio.Event()

    async def submit_request(self, request: ClientRequest) -> object:
        if isinstance(request, ProbeRequest):
            self.request_enqueued[request.name].set()
        return await super().submit_request(request)

    async def stop(self) -> None:
        self.stop_started.set()
        await super().stop()

    async def _consume_messages(self) -> None:
        assert self._consumer_ready is not None
        self._consumer_ready.set()
        await self.release_consumer.wait()
        await super()._consume_messages()


@pytest.mark.asyncio
async def test_stop_drains_already_queued_requests_before_consumer_exit(
    controller_storage: tuple[Engine, Path],
) -> None:
    database_engine, music_root = controller_storage
    controller = DrainingProbeController(database_engine, music_root)
    await controller.start()

    first = asyncio.create_task(controller.submit_request(ProbeRequest("first")))
    second = asyncio.create_task(controller.submit_request(ProbeRequest("second")))
    await controller.request_enqueued["first"].wait()
    await controller.request_enqueued["second"].wait()
    stopping = asyncio.create_task(controller.stop())
    await controller.stop_started.wait()
    assert controller.is_running is False

    controller.release_consumer.set()
    assert await asyncio.gather(first, second) == ["first", "second"]
    await stopping

    assert controller.calls == [
        "start:first",
        "end:first",
        "start:second",
        "end:second",
    ]


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_cancel_accepted_authoritative_work(
    controller_storage: tuple[Engine, Path],
) -> None:
    database_engine, music_root = controller_storage
    controller = GatedController(database_engine, music_root)
    await controller.start()

    waiter = asyncio.create_task(
        controller.submit_request(
            CreateGameRequest(
                "songmaster-token", "Songmaster", "songmaster-connection"
            )
        )
    )
    await controller.request_started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    controller.release_consumer.set()
    # A later request is a barrier proving the abandoned Create Game completed.
    member = await controller.submit_request(
        JoinGameRequest(GAME_ID, "player-token", "player-connection", "Player")
    )

    assert member.browser_token == "player-token"
    assert controller.game_engine.active_game is not None
    assert controller.game_engine.active_game.songmaster_browser_token == (
        "songmaster-token"
    )
    await controller.stop()


class FailingController(ProbeController):
    def _dispatch(self, message: object) -> object:
        if isinstance(message, ProbeRequest) and message.name == "fail":
            raise RuntimeError("unexpected handler failure")
        return super()._dispatch(message)


@pytest.mark.asyncio
async def test_unexpected_handler_error_fails_current_and_queued_requests(
    controller_storage: tuple[Engine, Path],
) -> None:
    database_engine, music_root = controller_storage
    controller = FailingController(database_engine, music_root)
    await controller.start()

    failing = asyncio.create_task(controller.submit_request(ProbeRequest("fail")))
    queued = asyncio.create_task(controller.submit_request(ProbeRequest("after")))

    with pytest.raises(RuntimeError, match="unexpected handler failure"):
        await failing
    with pytest.raises(ControllerFailedError) as queued_error:
        await queued

    assert queued_error.value.failure is controller.failure
    assert isinstance(controller.failure, RuntimeError)
    assert controller.is_running is False
    with pytest.raises(ControllerFailedError):
        controller.post_system_event(ProbeEvent("later"))
    with pytest.raises(ControllerFailedError):
        await controller.stop()


@pytest.mark.asyncio
async def test_dispatches_membership_playlist_and_connection_operations(
    controller_storage: tuple[Engine, Path],
) -> None:
    controller = make_controller(controller_storage)
    await controller.start()
    game = await create_game(controller)

    player = await controller.submit_request(
        JoinGameRequest(
            game.game_session_id,
            "player-token",
            "connection-1",
            "Player",
        )
    )
    selected = await controller.submit_request(
        SelectPlaylistRequest(
            game.game_session_id,
            "songmaster-token",
            "playlist-id",
        )
    )
    deselected = await controller.submit_request(
        DeselectPlaylistRequest(
            game.game_session_id,
            "songmaster-token",
            "playlist-id",
        )
    )
    preference = await controller.submit_request(
        SetAudioPreferenceRequest(game.game_session_id, "player-token", True)
    )
    controller.post_system_event(
        BrowserAudioCapabilityChangedEvent(
            game.game_session_id,
            "player-token",
            "connection-1",
            True,
        )
    )
    controller.post_system_event(
        MemberDisconnectedEvent(
            game.game_session_id,
            "player-token",
            "connection-1",
        )
    )
    reconnected = await controller.submit_request(
        JoinGameRequest(game.game_session_id, "player-token", "connection-2")
    )

    assert player is reconnected
    assert selected is deselected is game
    assert game.selected_playlist_ids == set()
    assert preference.audio_enabled is True
    assert player.connected is True
    assert player.browser_audio_usable is False
    assert controller.system_event_rejections == ()
    await controller.stop()


@pytest.mark.asyncio
async def test_persistent_start_audio_capability_and_play_song_through_controller(
    controller_storage: tuple[Engine, Path],
) -> None:
    database_engine, music_root = controller_storage
    playlist_id = create_persistent_playlist(database_engine, music_root)
    controller = make_controller(controller_storage)
    await controller.start()
    game = await create_game(controller)
    await controller.submit_request(
        SelectPlaylistRequest(
            game.game_session_id,
            "songmaster-token",
            playlist_id,
        )
    )

    start_result = await controller.submit_request(
        StartGameRequest(game.game_session_id, "songmaster-token")
    )
    controller.post_system_event(
        BrowserAudioCapabilityChangedEvent(
            game.game_session_id,
            "songmaster-token",
            "songmaster-connection",
            True,
        )
    )
    # Rejected Play Song is delivered to its requester and does not stop the
    # consumer; it also acts as a FIFO barrier behind the capability event.
    with pytest.raises(UnauthorizedPlaySongError):
        await controller.submit_request(
            PlaySongRequest(game.game_session_id, "not-songmaster")
        )
    pending = await controller.submit_request(
        PlaySongRequest(game.game_session_id, "songmaster-token")
    )

    assert start_result.game is game
    assert start_result.resolution.exclusions == ()
    assert game.original_deck_size == 1
    assert pending is game.pending_audio_preparation
    assert pending.audio_target_browser_tokens == frozenset({"songmaster-token"})
    assert controller.game_engine.state is GameState.AUDIO_PREPARING
    assert controller.is_running is True
    await controller.stop()


@pytest.mark.asyncio
async def test_exhausted_start_waits_for_play_song_then_finalizes_without_timer(
    controller_storage: tuple[Engine, Path],
) -> None:
    database_engine, music_root = controller_storage
    playlist_id = create_persistent_playlist(database_engine, music_root)

    def short_track_inspector(
        _: str | Path,
        relative_path: str | Path,
    ) -> TrackMetadata:
        return TrackMetadata(
            relative_path=Path(relative_path),
            artist="Artist",
            title="Short Song",
            duration_seconds=10.0,
            file_size_bytes=10_000,
            metadata_source=MetadataSource.FILENAME,
            metadata_status=MetadataStatus.COMPLETE,
            media_status=MediaStatus.VALID,
        )

    controller = GameController(
        database_engine,
        music_root,
        game_engine=make_game_engine(),
        inspect_media=short_track_inspector,
    )
    await controller.start()
    game = await create_game(controller)
    await controller.submit_request(
        SelectPlaylistRequest(
            game.game_session_id,
            "songmaster-token",
            playlist_id,
        )
    )

    result = await controller.submit_request(
        StartGameRequest(game.game_session_id, "songmaster-token")
    )

    assert result.game is game
    assert controller.game_engine.state is GameState.ROUND_COMPLETE
    assert game.deck_exhausted is True
    assert game.deck_pointer == 1
    assert game.ended_at is None
    assert game.final_standings == ()

    finalization = await controller.submit_request(
        PlaySongRequest(game.game_session_id, "songmaster-token")
    )

    assert isinstance(finalization, GameFinalizationResult)
    assert finalization.end_reason is GameEndReason.DECK_EXHAUSTED
    assert controller.game_engine.state is GameState.FINISHED
    assert game.ended_at == ENDED_AT
    assert game.pending_audio_preparation is None
    assert controller.preparation_timeout_active is False
    assert controller.is_running is True
    await controller.stop()
