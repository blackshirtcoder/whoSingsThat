import asyncio
import json
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
import uvicorn
import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

from who_sings_that.application import GameController
from who_sings_that.catalog import (
    MediaStatus,
    MetadataSource,
    MetadataStatus,
    TrackMetadata,
)
from who_sings_that.database import (
    add_tracks_to_playlist,
    create_playlist,
    import_tracks,
)
from who_sings_that.domain import GameState, RoundVoidReason
from who_sings_that.web import ServerSettings, create_app


@dataclass(slots=True)
class RunningServer:
    app: object
    server: uvicorn.Server
    task: asyncio.Task[None]
    websocket_url: str
    controllers: list[GameController]


def _inspect_synthetic_track(
    _music_root: str | Path,
    relative_path: str | Path,
) -> TrackMetadata:
    return TrackMetadata(
        relative_path=Path(relative_path),
        artist="Inspected Artist",
        title="Inspected Title",
        duration_seconds=120.0,
        file_size_bytes=10_000,
        metadata_source=MetadataSource.FILENAME,
        metadata_status=MetadataStatus.COMPLETE,
        media_status=MediaStatus.VALID,
    )


@pytest_asyncio.fixture
async def running_server(tmp_path: Path) -> AsyncIterator[RunningServer]:
    music_root = tmp_path / "music"
    music_root.mkdir()
    settings = ServerSettings(
        database_path=tmp_path / "wst-test.sqlite3",
        music_root=music_root,
        max_websocket_message_bytes=4096,
    )
    controllers: list[GameController] = []

    def controller_factory(engine, configured):
        controller = GameController(
            engine,
            configured.music_root,
            inspect_media=_inspect_synthetic_track,
        )
        controllers.append(controller)
        return controller

    app = create_app(settings, controller_factory=controller_factory)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_level="warning",
            lifespan="on",
            # Keep the listener cap above the advertised application cap so
            # this suite also exercises the structured Phase A size rejection.
            ws_max_size=settings.max_websocket_message_bytes * 2,
        )
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        await _eventually(lambda: server.started)
        yield RunningServer(
            app=app,
            server=server,
            task=task,
            websocket_url=f"ws://127.0.0.1:{port}/ws",
            controllers=controllers,
        )
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)
        assert all(not controller.is_running for controller in controllers)


async def _connect(server: RunningServer) -> tuple[ClientConnection, dict]:
    websocket = await websockets.connect(server.websocket_url)
    connected = json.loads(await websocket.recv())
    return websocket, connected


async def _command(
    websocket: ClientConnection,
    operation: str,
    payload: dict | None = None,
    *,
    request_id: str | None = None,
    version: int = 1,
) -> dict:
    correlation_id = request_id or uuid4().hex
    await websocket.send(
        json.dumps(
            {
                "version": version,
                "request_id": correlation_id,
                "type": operation,
                "payload": payload or {},
            }
        )
    )
    return json.loads(await websocket.recv())


async def _identity(
    websocket: ClientConnection,
    browser_token: str | None = None,
) -> str:
    payload = {} if browser_token is None else {"browser_token": browser_token}
    response = await _command(websocket, "establish_identity", payload)
    assert response["ok"] is True
    return response["result"]["browser_token"]


async def _eventually(predicate, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("condition did not become true before timeout")
        await asyncio.sleep(0.01)


def _add_secret_playlist(server: RunningServer) -> str:
    engine = server.app.state.database_engine
    (track,) = import_tracks(
        engine,
        [
            TrackMetadata(
                relative_path=Path("private/Secret Artist - Secret Title.mp3"),
                artist="Secret Artist",
                title="Secret Title",
                duration_seconds=120.0,
                file_size_bytes=10_000,
                metadata_source=MetadataSource.FILENAME,
                metadata_status=MetadataStatus.COMPLETE,
                media_status=MediaStatus.VALID,
            )
        ],
    )
    playlist = create_playlist(engine, "Secret Playlist")
    add_tracks_to_playlist(engine, playlist.playlist_id, [track.track_id])
    return playlist.playlist_id


async def _create_two_player_game(
    server: RunningServer,
) -> tuple[ClientConnection, ClientConnection, str, str, str]:
    songmaster, _ = await _connect(server)
    contestant, _ = await _connect(server)
    songmaster_token = await _identity(songmaster)
    contestant_token = await _identity(contestant)
    created = await _command(
        songmaster,
        "create_game",
        {"display_name": "Songmaster"},
    )
    game_id = created["result"]["game_session_id"]
    joined = await _command(
        contestant,
        "join_game",
        {"game_session_id": game_id, "display_name": "Contestant"},
    )
    assert joined["ok"] is True
    return songmaster, contestant, songmaster_token, contestant_token, game_id


async def _start_playing_round(
    server: RunningServer,
    songmaster: ClientConnection,
    *,
    extra_ready: ClientConnection | None = None,
) -> tuple[str, list[dict]]:
    playlist_id = _add_secret_playlist(server)
    responses = [
        await _command(songmaster, "select_playlist", {"playlist_id": playlist_id}),
        await _command(
            songmaster,
            "set_audio_capability",
            {"browser_audio_usable": True},
        ),
        await _command(songmaster, "start_game"),
        await _command(songmaster, "play_song"),
    ]
    preparation_id = responses[-1]["result"]["preparation_id"]
    responses.append(
        await _command(
            songmaster,
            "audio_preparation_ready",
            {"preparation_id": preparation_id},
        )
    )
    if extra_ready is not None:
        responses.append(
            await _command(
                extra_ready,
                "audio_preparation_ready",
                {"preparation_id": preparation_id},
            )
        )
    controller = server.app.state.game_controller
    await _eventually(lambda: controller.game_engine.state is GameState.PLAYING)
    active_round = controller.game_engine.active_game.active_round
    assert active_round is not None
    return str(active_round.round_id), responses


@pytest.mark.asyncio
async def test_lifespan_creates_one_started_controller_for_multiple_connections(
    running_server: RunningServer,
) -> None:
    assert len(running_server.controllers) == 1
    controller = running_server.controllers[0]
    assert controller.is_running

    first, _ = await _connect(running_server)
    second, _ = await _connect(running_server)
    try:
        assert running_server.app.state.game_controller is controller
        assert len(running_server.controllers) == 1
    finally:
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_handshake_identity_and_protocol_rejections_keep_socket_usable(
    running_server: RunningServer,
) -> None:
    websocket, connected = await _connect(running_server)
    try:
        assert connected["type"] == "transport_connected"
        assert connected["result"]["connection_id"]
        assert connected["result"]["max_message_bytes"] == 4096

        unsupported = await _command(
            websocket,
            "establish_identity",
            request_id="unsupported",
            version=2,
        )
        assert unsupported == {
            "version": 1,
            "request_id": "unsupported",
            "type": "result",
            "ok": False,
            "error": {
                "code": "unsupported_protocol_version",
                "message": "Only protocol version 1 is supported.",
            },
        }

        await websocket.send("{")
        malformed = json.loads(await websocket.recv())
        assert malformed["error"]["code"] == "invalid_command"
        token = await _identity(websocket)
        assert len(token) >= 16

        impersonation = await _command(
            websocket,
            "create_game",
            {"display_name": "Alice", "connection_id": "client-chosen"},
            request_id="impersonation",
        )
        assert impersonation["request_id"] == "impersonation"
        assert impersonation["error"]["code"] == "invalid_command"
        created = await _command(
            websocket,
            "create_game",
            {"display_name": "Alice"},
        )
        assert created["ok"] is True
    finally:
        await websocket.close()


@pytest.mark.asyncio
async def test_oversized_application_message_is_rejected_without_losing_socket(
    running_server: RunningServer,
) -> None:
    websocket, _ = await _connect(running_server)
    try:
        await websocket.send("x" * 4097)
        oversized = json.loads(await websocket.recv())
        assert oversized["error"]["code"] == "message_too_large"
        assert await _identity(websocket)
    finally:
        await websocket.close()


@pytest.mark.asyncio
async def test_create_join_duplicate_name_authority_and_request_correlation(
    running_server: RunningServer,
) -> None:
    songmaster, contestant, _, _, game_id = await _create_two_player_game(
        running_server
    )
    third, _ = await _connect(running_server)
    try:
        await _identity(third)
        duplicate = await _command(
            third,
            "join_game",
            {"game_session_id": game_id, "display_name": " contestant "},
            request_id="duplicate-name",
        )
        assert duplicate["request_id"] == "duplicate-name"
        assert duplicate["error"]["code"] == "duplicate_player_name"

        joined = await _command(
            third,
            "join_game",
            {"game_session_id": game_id, "display_name": "Third"},
        )
        assert joined["ok"] is True

        unauthorized = await _command(contestant, "start_game")
        assert unauthorized["error"]["code"] == "not_songmaster"

        unknown = await _command(third, "definitely_not_a_controller_method")
        assert unknown["error"]["code"] == "unknown_operation"
    finally:
        await songmaster.close()
        await contestant.close()
        await third.close()


@pytest.mark.asyncio
async def test_reconnect_rejects_unknown_and_cross_game_tokens(
    running_server: RunningServer,
) -> None:
    songmaster, _, token, _, game_id = await _create_two_player_game(running_server)
    unknown_socket, _ = await _connect(running_server)
    stale_socket, _ = await _connect(running_server)
    try:
        await _identity(unknown_socket, "unknown-browser-token")
        unknown = await _command(
            unknown_socket,
            "reconnect",
            {"game_session_id": game_id},
        )
        assert unknown["error"]["code"] == "invalid_membership"

        await _identity(stale_socket, token)
        stale = await _command(
            stale_socket,
            "reconnect",
            {"game_session_id": str(uuid4())},
        )
        assert stale["error"]["code"] == "wrong_game"
    finally:
        await songmaster.close()
        await unknown_socket.close()
        await stale_socket.close()


@pytest.mark.asyncio
async def test_replacement_socket_becomes_authoritative_and_old_cleanup_is_stale(
    running_server: RunningServer,
) -> None:
    original, _ = await _connect(running_server)
    token = await _identity(original)
    created = await _command(
        original,
        "create_game",
        {"display_name": "Songmaster"},
    )
    game_id = created["result"]["game_session_id"]
    replacement, replacement_hello = await _connect(running_server)
    try:
        await _identity(replacement, token)
        reconnected = await _command(
            replacement,
            "reconnect",
            {"game_session_id": game_id},
        )
        assert reconnected["ok"] is True
        assert reconnected["result"]["display_name"] == "Songmaster"

        with pytest.raises(ConnectionClosed):
            await _command(
                original,
                "set_audio_preference",
                {"audio_enabled": False},
            )

        accepted = await _command(
            replacement,
            "set_audio_preference",
            {"audio_enabled": False},
        )
        assert accepted["ok"] is True
        await asyncio.sleep(0.05)
        controller = running_server.app.state.game_controller
        game = controller.game_engine.active_game
        assert game.members[token].connected
        assert controller.connection_registry.is_current(
            game.game_session_id,
            token,
            replacement_hello["result"]["connection_id"],
        )
    finally:
        await original.close()
        await replacement.close()


@pytest.mark.asyncio
async def test_real_concurrent_buzz_has_one_winner_and_leaks_no_answer(
    running_server: RunningServer,
) -> None:
    songmaster, contestant, songmaster_token, contestant_token, _ = (
        await _create_two_player_game(running_server)
    )
    try:
        round_id, setup_responses = await _start_playing_round(
            running_server,
            songmaster,
        )
        serialized = json.dumps(setup_responses)
        assert "Secret Artist" not in serialized
        assert "Secret Title" not in serialized
        assert "private/" not in serialized

        first, second = await asyncio.gather(
            _command(songmaster, "buzz", {"round_id": round_id}),
            _command(contestant, "buzz", {"round_id": round_id}),
        )
        accepted = [response for response in (first, second) if response["ok"]]
        rejected = [response for response in (first, second) if not response["ok"]]
        assert len(accepted) == 1
        assert len(rejected) == 1
        assert rejected[0]["error"]["code"] == "buzz_not_allowed"

        game = running_server.app.state.game_controller.game_engine.active_game
        expected_winner = songmaster_token if first["ok"] else contestant_token
        assert game.round_termination.winning_browser_token == expected_winner
    finally:
        await songmaster.close()
        await contestant.close()


@pytest.mark.asyncio
async def test_contestant_disconnect_reaches_controller_and_voids_playback(
    running_server: RunningServer,
) -> None:
    songmaster, contestant, _, contestant_token, _ = (
        await _create_two_player_game(running_server)
    )
    try:
        assert (
            await _command(
                contestant,
                "set_audio_preference",
                {"audio_enabled": True},
            )
        )["ok"]
        assert (
            await _command(
                contestant,
                "set_audio_capability",
                {"browser_audio_usable": True},
            )
        )["ok"]
        await _start_playing_round(
            running_server,
            songmaster,
            extra_ready=contestant,
        )
        await contestant.close()
        controller = running_server.app.state.game_controller
        await _eventually(
            lambda: controller.game_engine.state is GameState.ROUND_COMPLETE
        )
        game = controller.game_engine.active_game
        assert not game.members[contestant_token].connected
        assert game.last_voided_round.reason is RoundVoidReason.PLAYBACK_CLIENT_LOST
    finally:
        await songmaster.close()
        await contestant.close()


@pytest.mark.asyncio
async def test_abrupt_contestant_disconnect_reaches_controller(
    running_server: RunningServer,
) -> None:
    songmaster, contestant, _, contestant_token, _ = (
        await _create_two_player_game(running_server)
    )
    try:
        contestant.transport.abort()
        controller = running_server.app.state.game_controller
        await _eventually(
            lambda: not controller.game_engine.active_game.members[
                contestant_token
            ].connected
        )
    finally:
        await songmaster.close()
        await contestant.close()


@pytest.mark.asyncio
async def test_songmaster_disconnect_and_timely_reconnect_restore_authority(
    running_server: RunningServer,
) -> None:
    songmaster, contestant, token, _, game_id = await _create_two_player_game(
        running_server
    )
    reconnected: ClientConnection | None = None
    try:
        await songmaster.close()
        controller = running_server.app.state.game_controller
        await _eventually(
            lambda: controller.game_engine.active_game.songmaster_recovery is not None
        )

        reconnected, _ = await _connect(running_server)
        await _identity(reconnected, token)
        response = await _command(
            reconnected,
            "reconnect",
            {"game_session_id": game_id},
        )
        assert response["ok"] is True
        game = controller.game_engine.active_game
        assert game.songmaster_recovery is None
        assert game.songmaster_browser_token == token
        assert game.songmaster.connected
    finally:
        await songmaster.close()
        await contestant.close()
        if reconnected is not None:
            await reconnected.close()


@pytest.mark.asyncio
async def test_unexpected_failure_is_generic_and_contains_no_traceback(
    running_server: RunningServer,
) -> None:
    websocket, _ = await _connect(running_server)
    try:
        await _identity(websocket)

        async def explode(_request):
            raise RuntimeError("sensitive server detail")

        running_server.app.state.game_controller.submit_request = explode
        response = await _command(
            websocket,
            "create_game",
            {"display_name": "Alice"},
        )
        serialized = json.dumps(response)
        assert response["error"]["code"] == "internal_error"
        assert "sensitive server detail" not in serialized
        assert "Traceback" not in serialized
    finally:
        await websocket.close()
