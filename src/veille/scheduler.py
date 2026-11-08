"""Nightly scheduler loop used by ``veille worker``."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from veille.contracts import Clock

logger = logging.getLogger(__name__)


def next_run_at(now: datetime, run_at: time, tz: ZoneInfo) -> datetime:
    """Next UTC instant at which local wall-clock time equals ``run_at`` (strictly after now)."""
    local_now = now.astimezone(tz)
    candidate = datetime.combine(local_now.date(), run_at, tzinfo=tz)
    # Compare in UTC: two datetimes sharing a tzinfo are compared on wall-clock time,
    # which is wrong during the DST fall-back hour (02:15 CET is after 02:30 CEST).
    if candidate.astimezone(UTC) <= now.astimezone(UTC):
        candidate = datetime.combine(local_now.date() + timedelta(days=1), run_at, tzinfo=tz)
    return candidate.astimezone(UTC)


def today_run_at(now: datetime, run_at: time, tz: ZoneInfo) -> datetime:
    """UTC instant of today's (local calendar day) ``run_at``."""
    local_now = now.astimezone(tz)
    return datetime.combine(local_now.date(), run_at, tzinfo=tz).astimezone(UTC)


def needs_catch_up(
    now: datetime, run_at: time, tz: ZoneInfo, last_run_started: datetime | None
) -> bool:
    """True when today's ``run_at`` has passed and no run started since then (the worker
    was down at that time). Cheap: themes that already have today's article are skipped."""
    due = today_run_at(now, run_at, tz)
    if now.astimezone(UTC) < due:
        return False
    return last_run_started is None or last_run_started.astimezone(UTC) < due


def run_forever(
    job: Callable[[], object],
    *,
    clock: Clock,
    run_at: time,
    tz: ZoneInfo,
    stop: threading.Event,
    max_sleep_s: float = 60.0,
    catch_up: bool = False,
) -> None:
    """Call ``job`` every day at ``run_at`` (local time) until ``stop`` is set.

    With ``catch_up`` the job also runs once right away (see :func:`needs_catch_up`).
    Sleeps in short chunks so clock changes (suspend, DST) are picked up. A failing job
    is logged and never stops the loop (a BaseException such as KeyboardInterrupt does).
    """
    due = clock.now() if catch_up else next_run_at(clock.now(), run_at, tz)
    if catch_up:
        logger.info("today's run was missed, catching up now")
    else:
        logger.info("scheduler started, next run at %s", due.astimezone(tz).isoformat())
    while not stop.is_set():
        remaining = (due - clock.now()).total_seconds()
        if remaining > 0:
            stop.wait(min(remaining, max_sleep_s))
            continue
        try:
            job()
        except Exception:
            logger.exception("scheduled run failed")
        due = next_run_at(clock.now(), run_at, tz)
        logger.info("next run at %s", due.astimezone(tz).isoformat())
