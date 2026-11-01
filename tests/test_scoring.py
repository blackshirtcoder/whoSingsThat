from decimal import Decimal

import pytest

from who_sings_that.domain.scoring import (
    BASE_SCORES,
    ScoringChoice,
    calculate_points,
    speed_multiplier,
)


@pytest.mark.parametrize(
    ("choice", "base_score"),
    [
        (ScoringChoice.ARTIST, 20),
        (ScoringChoice.TITLE, 20),
        (ScoringChoice.BOTH, 60),
        (ScoringChoice.NEITHER, 0),
    ],
)
def test_v1_base_scores(choice: ScoringChoice, base_score: int) -> None:
    assert BASE_SCORES[choice] == base_score


def test_multiplier_stays_at_maximum_through_plateau_boundary() -> None:
    assert speed_multiplier(30, Decimal("0.1"), Decimal("2.999")) == 3.0
    assert speed_multiplier(30, Decimal("0.1"), 3) == 3.0


def test_multiplier_decays_linearly_after_plateau() -> None:
    assert speed_multiplier(30, Decimal("0.1"), Decimal("9.75")) == 2.5
    assert speed_multiplier(30, Decimal("0.1"), Decimal("16.5")) == 2.0
    assert speed_multiplier(30, Decimal("0.1"), Decimal("23.25")) == 1.5


def test_multiplier_reaches_minimum_at_duration() -> None:
    assert speed_multiplier(30, Decimal("0.1"), 30) == 1.0


def test_zero_percent_plateau_only_awards_maximum_at_zero() -> None:
    assert speed_multiplier(10, 0, 0) == 3.0
    assert speed_multiplier(10, 0, Decimal("0.001")) < 3.0
    assert speed_multiplier(10, 0, 10) == 1.0


@pytest.mark.parametrize("elapsed_time", [0, 50, 100])
def test_hundred_percent_plateau_is_always_maximum(elapsed_time: int) -> None:
    assert speed_multiplier(100, 1, elapsed_time) == 3.0


@pytest.mark.parametrize(
    ("choice", "expected_points"),
    [
        (ScoringChoice.ARTIST, 60),
        (ScoringChoice.TITLE, 60),
        (ScoringChoice.BOTH, 180),
        (ScoringChoice.NEITHER, 0),
    ],
)
def test_points_at_maximum_multiplier(
    choice: ScoringChoice, expected_points: int
) -> None:
    assert calculate_points(choice, 30, Decimal("0.1"), 3) == expected_points


@pytest.mark.parametrize(
    ("choice", "expected_points"),
    [
        (ScoringChoice.ARTIST, 20),
        (ScoringChoice.TITLE, 20),
        (ScoringChoice.BOTH, 60),
        (ScoringChoice.NEITHER, 0),
    ],
)
def test_points_at_minimum_multiplier(
    choice: ScoringChoice, expected_points: int
) -> None:
    assert calculate_points(choice, 30, Decimal("0.1"), 30) == expected_points


def test_final_points_are_truncated_not_rounded() -> None:
    # With no plateau, t=5.05 of 10 gives 1.99x: 20 * 1.99 = 39.8.
    assert calculate_points(ScoringChoice.ARTIST, 10, 0, Decimal("5.05")) == 39


def test_decimal_arithmetic_is_exact_at_integer_point_boundary() -> None:
    # 20 * 2.95 is exactly 59 and must not become 58 due to float drift.
    assert calculate_points(ScoringChoice.ARTIST, 40, 0, 1) == 59


@pytest.mark.parametrize("duration", [0, -1, Decimal("-0.01")])
def test_non_positive_duration_is_rejected(duration: int | Decimal) -> None:
    with pytest.raises(ValueError, match="duration must be greater than zero"):
        speed_multiplier(duration, 0.1, 0)


@pytest.mark.parametrize("plateau_fraction", [-1, Decimal("-0.01"), 1.01, 2])
def test_out_of_range_plateau_is_rejected(
    plateau_fraction: float | Decimal,
) -> None:
    with pytest.raises(ValueError, match="plateau_fraction must be between 0 and 1"):
        speed_multiplier(30, plateau_fraction, 0)


@pytest.mark.parametrize("elapsed_time", [-1, Decimal("-0.01"), 30.01, 31])
def test_out_of_range_elapsed_time_is_rejected(
    elapsed_time: float | Decimal,
) -> None:
    with pytest.raises(ValueError, match="elapsed_time must be between 0 and duration"):
        speed_multiplier(30, 0.1, elapsed_time)


@pytest.mark.parametrize(
    ("argument", "values"),
    [
        ("duration", (float("nan"), 0.1, 0)),
        ("duration", (float("inf"), 0.1, 0)),
        ("plateau_fraction", (30, float("nan"), 0)),
        ("plateau_fraction", (30, float("inf"), 0)),
        ("elapsed_time", (30, 0.1, float("nan"))),
        ("elapsed_time", (30, 0.1, float("inf"))),
    ],
)
def test_non_finite_arguments_are_rejected(
    argument: str, values: tuple[float | int, float, float | int]
) -> None:
    with pytest.raises(ValueError, match=rf"{argument} must be finite"):
        speed_multiplier(*values)


@pytest.mark.parametrize(
    ("argument", "values"),
    [
        ("duration", (True, 0.1, 0)),
        ("duration", ("30", 0.1, 0)),
        ("plateau_fraction", (30, None, 0)),
        ("elapsed_time", (30, 0.1, False)),
    ],
)
def test_non_numeric_arguments_are_rejected(
    argument: str, values: tuple[object, object, object]
) -> None:
    with pytest.raises(TypeError, match=rf"{argument} must be a number"):
        speed_multiplier(*values)  # type: ignore[arg-type]


def test_invalid_scoring_choice_is_rejected() -> None:
    with pytest.raises(TypeError, match="choice must be a ScoringChoice"):
        calculate_points("artist", 30, 0.1, 0)  # type: ignore[arg-type]
