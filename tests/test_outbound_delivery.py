import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from starlette.websockets import WebSocketState

from who_sings_that.application import (
    AuthoritativeProjectionUpdate,
    CapturedRecipientProjection,
    CreateGameRequest,
    CurrentConnectionRequest,
    GameController,
    GameEventNotification,
    RecipientProjection,
    SetAudioPreferenceRequest,
)
from who_sings_that.database import create_sqlite_engine, upgrade_database
from who_sings_that.web.transport import (
    TransportSession,
    WebSocketTransport,
    _OutboundMailbox,
)


def _recipient() -> CapturedRecipientProjection:
    return CapturedRecipientProjection(
        browser_token="private-browser-token",
        connection_id="connection",
        recipient=RecipientProjection(
            public_participant_id=uuid4(),
            is_songmaster=False,
            eligible_to_buzz=False,
            has_authoritative_connection=True,
            has_control_privileges=False,
            audio_enabled=False,
            browser_audio_usable=False,
            can_act=True,
            available_songmaster_operations=(),
        ),
    )


def _update(revision: int) -> AuthoritativeProjectionUpdate:
    game_id = GAME_ID
    recipient = RECIPIENT
    return AuthoritativeProjectionUpdate(
        game_session_id=game_id,
        revision=revision,
        captured_at=datetime(2026, 11, 11, tzinfo=UTC),
        public=None,
        recipients=(recipient,),
        notifications=(GameEventNotification("test_event", {}),),
        invalidated=True,
    )


GAME_ID = uuid4()
RECIPIENT = _recipient()


def test_state_backlog_coalesces_to_latest_revision() -> None:
    mailbox = _OutboundMailbox()

    for revision in range(1, 101):
        mailbox.put_state("state_update", _update(revision), RECIPIENT)

    assert mailbox.backlog_size == 1


def test_transient_event_backlog_is_bounded_and_reports_overflow() -> None:
    mailbox = _OutboundMailbox(max_events=3)
    update = _update(1)

    assert mailbox.put_event(update, {"event": "one"}) is True
    assert mailbox.put_event(update, {"event": "two"}) is True
    assert mailbox.put_event(update, {"event": "three"}) is True
    assert mailbox.put_event(update, {"event": "overflow"}) is False
    assert mailbox.backlog_size == 3


@pytest.mark.asyncio
async def test_initial_snapshot_is_sent_before_a_coalesced_later_update() -> None:
    mailbox = _OutboundMailbox()
    mailbox.put_state("state_snapshot", _update(1), RECIPIENT)
    mailbox.put_state("state_update", _update(2), RECIPIENT)
    mailbox.put_state("state_update", _update(3), RECIPIENT)

    initial = await mailbox.get()
    latest = await mailbox.get()

    assert initial is not None and initial.message["type"] == "state_snapshot"
    assert initial.message["payload"]["revision"] == 1
    assert latest is not None and latest.message["type"] == "state_update"
    assert latest.message["payload"]["revision"] == 3


@pytest.mark.asyncio
async def test_stalled_socket_send_has_a_finite_deadline(monkeypatch) -> None:
    class BlockingWebSocket:
        async def send_json(self, _message) -> None:
            await asyncio.Event().wait()

    monkeypatch.setattr(
        "who_sings_that.web.transport._SEND_TIMEOUT_SECONDS",
        0.01,
    )
    session = TransportSession(
        connection_id="blocked",
        websocket=BlockingWebSocket(),  # type: ignore[arg-type]
    )

    with pytest.raises(TimeoutError):
        await WebSocketTransport._send_session_json(session, {"type": "test"})


@pytest.mark.asyncio
async def test_blocked_socket_sender_does_not_stall_authoritative_controller(
    tmp_path,
) -> None:
    class BlockingWebSocket:
        def __init__(self) -> None:
            self.application_state = WebSocketState.CONNECTED
            self.send_started = asyncio.Event()

        async def send_json(self, _message) -> None:
            self.send_started.set()
            await asyncio.Event().wait()

        async def close(self, *, code: int) -> None:
            self.application_state = WebSocketState.DISCONNECTED

    database_path = tmp_path / "delivery.sqlite3"
    upgrade_database(database_path)
    database_engine = create_sqlite_engine(database_path)
    music_root = tmp_path / "music"
    music_root.mkdir()
    controller = GameController(database_engine, music_root)
    transport = WebSocketTransport(controller, 4096)
    websocket = BlockingWebSocket()
    session = TransportSession(
        connection_id="connection",
        websocket=websocket,  # type: ignore[arg-type]
        browser_token="browser-token",
    )
    await controller.start()
    assert await transport.connections.add(session)
    synchronized = await controller.submit_membership_request(
        CreateGameRequest("browser-token", "Player", "connection")
    )
    await transport.connections.bind_membership(
        session,
        synchronized.update.game_session_id,
        lambda: controller.projection_for_game_member(
            synchronized.update.game_session_id,
            "browser-token",
        ),
    )
    session.sender_task = asyncio.create_task(transport._deliver_outbound(session))
    await websocket.send_started.wait()

    changed = await asyncio.wait_for(
        controller.submit_projected_request(
            CurrentConnectionRequest(
                SetAudioPreferenceRequest(
                    synchronized.update.game_session_id,
                    "browser-token",
                    False,
                ),
                "connection",
            ),
            "browser-token",
        ),
        timeout=0.5,
    )

    assert changed.recipient.recipient.audio_enabled is False
    await transport.shutdown()
    await controller.stop()
    database_engine.dispose()
