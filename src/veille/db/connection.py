"""SQLite connection factory."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

MEMORY = ":memory:"


def connect(path: Path | str) -> sqlite3.Connection:
    """Open a SQLite connection configured for the application.

    Parent directories are created. The connection is in autocommit mode
    (``isolation_level=None``): callers manage transactions explicitly with
    ``BEGIN`` / ``COMMIT``.
    """
    target = str(path)
    if target != MEMORY:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA synchronous=NORMAL")
    logger.debug("Opened SQLite database %s", target)
    return conn
