from __future__ import annotations

import threading
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from veille.clock import FixedClock
from veille.scheduler import needs_catch_up, next_run_at, run_forever

PARIS = ZoneInfo("Europe/Paris")


def test_next_run_same_day_and_next_day() -> None:
    before = datetime(2026, 11, 8, 3, 0, tzinfo=UTC)  # 04:00 Paris
    assert next_run_at(before, time(5, 30), PARIS) == datetime(2026, 11, 8, 4, 30, tzinfo=UTC)
    after = datetime(2026, 11, 8, 5, 0, tzinfo=UTC)
    assert next_run_at(after, time(5, 30), PARIS) == datetime(2026, 11, 9, 4, 30, tzinfo=UTC)


def test_next_run_across_dst_change() -> None:
    # 2027-03-28: Paris switches to UTC+2.
    now = datetime(2027, 3, 27, 12, 0, tzinfo=UTC)
    assert next_run_at(now, time(5, 30), PARIS) == datetime(2027, 3, 28, 3, 30, tzinfo=UTC)


class SteppingClock(FixedClock):
    """Advances by one hour at each read so the loop progresses without real waiting."""

    def now(self) -> datetime:
        value = super().now()
        self.advance(timedelta(hours=1))
        return value


def test_run_forever_runs_job_and_survives_errors() -> None:
    stop = threading.Event()
    calls: list[int] = []

    def job() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        if len(calls) >= 2:
            stop.set()

    clock = SteppingClock(datetime(2026, 11, 8, 0, 0, tzinfo=UTC))
    run_forever(job, clock=clock, run_at=time(5, 30), tz=PARIS, stop=stop, max_sleep_s=0)
    assert len(calls) == 2


def test_next_run_during_dst_fall_back_hour() -> None:
    # 2026-10-25 in Paris: 03:00 CEST -> 02:00 CET, so 02:00-03:00 happens twice.
    run_at = time(2, 30)
    # 02:45 CEST (first pass, already past 02:30 CEST = 00:30 UTC).
    first = datetime(2026, 10, 25, 0, 45, tzinfo=UTC)
    # 02:15 CET (second pass): wall-clock 02:15 < 02:30 but 02:30 CEST is long gone.
    second = datetime(2026, 10, 25, 1, 15, tzinfo=UTC)
    tomorrow = datetime(2026, 10, 26, 1, 30, tzinfo=UTC)  # 02:30 CET
    assert next_run_at(first, run_at, PARIS) == tomorrow
    assert next_run_at(second, run_at, PARIS) == tomorrow
    # Before the first 02:30: today's 02:30 CEST.
    early = datetime(2026, 10, 25, 0, 15, tzinfo=UTC)
    assert next_run_at(early, run_at, PARIS) == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)


def test_needs_catch_up() -> None:
    run_at = time(5, 30)  # 04:30 UTC in November
    due = datetime(2026, 11, 8, 4, 30, tzinfo=UTC)
    later = datetime(2026, 11, 8, 9, 0, tzinfo=UTC)
    assert needs_catch_up(later, run_at, PARIS, None)
    assert needs_catch_up(later, run_at, PARIS, due - timedelta(days=1))  # yesterday's run
    assert not needs_catch_up(later, run_at, PARIS, due + timedelta(minutes=1))  # done today
    assert not needs_catch_up(due - timedelta(minutes=1), run_at, PARIS, None)  # not due yet


def test_run_forever_catch_up_runs_at_startup() -> None:
    stop = threading.Event()
    calls: list[datetime] = []
    start = datetime(2026, 11, 8, 9, 0, tzinfo=UTC)

    def job() -> None:
        calls.append(start)
        stop.set()

    run_forever(
        job,
        clock=FixedClock(start),
        run_at=time(5, 30),
        tz=PARIS,
        stop=stop,
        max_sleep_s=0,
        catch_up=True,
    )
    assert calls == [start]


def test_run_forever_lets_interrupt_through() -> None:
    def job() -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_forever(
            job,
            clock=FixedClock(datetime(2026, 11, 8, 9, 0, tzinfo=UTC)),
            run_at=time(5, 30),
            tz=PARIS,
            stop=threading.Event(),
            catch_up=True,
        )
