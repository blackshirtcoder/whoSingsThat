from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from who_sings_that.application import (
    AudioStopReason,
    MediaCapabilityPhase,
    MediaCapabilityStore,
    MediaCapabilityUnavailable,
    PlayAudioCommand,
    PrepareAudioCommand,
    StopAudioCommand,
)
from who_sings_that.web.media import _resolve_protected_media
from who_sings_that.web.transport import _OutboundMailbox


class MutableMonotonicClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


def test_media_capability_is_opaque_scoped_promoted_and_expired() -> None:
    clock = MutableMonotonicClock()
    store = MediaCapabilityStore(10.0, clock)
    game_id = uuid4()
    preparation_id = uuid4()
    round_id = uuid4()
    issued = store.issue(
        game_session_id=game_id,
        preparation_id=preparation_id,
        browser_token="private-browser-token",
        connection_id="trusted-connection",
        track_id=42,
    )

    assert len(issued.opaque_token) >= 32
    assert "42" not in issued.opaque_token
    assert store.authorize(issued.opaque_token) == issued
    store.promote_ready(
        preparation_id,
        frozenset({"private-browser-token"}),
        round_id,
    )
    promoted = store.authorize(issued.opaque_token)
    assert promoted.phase is MediaCapabilityPhase.PLAYING
    assert promoted.round_id == round_id

    clock.value = 110.0
    with pytest.raises(MediaCapabilityUnavailable):
        store.authorize(issued.opaque_token)
    assert store.active_count == 0


def test_media_capability_rejects_non_ready_and_stale_connections() -> None:
    clock = MutableMonotonicClock()
    store = MediaCapabilityStore(60.0, clock)
    game_id = uuid4()
    preparation_id = uuid4()
    ready = store.issue(
        game_session_id=game_id,
        preparation_id=preparation_id,
        browser_token="ready",
        connection_id="ready-connection",
        track_id=1,
    )
    failed = store.issue(
        game_session_id=game_id,
        preparation_id=preparation_id,
        browser_token="failed",
        connection_id="failed-connection",
        track_id=1,
    )
    store.promote_ready(preparation_id, frozenset({"ready"}), uuid4())

    with pytest.raises(MediaCapabilityUnavailable):
        store.authorize(failed.opaque_token)
    store.reconcile_connections(
        lambda _game, _browser, connection: connection != "ready-connection"
    )
    with pytest.raises(MediaCapabilityUnavailable):
        store.authorize(ready.opaque_token)


def test_media_capabilities_do_not_cross_games_or_preparations() -> None:
    clock = MutableMonotonicClock()
    store = MediaCapabilityStore(60.0, clock)
    first_game = uuid4()
    second_game = uuid4()
    first = store.issue(
        game_session_id=first_game,
        preparation_id=uuid4(),
        browser_token="same-browser",
        connection_id="first-connection",
        track_id=1,
    )
    second = store.issue(
        game_session_id=second_game,
        preparation_id=uuid4(),
        browser_token="same-browser",
        connection_id="second-connection",
        track_id=1,
    )
    assert first.opaque_token != second.opaque_token

    store.revoke_game(first_game)
    with pytest.raises(MediaCapabilityUnavailable):
        store.authorize(first.opaque_token)
    assert store.authorize(second.opaque_token) == second


def test_media_path_rejects_traversal_absolute_and_symlink_escape(
    tmp_path: Path,
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    safe = root / "safe.mp3"
    safe.write_bytes(b"safe")
    resolved, status = _resolve_protected_media(root, "safe.mp3")
    assert resolved == safe
    assert status.st_size == 4

    outside = tmp_path / "outside.mp3"
    outside.write_bytes(b"secret")
    (root / "escape.mp3").symlink_to(outside)
    for path in (
        "../outside.mp3",
        str(outside),
        "C:\\Music\\song.mp3",
        "escape.mp3",
    ):
        with pytest.raises((OSError, ValueError)):
            _resolve_protected_media(root, path)


def test_media_path_rejects_missing_directory_and_unreadable_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    with pytest.raises(OSError):
        _resolve_protected_media(root, "missing.mp3")
    directory = root / "directory.mp3"
    directory.mkdir()
    with pytest.raises(OSError):
        _resolve_protected_media(root, "directory.mp3")
    unreadable = root / "unreadable.mp3"
    unreadable.write_bytes(b"audio")
    unreadable.chmod(0)
    try:
        with pytest.raises(OSError):
            _resolve_protected_media(root, "unreadable.mp3")
    finally:
        unreadable.chmod(0o600)


@pytest.mark.asyncio
async def test_audio_control_lane_is_bounded_fifo_and_not_coalesced() -> None:
    mailbox = _OutboundMailbox(max_events=3)
    game_id = uuid4()
    preparation_id = uuid4()
    round_id = uuid4()
    commands = (
        PrepareAudioCommand(
            game_id,
            preparation_id,
            "browser",
            "connection",
            "/media/opaque",
            Decimal(30),
            30,
            5.0,
        ),
        PlayAudioCommand(
            game_id,
            preparation_id,
            round_id,
            "browser",
            "connection",
            Decimal(30),
            30,
            datetime.now(UTC),
        ),
        StopAudioCommand(
            game_id,
            preparation_id,
            round_id,
            "browser",
            "connection",
            AudioStopReason.ACCEPTED_BUZZ,
        ),
    )
    for command in commands:
        assert mailbox.put_control(command)
    assert not mailbox.put_control(commands[-1])
    assert mailbox.backlog_size == 3

    records = [await mailbox.get() for _ in commands]
    assert [record.message["type"] for record in records if record is not None] == [
        "prepare_audio",
        "play_audio",
        "stop_audio",
    ]
