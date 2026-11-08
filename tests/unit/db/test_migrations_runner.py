from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from veille.db.connection import connect
from veille.db.migrations_runner import apply_migrations, available_migrations


def test_bundled_migrations_are_found() -> None:
    versions = [v for v, _ in available_migrations()]
    assert versions[0] == "001"
    assert versions == sorted(versions)


def test_custom_folder_order_and_ignored_files(tmp_path: Path) -> None:
    (tmp_path / "002_b.sql").write_text("CREATE TABLE b (x);", encoding="utf-8")
    (tmp_path / "001_a.sql").write_text("CREATE TABLE a (x);", encoding="utf-8")
    (tmp_path / "README.md").write_text("ignored", encoding="utf-8")
    conn = connect(tmp_path / "db" / "t.db")
    assert apply_migrations(conn, tmp_path) == ["001", "002"]  # type: ignore[arg-type]
    assert apply_migrations(conn, tmp_path) == []  # type: ignore[arg-type]
    conn.close()


def test_duplicate_versions_rejected(tmp_path: Path) -> None:
    (tmp_path / "001_a.sql").write_text("", encoding="utf-8")
    (tmp_path / "001_b.sql").write_text("", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Duplicate"):
        available_migrations(tmp_path)  # type: ignore[arg-type]


def test_failed_migration_rolls_back(tmp_path: Path) -> None:
    (tmp_path / "001_ok.sql").write_text("CREATE TABLE ok (x);", encoding="utf-8")
    (tmp_path / "002_bad.sql").write_text(
        "CREATE TABLE partial (x);\nTHIS IS NOT SQL;", encoding="utf-8"
    )
    conn = connect(tmp_path / "t.db")
    with pytest.raises(sqlite3.OperationalError):
        apply_migrations(conn, tmp_path)  # type: ignore[arg-type]
    assert not conn.in_transaction
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "ok" in tables
    assert "partial" not in tables
    versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations")]
    assert versions == ["001"]
    conn.close()


def test_connect_pragmas(tmp_path: Path) -> None:
    conn = connect(tmp_path / "nested" / "dir" / "x.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
    assert isinstance(conn.execute("SELECT 1 AS one").fetchone(), sqlite3.Row)
    conn.close()


def test_connect_memory() -> None:
    conn = connect(":memory:")
    assert conn.execute("SELECT 1").fetchone()[0] == 1
    conn.close()
