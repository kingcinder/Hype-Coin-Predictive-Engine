from __future__ import annotations

from datetime import UTC, datetime, timedelta


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def floor_to_hour(value: datetime) -> datetime:
    value = ensure_utc(value)
    return value.replace(minute=0, second=0, microsecond=0)


def parse_ms_timestamp(value: int | float | None) -> datetime | None:
    if value is None:
        return None
    return datetime.fromtimestamp(float(value) / 1000.0, tz=UTC)


def hours_between(start: datetime, end: datetime) -> list[datetime]:
    current = floor_to_hour(start)
    end = floor_to_hour(end)
    out: list[datetime] = []
    while current <= end:
        out.append(current)
        current += timedelta(hours=1)
    return out
