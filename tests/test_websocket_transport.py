import asyncio
import json
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import httpx
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

_UNSOLICITED: dict[int, list[dict]] = {}


@dataclass(slots=True)
class RunningServer:
    app: object
    server: uvicorn.Server
    task: asyncio.Task[None]
    websocket_url: str
    http_url: str
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
            http_url=f"http://127.0.0.1:{port}",
            controllers=controllers,
        )
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)
        assert all(not controller.is_running for controller in controllers)


async def _connect(server: RunningServer) -> tuple[ClientConnection, dict]:
    websocket = await websockets.connect(server.websocket_url)
    _UNSOLICITED[id(websocket)] = []
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
    while True:
        message = json.loads(await websocket.recv())
        if (
            message.get("type") == "result"
            and message.get("request_id") == correlation_id
        ):
            return message
        _UNSOLICITED[id(websocket)].append(message)


async def _next_unsolicited(
    websocket: ClientConnection,
    *,
    message_type: str | None = None,
    timeout: float = 2.0,
) -> dict:
    queued = _UNSOLICITED[id(websocket)]
    while True:
        for index, message in enumerate(queued):
            if message_type is None or message.get("type") == message_type:
                return queued.pop(index)
        message = json.loads(await asyncio.wait_for(websocket.recv(), timeout))
        if message_type is None or message.get("type") == message_type:
            return message
        queued.append(message)


async def _next_state_after(
    websocket: ClientConnection,
    revision: int = 0,
    *,
    state: str | None = None,
    timeout: float = 2.0,
) -> dict:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise AssertionError("authoritative state update did not arrive")
        message = await _next_unsolicited(websocket, timeout=remaining)
        if message.get("type") not in {"state_snapshot", "state_update"}:
            continue
        payload = message["payload"]
        if payload["revision"] <= revision:
            continue
        if state is not None and payload.get("state") != state:
            continue
        return message


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
    (
        songmaster,
        contestant,
        songmaster_token,
        contestant_token,
        _,
    ) = await _create_two_player_game(running_server)
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
async def test_authorized_media_range_and_audio_control_lifecycle(
    running_server: RunningServer,
) -> None:
    media_path = (
        running_server.app.state.settings.music_root
        / "private/Secret Artist - Secret Title.mp3"
    )
    media_path.parent.mkdir()
    media_bytes = bytes(range(256)) * 40
    media_path.write_bytes(media_bytes)
    songmaster, contestant, _, _, _ = await _create_two_player_game(running_server)
    try:
        playlist_id = _add_secret_playlist(running_server)
        assert (
            await _command(
                songmaster,
                "select_playlist",
                {"playlist_id": playlist_id},
            )
        )["ok"]
        assert (
            await _command(
                songmaster,
                "set_audio_capability",
                {"browser_audio_usable": True},
            )
        )["ok"]
        assert (await _command(songmaster, "start_game"))["ok"]
        played = await _command(songmaster, "play_song")
        preparation_id = played["result"]["preparation_id"]
        prepare = await _next_unsolicited(
            songmaster,
            message_type="prepare_audio",
        )
        payload = prepare["payload"]
        assert payload["preparation_id"] == preparation_id
        assert payload["effective_playback_offset_seconds"] == "30"
        assert payload["playback_duration_seconds"] == 30
        assert payload["preparation_timeout_seconds"] == 5.0
        assert payload["media_url"].startswith("/media/")
        serialized = json.dumps(prepare)
        assert "Secret Artist" not in serialized
        assert "Secret Title" not in serialized
        assert "private/" not in serialized
        assert not any(
            message.get("type") == "prepare_audio"
            for message in _UNSOLICITED[id(contestant)]
        )

        async with httpx.AsyncClient(base_url=running_server.http_url) as client:
            full = await client.get(payload["media_url"])
            assert full.status_code == 200
            assert full.content == media_bytes
            assert full.headers["content-type"] == "audio/mpeg"
            assert full.headers["content-length"] == str(len(media_bytes))
            assert full.headers["accept-ranges"] == "bytes"
            assert "no-store" in full.headers["cache-control"]

            partial = await client.get(
                payload["media_url"],
                headers={"Range": "bytes=100-199"},
            )
            assert partial.status_code == 206
            assert partial.content == media_bytes[100:200]
            assert partial.headers["content-range"] == (
                f"bytes 100-199/{len(media_bytes)}"
            )
            assert partial.headers["content-length"] == "100"

            multipart = await client.get(
                payload["media_url"],
                headers={"Range": "bytes=0-9,20-29"},
            )
            assert multipart.status_code == 206
            assert multipart.headers["content-type"].startswith(
                "multipart/byteranges; boundary="
            )
            assert b"Content-Range: bytes 0-9/" in multipart.content
            assert b"Content-Range: bytes 20-29/" in multipart.content

            malformed = await client.get(
                payload["media_url"],
                headers={"Range": "not-bytes"},
            )
            assert malformed.status_code == 400
            unsatisfiable = await client.get(
                payload["media_url"],
                headers={"Range": "bytes=999999-"},
            )
            assert unsatisfiable.status_code == 416
            assert unsatisfiable.headers["content-range"] == (
                f"bytes */{len(media_bytes)}"
            )
            unknown = await client.get("/media/not-a-capability")
            assert unknown.status_code == 404

            media_path.unlink()
            missing = await client.get(payload["media_url"])
            assert missing.status_code == 404
            missing_serialized = missing.text + repr(missing.headers)
            assert "Secret Artist" not in missing_serialized
            assert "Secret Title" not in missing_serialized
            assert str(media_path.parent) not in missing_serialized
            media_path.write_bytes(media_bytes)

        assert (
            await _command(
                songmaster,
                "audio_preparation_ready",
                {"preparation_id": preparation_id},
            )
        )["ok"]
        play = await _next_unsolicited(songmaster, message_type="play_audio")
        assert play["payload"]["preparation_id"] == preparation_id
        assert play["payload"]["round_id"]
        assert "media_url" not in play["payload"]
        round_id = play["payload"]["round_id"]
        assert (await _command(songmaster, "buzz", {"round_id": round_id}))["ok"]
        stop = await _next_unsolicited(songmaster, message_type="stop_audio")
        assert stop["payload"]["round_id"] == round_id
        assert stop["payload"]["reason"] == "accepted_buzz"

        async with httpx.AsyncClient(base_url=running_server.http_url) as client:
            revoked = await client.get(payload["media_url"])
            assert revoked.status_code == 404
    finally:
        await songmaster.close()
        await contestant.close()


@pytest.mark.asyncio
async def test_connection_replacement_revokes_preparation_media(
    running_server: RunningServer,
) -> None:
    (
        songmaster,
        contestant,
        songmaster_token,
        _,
        game_id,
    ) = await _create_two_player_game(running_server)
    replacement: ClientConnection | None = None
    try:
        playlist_id = _add_secret_playlist(running_server)
        await _command(
            songmaster,
            "select_playlist",
            {"playlist_id": playlist_id},
        )
        await _command(
            songmaster,
            "set_audio_capability",
            {"browser_audio_usable": True},
        )
        await _command(songmaster, "start_game")
        await _command(songmaster, "play_song")
        prepare = await _next_unsolicited(
            songmaster,
            message_type="prepare_audio",
        )

        replacement, _ = await _connect(running_server)
        await _identity(replacement, songmaster_token)
        reconnected = await _command(
            replacement,
            "reconnect",
            {"game_session_id": game_id},
        )
        assert reconnected["ok"]
        async with httpx.AsyncClient(base_url=running_server.http_url) as client:
            response = await client.get(prepare["payload"]["media_url"])
        assert response.status_code == 404
        controller = running_server.app.state.game_controller
        assert controller.game_engine.state is GameState.ROUND_COMPLETE
        assert controller.media_capabilities.active_count == 0
    finally:
        await songmaster.close()
        await contestant.close()
        if replacement is not None:
            await replacement.close()


@pytest.mark.asyncio
async def test_contestant_disconnect_reaches_controller_and_voids_playback(
    running_server: RunningServer,
) -> None:
    songmaster, contestant, _, contestant_token, _ = await _create_two_player_game(
        running_server
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
        stop = await _next_unsolicited(songmaster, message_type="stop_audio")
        assert stop["payload"]["reason"] == "playback_client_loss"
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
    songmaster, contestant, _, contestant_token, _ = await _create_two_player_game(
        running_server
    )
    try:
        contestant.transport.abort()
        controller = running_server.app.state.game_controller
        await _eventually(
            lambda: (
                not controller.game_engine.active_game.members[
                    contestant_token
                ].connected
            )
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
        running_server.app.state.game_controller.submit_membership_request = explode
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


@pytest.mark.asyncio
async def test_initial_snapshots_and_join_broadcast_use_safe_public_ids(
    running_server: RunningServer,
) -> None:
    songmaster, _ = await _connect(running_server)
    contestant, _ = await _connect(running_server)
    songmaster_token = await _identity(songmaster)
    contestant_token = await _identity(contestant)
    try:
        created = await _command(
            songmaster,
            "create_game",
            {"display_name": "Songmaster"},
        )
        creator_state = await _next_state_after(songmaster)
        creator_payload = creator_state["payload"]
        assert creator_state["type"] == "state_snapshot"
        assert creator_payload["state"] == "lobby"
        assert creator_payload["recipient"]["is_songmaster"] is True
        creator_public_id = creator_payload["recipient"]["public_participant_id"]
        assert creator_public_id == created["result"]["public_participant_id"]

        await _command(
            contestant,
            "join_game",
            {
                "game_session_id": created["result"]["game_session_id"],
                "display_name": "Contestant",
            },
        )
        joined_state = await _next_state_after(contestant)
        creator_update = await _next_state_after(
            songmaster,
            creator_payload["revision"],
        )
        joined_payload = joined_state["payload"]
        assert creator_update["payload"]["revision"] == joined_payload["revision"]
        participant_ids = {
            item["public_participant_id"] for item in joined_payload["participants"]
        }
        assert len(participant_ids) == 2
        assert creator_public_id in participant_ids
        serialized = json.dumps((joined_payload, creator_update["payload"]))
        assert songmaster_token not in serialized
        assert contestant_token not in serialized

        impersonation, _ = await _connect(running_server)
        try:
            await _identity(impersonation, creator_public_id)
            rejected = await _command(
                impersonation,
                "reconnect",
                {"game_session_id": created["result"]["game_session_id"]},
            )
            assert rejected["error"]["code"] == "invalid_membership"
        finally:
            await impersonation.close()
    finally:
        await songmaster.close()
        await contestant.close()


@pytest.mark.asyncio
async def test_public_id_songmaster_transfer_updates_every_recipient(
    running_server: RunningServer,
) -> None:
    songmaster, contestant, _, _, _ = await _create_two_player_game(running_server)
    try:
        songmaster_initial = await _next_state_after(songmaster)
        contestant_initial = await _next_state_after(contestant)
        if (
            songmaster_initial["payload"]["revision"]
            < contestant_initial["payload"]["revision"]
        ):
            songmaster_initial = await _next_state_after(
                songmaster,
                songmaster_initial["payload"]["revision"],
            )
        contestant_id = contestant_initial["payload"]["recipient"][
            "public_participant_id"
        ]
        unauthorized = await _command(
            contestant,
            "transfer_songmaster",
            {"recipient_public_participant_id": contestant_id},
        )
        assert unauthorized["error"]["code"] == "not_songmaster"

        transferred = await _command(
            songmaster,
            "transfer_songmaster",
            {"recipient_public_participant_id": contestant_id},
        )
        assert transferred["ok"] is True
        songmaster_update = await _next_state_after(
            songmaster,
            songmaster_initial["payload"]["revision"],
        )
        contestant_update = await _next_state_after(
            contestant,
            contestant_initial["payload"]["revision"],
        )
        assert songmaster_update["payload"]["recipient"]["is_songmaster"] is False
        assert contestant_update["payload"]["recipient"]["is_songmaster"] is True
        assert (
            contestant_update["payload"]["songmaster_public_participant_id"]
            == contestant_id
        )
    finally:
        await songmaster.close()
        await contestant.close()


@pytest.mark.asyncio
async def test_pre_reveal_payloads_are_secret_and_provisional_score_replaces(
    running_server: RunningServer,
) -> None:
    songmaster, contestant, _, _, _ = await _create_two_player_game(running_server)
    try:
        await _next_state_after(songmaster)
        await _next_state_after(contestant)
        round_id, _ = await _start_playing_round(running_server, songmaster)
        songmaster_playing = await _next_state_after(songmaster, state="playing")
        contestant_playing = await _next_state_after(contestant, state="playing")
        pre_reveal = json.dumps(
            (songmaster_playing["payload"], contestant_playing["payload"])
        )
        assert "Secret Artist" not in pre_reveal
        assert "Secret Title" not in pre_reveal
        assert "Inspected Artist" not in pre_reveal
        assert "Inspected Title" not in pre_reveal
        assert "private/" not in pre_reveal
        assert ".mp3" not in pre_reveal

        assert (await _command(contestant, "buzz", {"round_id": round_id}))["ok"]
        await _next_state_after(songmaster, state="awaiting_reveal")
        assert (await _command(songmaster, "reveal", {"round_id": round_id}))["ok"]
        scoring = await _next_state_after(songmaster, state="scoring")
        contestant_scoring = await _next_state_after(contestant, state="scoring")
        assert scoring["payload"]["current_round"]["artist"] == "Secret Artist"
        assert scoring["payload"]["current_round"]["title"] == "Secret Title"

        unauthorized = await _command(
            contestant,
            "select_score",
            {"round_id": round_id, "scoring_choice": "both"},
        )
        assert unauthorized["error"]["code"] == "not_songmaster"

        assert (
            await _command(
                songmaster,
                "select_score",
                {"round_id": round_id, "scoring_choice": "both"},
            )
        )["ok"]
        both = await _next_state_after(
            contestant,
            contestant_scoring["payload"]["revision"],
            state="scoring",
        )
        contestant_id = both["payload"]["recipient"]["public_participant_id"]
        both_row = next(
            item
            for item in both["payload"]["participants"]
            if item["public_participant_id"] == contestant_id
        )
        assert both_row["committed_score"] == 0
        assert both_row["provisional_score"] > 0

        assert (
            await _command(
                songmaster,
                "select_score",
                {"round_id": round_id, "scoring_choice": "artist"},
            )
        )["ok"]
        artist = await _next_state_after(
            contestant,
            both["payload"]["revision"],
            state="scoring",
        )
        artist_row = next(
            item
            for item in artist["payload"]["participants"]
            if item["public_participant_id"] == contestant_id
        )
        assert 0 < artist_row["provisional_score"] < both_row["provisional_score"]
        assert artist_row["effective_score"] == artist_row["provisional_score"]

        assert (
            await _command(
                songmaster,
                "accept_score",
                {"round_id": round_id, "scoring_choice": "artist"},
            )
        )["ok"]
        committed = await _next_state_after(
            contestant,
            artist["payload"]["revision"],
            state="round_complete",
        )
        committed_row = next(
            item
            for item in committed["payload"]["participants"]
            if item["public_participant_id"] == contestant_id
        )
        assert committed_row["committed_score"] == artist_row["effective_score"]
        assert committed_row["provisional_score"] == 0

        assert (
            await _command(
                songmaster,
                "correct_score",
                {"round_id": round_id, "scoring_choice": "both"},
            )
        )["ok"]
        corrected = await _next_state_after(
            contestant,
            committed["payload"]["revision"],
            state="round_complete",
        )
        corrected_row = next(
            item
            for item in corrected["payload"]["participants"]
            if item["public_participant_id"] == contestant_id
        )
        assert corrected_row["committed_score"] > committed_row["committed_score"]
    finally:
        await songmaster.close()
        await contestant.close()


@pytest.mark.asyncio
async def test_mid_round_join_and_reconnect_have_recipient_specific_eligibility(
    running_server: RunningServer,
) -> None:
    (
        songmaster,
        contestant,
        _,
        contestant_token,
        game_id,
    ) = await _create_two_player_game(running_server)
    third, _ = await _connect(running_server)
    replacement: ClientConnection | None = None
    try:
        await _next_state_after(songmaster)
        contestant_initial = await _next_state_after(contestant)
        original_public_id = contestant_initial["payload"]["recipient"][
            "public_participant_id"
        ]
        await _start_playing_round(running_server, songmaster)
        await _identity(third)
        assert (
            await _command(
                third,
                "join_game",
                {"game_session_id": game_id, "display_name": "Late Player"},
            )
        )["ok"]
        late_snapshot = await _next_state_after(third, state="playing")
        assert late_snapshot["payload"]["recipient"]["eligible_to_buzz"] is False

        await contestant.close()
        controller = running_server.app.state.game_controller
        await _eventually(
            lambda: (
                not controller.game_engine.active_game.members[
                    contestant_token
                ].connected
            )
        )
        replacement, _ = await _connect(running_server)
        await _identity(replacement, contestant_token)
        assert (
            await _command(
                replacement,
                "reconnect",
                {"game_session_id": game_id},
            )
        )["ok"]
        resumed = await _next_state_after(replacement, state="playing")
        assert resumed["payload"]["recipient"]["eligible_to_buzz"] is True
        assert (
            resumed["payload"]["recipient"]["public_participant_id"]
            == original_public_id
        )
    finally:
        await songmaster.close()
        await contestant.close()
        await third.close()
        if replacement is not None:
            await replacement.close()


@pytest.mark.asyncio
async def test_disconnect_recovery_and_cancel_emit_authoritative_updates(
    running_server: RunningServer,
) -> None:
    (
        songmaster,
        contestant,
        songmaster_token,
        _,
        game_id,
    ) = await _create_two_player_game(running_server)
    replacement: ClientConnection | None = None
    try:
        await _next_state_after(songmaster)
        contestant_initial = await _next_state_after(contestant)
        await songmaster.close()
        recovery = await _next_state_after(
            contestant,
            contestant_initial["payload"]["revision"],
            state="lobby",
        )
        assert recovery["payload"]["suspended_for_songmaster_recovery"] is True
        assert recovery["payload"]["recovery"]["grace_deadline"].endswith("Z")

        replacement, _ = await _connect(running_server)
        await _identity(replacement, songmaster_token)
        assert (
            await _command(
                replacement,
                "reconnect",
                {"game_session_id": game_id},
            )
        )["ok"]
        recovered = await _next_state_after(replacement, state="lobby")
        assert recovered["payload"]["suspended_for_songmaster_recovery"] is False
        assert recovered["payload"]["last_recovery_outcome"]["resolution"] == (
            "original_reconnected"
        )

        assert (await _command(replacement, "cancel_game"))["ok"]
        invalidated = await _next_state_after(
            contestant,
            recovery["payload"]["revision"],
            state="no_game",
        )
        assert invalidated["payload"]["active"] is False
        assert invalidated["payload"]["game_session_id"] == game_id
    finally:
        await songmaster.close()
        await contestant.close()
        if replacement is not None:
            await replacement.close()


@pytest.mark.asyncio
async def test_finished_member_reconnect_receives_final_standings_snapshot(
    running_server: RunningServer,
) -> None:
    (
        songmaster,
        contestant,
        _,
        contestant_token,
        game_id,
    ) = await _create_two_player_game(running_server)
    replacement: ClientConnection | None = None
    try:
        await _next_state_after(songmaster)
        await _next_state_after(contestant)
        playlist_id = _add_secret_playlist(running_server)
        assert (
            await _command(
                songmaster,
                "select_playlist",
                {"playlist_id": playlist_id},
            )
        )["ok"]
        assert (await _command(songmaster, "start_game"))["ok"]
        assert (await _command(songmaster, "end_game"))["ok"]
        finished = await _next_state_after(contestant, state="finished")
        assert len(finished["payload"]["final_standings"]) == 2

        await contestant.close()
        controller = running_server.app.state.game_controller
        await _eventually(
            lambda: (
                not controller.game_engine.active_game.members[
                    contestant_token
                ].connected
            )
        )
        replacement, _ = await _connect(running_server)
        await _identity(replacement, contestant_token)
        assert (
            await _command(
                replacement,
                "reconnect",
                {"game_session_id": game_id},
            )
        )["ok"]
        snapshot = await _next_state_after(replacement, state="finished")
        assert snapshot["type"] == "state_snapshot"
        assert snapshot["payload"]["finished"] is True
        assert (
            snapshot["payload"]["final_standings"]
            == finished["payload"]["final_standings"]
        )
    finally:
        await songmaster.close()
        await contestant.close()
        if replacement is not None:
            await replacement.close()
