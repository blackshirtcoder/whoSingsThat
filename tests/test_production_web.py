import sys
import wave
from array import array
from pathlib import Path

import pytest

from who_sings_that.web import ServerSettings, create_app


def test_production_and_diagnostic_routes_are_registered(tmp_path: Path) -> None:
    music_root = tmp_path / "music"
    music_root.mkdir()
    app = create_app(
        ServerSettings(
            database_path=tmp_path / "production-route.sqlite3",
            music_root=music_root,
        )
    )

    route_paths = {route.path for route in app.routes}
    assert "/" in route_paths
    assert "/audio-test" in route_paths

    static = Path("src/who_sings_that/web/static")
    production = (static / "index.html").read_text(encoding="utf-8")
    stylesheet = (static / "production.css").read_text(encoding="utf-8")
    production_javascript = (static / "production-ui.js").read_text(encoding="utf-8")
    music_output = (static / "music-output.js").read_text(encoding="utf-8")
    sound_manager = (static / "notification-sounds.js").read_text(encoding="utf-8")
    sound_assets = (static / "sound-assets.js").read_text(encoding="utf-8")
    assert "Connect to game" in production
    assert "Connect to the game server" in production
    assert "Create a game or join the Songmaster when a game is active." in production
    assert "Ready when you are" not in production
    assert "Turn the room into a game" not in production
    assert "browser identity stays private" not in production
    assert "Who Sings That?" in production
    assert "uuid" not in production.lower()
    for control in (
        "Play music",
        "Playlists",
        "Game settings",
        "Start game",
        "Cancel game",
        "Transfer Songmaster",
        "I heard it",
        "Play again",
    ):
        assert control in production
    assert production.count("<audio") == 2
    assert 'id="mystery-audio"' in production
    assert 'id="output-check-audio"' in production
    assert 'src="/static/sounds/audio-ready.wav"' not in production
    assert "/media/" not in production
    assert "overflow-x: hidden" in stylesheet
    for color in ("#f3b84b", "#22c55e", "#facc15", "#ef4444"):
        assert color in stylesheet.lower()
    assert "class ProductionApp" in production_javascript
    assert "duration_warning_confirmation_token" in production_javascript
    assert "confirmOutputFromDedicatedCheck" in music_output
    assert "set_audio_capability" in music_output
    assert "AUDIO_READY_SOUND_URL" in music_output
    assert "browser_token" not in production_javascript
    assert "relative_path" not in production_javascript
    assert "NOTIFICATION_SOUND_FILES" in sound_manager
    assert "audioReady" in sound_assets
    assert 'SOUND_ASSET_VERSION = "1"' in sound_assets
    for name in (
        "connected.wav",
        "game-created.wav",
        "welcome.wav",
        "audio-ready.wav",
    ):
        assert name in sound_assets
        assert (static / "sounds" / name).read_bytes().startswith(b"RIFF")


def test_nested_sound_assets_are_declared_as_package_data() -> None:
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    assert '"who_sings_that.web" = ["static/*", "static/sounds/*"]' in pyproject


def test_temporary_diagnostic_wavs_have_distinct_beep_counts() -> None:
    sound_directory = Path("src/who_sings_that/web/static/sounds")
    expected = {
        "connected.wav": (1, 0.28),
        "game-created.wav": (2, 0.63),
        "welcome.wav": (3, 0.98),
        "audio-ready.wav": (4, 1.33),
    }

    for filename, (beep_count, expected_duration) in expected.items():
        with wave.open(str(sound_directory / filename), "rb") as wav:
            assert wav.getnchannels() == 1
            assert wav.getsampwidth() == 2
            assert wav.getframerate() == 48_000
            assert wav.getcomptype() == "NONE"
            frames = array("h", wav.readframes(wav.getnframes()))
            if sys.byteorder != "little":
                frames.byteswap()
            duration = wav.getnframes() / wav.getframerate()

        assert duration == pytest.approx(expected_duration, abs=0.01)
        # SoX may dither digital silence by one least-significant bit.
        assert max(abs(sample) for sample in frames[:1_440]) <= 1
        assert max(abs(sample) for sample in frames[-1_440:]) <= 1
        assert max(abs(sample) for sample in frames) / 32_768 == pytest.approx(
            0.55,
            abs=0.01,
        )
        assert _count_audible_regions(frames, 48_000) == beep_count


def _count_audible_regions(samples: array, sample_rate: int) -> int:
    window_size = sample_rate // 100  # 10 ms envelope windows
    audible_windows = []
    for start in range(0, len(samples), window_size):
        window = samples[start : start + window_size]
        mean_square = sum(sample * sample for sample in window) / max(1, len(window))
        audible_windows.append(mean_square**0.5 > 1_000)
    return sum(
        audible and (index == 0 or not audible_windows[index - 1])
        for index, audible in enumerate(audible_windows)
    )
