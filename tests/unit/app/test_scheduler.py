from __future__ import annotations

import threading
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from veille.clock import FixedClock
from veille.scheduler import next_run_at, run_forever

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
