"""SQLite representations for database values requiring explicit semantics."""

from datetime import UTC, datetime

from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.types import Text, TypeDecorator


class UTCDateTimeText(TypeDecorator[datetime]):
    """Persist aware datetimes as microsecond-precision UTC ISO-8601 text."""

    impl = Text
    cache_ok = True

    def process_bind_param(
        self,
        value: datetime | None,
        dialect: Dialect,
    ) -> str | None:
        if value is None:
            return None
        if value.utcoffset() is None:
            raise ValueError("UTCDateTimeText requires a timezone-aware datetime")
        utc_value = value.astimezone(UTC)
        return utc_value.isoformat(timespec="microseconds").replace("+00:00", "Z")

    def process_result_value(
        self,
        value: str | None,
        dialect: Dialect,
    ) -> datetime | None:
        if value is None:
            return None
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError("stored catalog timestamp does not identify a timezone")
        return parsed.astimezone(UTC)
