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


def test_concurrent_migrate_on_fresh_file(tmp_path: Path) -> None:
    """Web and worker migrating the same fresh file at first boot."""
    import threading

    from veille.db import migrate

    path = tmp_path / "fresh.db"
    barrier = threading.Barrier(2)
    results: list[list[str]] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait()
            results.append(migrate(path))
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    applied = sorted(v for r in results for v in r)
    assert applied == [v for v, _ in available_migrations()]  # each applied exactly once
    conn = connect(path)
    assert [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY 1")] == (
        applied
    )
    conn.close()


def test_already_applied_inside_transaction_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another process applied the version between our first read and our lock."""
    (tmp_path / "001_a.sql").write_text("CREATE TABLE a (x);", encoding="utf-8")
    conn = connect(tmp_path / "t.db")
    other = connect(tmp_path / "t.db")
    assert apply_migrations(other, tmp_path) == ["001"]  # type: ignore[arg-type]
    import veille.db.migrations_runner as runner

    original = runner.applied_versions
    # Simulate the stale first read: the version looks pending outside the lock.
    monkeypatch.setattr(runner, "applied_versions", lambda c: original(c) and set())
    assert apply_migrations(conn, tmp_path) == []  # type: ignore[arg-type]
    assert not conn.in_transaction
    conn.close()
    other.close()


def test_split_statements() -> None:
    from veille.db.migrations_runner import split_statements

    sql = (
        "-- header; with a semicolon\n"
        "CREATE TABLE a (x TEXT DEFAULT ';');\n"
        "CREATE TRIGGER t AFTER INSERT ON a BEGIN\n  SELECT 1;\n  SELECT 2;\nEND;\n"
        "-- trailing comment\n"
        "SELECT 3"
    )
    parts = split_statements(sql)
    assert len(parts) == 3
    assert parts[0].endswith("DEFAULT ';');")
    assert parts[1].startswith("CREATE TRIGGER") and parts[1].endswith("END;")
    assert parts[2].endswith("SELECT 3;")
    assert split_statements("-- only a comment\n") == []
    # Unterminated text is kept (so that executing it fails loudly), never dropped.
    assert split_statements("CREATE TRIGGER t AFTER INSERT ON a BEGIN SELECT 1") == [
        "CREATE TRIGGER t AFTER INSERT ON a BEGIN SELECT 1"
    ]
