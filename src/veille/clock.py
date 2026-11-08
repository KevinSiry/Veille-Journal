"""Clock implementations."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """Deterministic clock for tests."""

    def __init__(self, at: datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("FixedClock requires an aware datetime")
        self._at = at.astimezone(UTC)

    def now(self) -> datetime:
        return self._at

    def advance(self, delta: timedelta) -> None:
        self._at += delta


def local_day(moment: datetime, tz: ZoneInfo) -> date:
    """Calendar day of ``moment`` in ``tz`` (the article ``day`` key)."""
    return moment.astimezone(tz).date()


def day_start_utc(day: date, tz: ZoneInfo) -> datetime:
    """UTC instant of local midnight starting ``day``."""
    return datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(UTC)
