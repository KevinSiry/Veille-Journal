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


def split_statements(sql: str) -> list[str]:
    """Split a migration script into statements (``;`` inside strings, comments and
    ``CREATE TRIGGER ... END`` bodies is handled by ``sqlite3.complete_statement``)."""
    statements: list[str] = []
    buffer = ""
    for part in sql.split(";"):
        buffer += part + ";"
        if sqlite3.complete_statement(buffer):
            if _has_code(buffer):
                statements.append(buffer.strip())
            buffer = ""
    if _has_code(buffer.rstrip(";")):  # trailing statement without ';'
        statements.append(buffer.rstrip(";").strip())
    return statements


def _has_code(text: str) -> bool:
    lines = (line.split("--", 1)[0] for line in text.splitlines())
    return bool("".join(lines).strip(" \t\r\n;"))


def apply_migrations(conn: sqlite3.Connection, package: Traversable | None = None) -> list[str]:
    """Apply pending migrations, each in its own ``BEGIN IMMEDIATE`` transaction.

    The applied versions are re-read inside the write transaction, so two processes
    migrating the same fresh file concurrently (web + worker at first boot) apply each
    migration once: the second one waits for the lock, then skips. Returns new versions.
    """
    done = applied_versions(conn)
    newly: list[str] = []
    for version, file in available_migrations(package):
        if version in done:
            continue
        statements = split_statements(file.read_text(encoding="utf-8"))
        conn.execute("BEGIN IMMEDIATE")
        try:
            already = conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?", (version,)
            ).fetchone()
            if already is None:
                for statement in statements:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                    (version, datetime.now(UTC).isoformat(timespec="microseconds")),
                )
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            logger.exception("Migration %s failed", file.name)
            raise
        conn.execute("COMMIT")
        if already is not None:
            logger.info("Migration %s already applied by another process", file.name)
            continue
        logger.info("Applied migration %s", file.name)
        newly.append(version)
    return newly
