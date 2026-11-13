from pathlib import Path

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
    sound_manager = (static / "notification-sounds.js").read_text(encoding="utf-8")
    assert "Connect to game" in production
    assert "Connect to the game server" in production
    assert "Create a game or join the Songmaster when a game is active." in production
    assert "Ready when you are" not in production
    assert "Turn the room into a game" not in production
    assert "browser identity stays private" not in production
    assert "Who Sings That?" in production
    assert "uuid" not in production.lower()
    assert "overflow-x: hidden" in stylesheet
    for color in ("#f3b84b", "#22c55e", "#facc15", "#ef4444"):
        assert color in stylesheet.lower()
    assert "class ProductionApp" in production_javascript
    assert "audioReady" in sound_manager
    for name in (
        "connected.wav",
        "game-created.wav",
        "welcome.wav",
        "audio-ready.wav",
    ):
        assert (static / "sounds" / name).read_bytes().startswith(b"RIFF")


def test_nested_sound_assets_are_declared_as_package_data() -> None:
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    assert '"who_sings_that.web" = ["static/*", "static/sounds/*"]' in pyproject
