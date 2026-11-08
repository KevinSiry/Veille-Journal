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
    if candidate <= local_now:
        candidate = datetime.combine(local_now.date() + timedelta(days=1), run_at, tzinfo=tz)
    return candidate.astimezone(UTC)


def run_forever(
    job: Callable[[], object],
    *,
    clock: Clock,
    run_at: time,
    tz: ZoneInfo,
    stop: threading.Event,
    max_sleep_s: float = 60.0,
) -> None:
    """Call ``job`` every day at ``run_at`` (local time) until ``stop`` is set.

    Sleeps in short chunks so clock changes (suspend, DST) are picked up. A failing job
    is logged and never stops the loop.
    """
    due = next_run_at(clock.now(), run_at, tz)
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
