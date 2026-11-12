"""Pure scoring arithmetic for Who Sings That? V1."""

from decimal import Decimal, InvalidOperation
from enum import Enum


class ScoringChoice(Enum):
    """The Songmaster's possible adjudications for a buzzed answer."""

    ARTIST = "artist"
    TITLE = "title"
    BOTH = "both"
    NEITHER = "neither"


BASE_SCORES = {
    ScoringChoice.ARTIST: 20,
    ScoringChoice.TITLE: 20,
    ScoringChoice.BOTH: 60,
    ScoringChoice.NEITHER: 0,
}

_MIN_MULTIPLIER = Decimal("1.0")
_MAX_MULTIPLIER = Decimal("3.0")


def _as_decimal(value: float | Decimal, *, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise TypeError(f"{name} must be a number")

    try:
        converted = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f"{name} must be finite") from error

    if not converted.is_finite():
        raise ValueError(f"{name} must be finite")
    return converted


def _validated_inputs(
    duration: float | Decimal,
    plateau_fraction: float | Decimal,
    elapsed_time: float | Decimal,
) -> tuple[Decimal, Decimal, Decimal]:
    duration_decimal = _as_decimal(duration, name="duration")
    plateau_decimal = _as_decimal(plateau_fraction, name="plateau_fraction")
    elapsed_decimal = _as_decimal(elapsed_time, name="elapsed_time")

    if duration_decimal <= 0:
        raise ValueError("duration must be greater than zero")
    if not Decimal(0) <= plateau_decimal <= Decimal(1):
        raise ValueError("plateau_fraction must be between 0 and 1 inclusive")
    if not Decimal(0) <= elapsed_decimal <= duration_decimal:
        raise ValueError("elapsed_time must be between 0 and duration inclusive")

    return duration_decimal, plateau_decimal, elapsed_decimal


def _speed_multiplier_decimal(
    duration: float | Decimal,
    plateau_fraction: float | Decimal,
    elapsed_time: float | Decimal,
) -> Decimal:
    duration_decimal, plateau_decimal, elapsed_decimal = _validated_inputs(
        duration, plateau_fraction, elapsed_time
    )
    plateau_end = plateau_decimal * duration_decimal

    if elapsed_decimal <= plateau_end or plateau_decimal == 1:
        multiplier = _MAX_MULTIPLIER
    else:
        multiplier = _MAX_MULTIPLIER - Decimal(2) * (elapsed_decimal - plateau_end) / (
            duration_decimal - plateau_end
        )

    return min(_MAX_MULTIPLIER, max(_MIN_MULTIPLIER, multiplier))


def speed_multiplier(
    duration: float | Decimal,
    plateau_fraction: float | Decimal,
    elapsed_time: float | Decimal,
) -> float:
    """Return the V1 speed multiplier for an authoritative buzz time."""

    return float(_speed_multiplier_decimal(duration, plateau_fraction, elapsed_time))


def exact_speed_multiplier(
    duration: float | Decimal,
    plateau_fraction: float | Decimal,
    elapsed_time: float | Decimal,
) -> Decimal:
    """Return the exact Decimal multiplier used by authoritative scoring."""

    return _speed_multiplier_decimal(duration, plateau_fraction, elapsed_time)


def calculate_points(
    choice: ScoringChoice,
    duration: float | Decimal,
    plateau_fraction: float | Decimal,
    elapsed_time: float | Decimal,
) -> int:
    """Return awarded points, truncated after applying the speed multiplier."""

    if not isinstance(choice, ScoringChoice):
        raise TypeError("choice must be a ScoringChoice")

    multiplier = _speed_multiplier_decimal(duration, plateau_fraction, elapsed_time)
    return int(Decimal(BASE_SCORES[choice]) * multiplier)
