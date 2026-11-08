"""SQLite persistence layer (WAL + FTS5)."""

from __future__ import annotations

from pathlib import Path

from veille.contracts import Clock
from veille.db.connection import connect
from veille.db.migrations_runner import apply_migrations
from veille.db.store import SqliteStore

__all__ = ["SqliteStore", "connect", "migrate", "open_store"]


def migrate(path: Path) -> list[str]:
    """Apply pending migrations to the database at ``path``. Returns new versions."""
    conn = connect(path)
    try:
        return apply_migrations(conn)
    finally:
        conn.close()


def open_store(path: Path, *, clock: Clock | None = None) -> SqliteStore:
    """Connect, migrate and return a ready-to-use store."""
    conn = connect(path)
    try:
        apply_migrations(conn)
    except BaseException:
        conn.close()
        raise
    return SqliteStore(conn, clock=clock)
