from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest

from who_sings_that.domain.engine import (
    ActiveGameExistsError,
    GameEngine,
    InvalidCreateGameRequestError,
)
from who_sings_that.domain.models import (
    GameSettings,
    GameState,
    InvalidPlayerNameError,
    normalize_player_name,
    player_name_uniqueness_key,
)

FIXED_GAME_ID = UUID("12345678-1234-5678-1234-567812345678")
FIXED_CREATED_AT = datetime(2026, 11, 1, 12, 30, tzinfo=UTC)


class EntryRecordingGameEngine(GameEngine):
    def __init__(self) -> None:
        self.entered_states: list[GameState] = []
        super().__init__(
            clock=lambda: FIXED_CREATED_AT,
            game_id_factory=lambda: FIXED_GAME_ID,
        )

    def _enter_state(self, state: GameState) -> None:
        self.entered_states.append(state)
        super()._enter_state(state)


def make_engine(defaults: GameSettings | None = None) -> GameEngine:
    return GameEngine(
        defaults,
        clock=lambda: FIXED_CREATED_AT,
        game_id_factory=lambda: FIXED_GAME_ID,
    )


def test_engine_starts_in_no_game_with_no_active_game() -> None:
    engine = make_engine()

    assert engine.state is GameState.NO_GAME
    assert engine.active_game is None
    engine.validate_invariants()


def test_invariant_validation_does_not_reenter_current_state() -> None:
    engine = EntryRecordingGameEngine()
    assert engine.entered_states == [GameState.NO_GAME]

    engine.validate_invariants()
    assert engine.entered_states == [GameState.NO_GAME]

    engine.create_game("creator-token", "Creator")
    assert engine.entered_states == [GameState.NO_GAME, GameState.LOBBY]

    engine.validate_invariants()
    assert engine.entered_states == [GameState.NO_GAME, GameState.LOBBY]


def test_normal_flow_state_enumeration_is_exact() -> None:
    assert list(GameState) == [
        GameState.NO_GAME,
        GameState.LOBBY,
        GameState.ROUND_COMPLETE,
        GameState.AUDIO_PREPARING,
        GameState.PLAYING,
        GameState.AWAITING_REVEAL,
        GameState.SCORING,
        GameState.FINISHED,
    ]


def test_create_game_enters_lobby_and_establishes_lobby_invariants() -> None:
    engine = make_engine()

    game = engine.create_game("browser-token", "  Ada   Lovelace  ")

    assert engine.state is GameState.LOBBY
    assert engine.active_game is game
    assert game.deck is None
    assert game.active_round is None
    assert game.playlist_selection_editable is True
    engine.validate_invariants()


def test_creator_is_first_member_and_songmaster_with_zero_committed_score() -> None:
    game = make_engine().create_game("creator-token", "Grace")

    assert list(game.members) == ["creator-token"]
    creator = game.members["creator-token"]
    assert creator.browser_token == "creator-token"
    assert creator.display_name == "Grace"
    assert creator.committed_score == 0
    assert game.songmaster is creator


def test_create_game_starts_with_no_selected_playlists() -> None:
    game = make_engine().create_game("creator-token", "Grace")

    assert game.selected_playlist_ids == set()


def test_installation_defaults_are_copied_not_shared() -> None:
    defaults = GameSettings(
        playback_offset_seconds=12,
        playback_duration_seconds=45,
        speed_bonus_plateau_percent=Decimal(15),
    )
    game = make_engine(defaults).create_game("creator-token", "Grace")

    assert game.settings == defaults
    assert game.settings is not defaults

    game.settings.playback_duration_seconds = 20
    assert defaults.playback_duration_seconds == 45


def test_create_game_records_identity_and_creation_timestamp() -> None:
    game = make_engine().create_game("creator-token", "Grace")

    assert game.game_session_id == FIXED_GAME_ID
    assert game.created_at == FIXED_CREATED_AT


def test_default_game_session_ids_are_unique() -> None:
    first_game = GameEngine().create_game("first-token", "First")
    second_game = GameEngine().create_game("second-token", "Second")

    assert first_game.game_session_id != second_game.game_session_id


@pytest.mark.parametrize(
    ("entered", "displayed"),
    [
        ("  Ada   Lovelace  ", "Ada Lovelace"),
        ("Élodie", "Élodie"),
        ("李雷 2", "李雷 2"),
        ("O'Connor-Smith_2.0", "O'Connor-Smith_2.0"),
        ("Ａｄａ", "Ada"),
        ("Ada\t\nLovelace", "Ada Lovelace"),
    ],
)
def test_player_name_validation_and_display_normalization(
    entered: str, displayed: str
) -> None:
    assert normalize_player_name(entered) == displayed


def test_player_name_key_uses_unicode_normalization_and_case_folding() -> None:
    assert player_name_uniqueness_key("  STRASSE ") == player_name_uniqueness_key(
        "Straße"
    )
    assert player_name_uniqueness_key("Jose\N{COMBINING ACUTE ACCENT}") == (
        player_name_uniqueness_key("JOSÉ")
    )


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "player!",
        "player/name",
        "player🙂",
        "a" * 25,
    ],
)
def test_invalid_player_names_are_rejected(name: str) -> None:
    with pytest.raises(InvalidPlayerNameError):
        normalize_player_name(name)


def test_name_length_is_checked_after_whitespace_normalization() -> None:
    assert normalize_player_name(f"{'a' * 12}   {'b' * 11}") == (
        f"{'a' * 12} {'b' * 11}"
    )


def test_create_game_preserves_normalized_display_capitalization_and_name_key() -> None:
    game = make_engine().create_game("creator-token", "  ÉLODIE  ")

    creator = game.songmaster
    assert creator.display_name == "ÉLODIE"
    assert creator.name_uniqueness_key == player_name_uniqueness_key("élodie")


def test_duplicate_create_game_is_rejected_without_corrupting_active_game() -> None:
    engine = make_engine()
    original_game = engine.create_game("first-token", "First")

    with pytest.raises(ActiveGameExistsError, match="active game already exists"):
        engine.create_game("second-token", "Second")

    assert engine.state is GameState.LOBBY
    assert engine.active_game is original_game
    assert list(original_game.members) == ["first-token"]
    engine.validate_invariants()


@pytest.mark.parametrize(
    ("browser_token", "display_name", "error"),
    [
        ("creator-token", "invalid!", InvalidPlayerNameError),
        ("", "Valid Name", InvalidCreateGameRequestError),
    ],
)
def test_invalid_create_is_atomic_in_no_game(
    browser_token: str, display_name: str, error: type[Exception]
) -> None:
    engine = make_engine()

    with pytest.raises(error):
        engine.create_game(browser_token, display_name)

    assert engine.state is GameState.NO_GAME
    assert engine.active_game is None
    engine.validate_invariants()
