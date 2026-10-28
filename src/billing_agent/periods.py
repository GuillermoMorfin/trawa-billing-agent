"""Billing periods. A month runs from local midnight on the 1st to local midnight on the next 1st
in Europe/Berlin, so it has 92 or 100 quarter-hours on the days the clocks change."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")
QUARTER_HOUR = timedelta(minutes=15)


@dataclass(frozen=True)
class Period:
    month: str  # "2026-09"
    start_utc: datetime  # naive UTC, inclusive
    end_utc: datetime  # naive UTC, exclusive

    @property
    def expected_intervals(self) -> int:
        return int((self.end_utc - self.start_utc) / QUARTER_HOUR)

    @property
    def first_day(self) -> date:
        return date.fromisoformat(self.month + "-01")

    @property
    def last_day(self) -> date:
        return (self.end_utc.replace(tzinfo=UTC).astimezone(BERLIN) - timedelta(days=1)).date()


def _local_midnight_utc(d: date) -> datetime:
    local = datetime(d.year, d.month, d.day, tzinfo=BERLIN)
    return local.astimezone(UTC).replace(tzinfo=None)


def billing_period(month: str) -> Period:
    first = date.fromisoformat(month + "-01")
    nxt = date(first.year + first.month // 12, first.month % 12 + 1, 1)
    return Period(month, _local_midnight_utc(first), _local_midnight_utc(nxt))


def previous_month(month: str) -> str:
    first = date.fromisoformat(month + "-01")
    prev = first - timedelta(days=1)
    return f"{prev.year:04d}-{prev.month:02d}"
