"""Applies numbered SQL migrations (``migrations/NNN_name.sql``) in order."""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import UTC, datetime
from importlib import resources
from importlib.resources.abc import Traversable

logger = logging.getLogger(__name__)

_FILE_RE = re.compile(r"^(\d{3})_[A-Za-z0-9_]+\.sql$")

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
)
"""


def available_migrations(
    package: Traversable | None = None,
) -> list[tuple[str, Traversable]]:
    """(version, file) pairs sorted by version. ``package`` defaults to the bundled dir."""
    folder = package if package is not None else resources.files("veille.db") / "migrations"
    found: list[tuple[str, Traversable]] = []
    for entry in folder.iterdir():
        match = _FILE_RE.match(entry.name)
        if match and entry.is_file():
            found.append((match.group(1), entry))
    found.sort(key=lambda pair: pair[0])
    versions = [version for version, _ in found]
    if len(set(versions)) != len(versions):
        raise RuntimeError(f"Duplicate migration versions: {versions}")
    return found


def applied_versions(conn: sqlite3.Connection) -> set[str]:
    conn.execute(_CREATE_TABLE)
    return {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}


def apply_migrations(conn: sqlite3.Connection, package: Traversable | None = None) -> list[str]:
    """Apply pending migrations, each in its own transaction. Returns new versions."""
    done = applied_versions(conn)
    newly: list[str] = []
    for version, file in available_migrations(package):
        if version in done:
            continue
        sql = file.read_text(encoding="utf-8")
        applied_at = datetime.now(UTC).isoformat(timespec="microseconds")
        # The version matches \d{3} and applied_at is generated here: safe to inline.
        script = (
            "BEGIN IMMEDIATE;\n"
            f"{sql}\n;\n"
            "INSERT INTO schema_migrations (version, applied_at) "
            f"VALUES ('{version}', '{applied_at}');\n"
            "COMMIT;"
        )
        try:
            conn.executescript(script)
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            logger.exception("Migration %s failed", file.name)
            raise
        logger.info("Applied migration %s", file.name)
        newly.append(version)
    return newly
