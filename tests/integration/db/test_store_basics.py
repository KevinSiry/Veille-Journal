from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from veille.contracts import Store, ThemeConflictError
from veille.db import SqliteStore, migrate, open_store
from veille.db.connection import connect

from .conftest import NOW, make_draft, make_item


def test_migrate_fresh_then_idempotent(db_path: Path) -> None:
    assert migrate(db_path) == ["001"]
    assert migrate(db_path) == []
    conn = connect(db_path)
    rows = conn.execute("SELECT version, applied_at FROM schema_migrations").fetchall()
    assert [r["version"] for r in rows] == ["001"]
    conn.close()
    with open_store(db_path) as store:
        assert store.ping()


def test_implements_protocol(store: SqliteStore) -> None:
    assert isinstance(store, Store)


def test_ping_false_after_close(db_path: Path) -> None:
    store = open_store(db_path)
    store.close()
    assert store.ping() is False


def test_theme_crud(store: SqliteStore) -> None:
    t = store.create_theme("Climat", "Actualité climatique", ["GIEC", "COP30"])
    assert t.keywords == ("GIEC", "COP30")
    assert t.active is True
    assert t.created_at == NOW
    assert store.get_theme(t.id) == t
    assert store.get_theme(9999) is None

    other = store.create_theme("Aéronautique", "", [])
    assert [x.name for x in store.list_themes()] == ["Aéronautique", "Climat"]

    updated = store.update_theme(
        t.id, name="Climat & énergie", description="d", keywords=["CO2"], active=False
    )
    assert updated.name == "Climat & énergie"
    assert updated.keywords == ("CO2",)
    assert updated.active is False
    assert [x.id for x in store.list_themes(active_only=True)] == [other.id]


def test_theme_conflicts(store: SqliteStore) -> None:
    a = store.create_theme("A", "", [])
    store.create_theme("B", "", [])
    with pytest.raises(ThemeConflictError):
        store.create_theme("A", "", [])
    with pytest.raises(ThemeConflictError):
        store.update_theme(a.id, name="B", description="", keywords=[], active=True)
    with pytest.raises(KeyError):
        store.update_theme(999, name="Z", description="", keywords=[], active=True)
    assert not store.connection.in_transaction


def test_sources(store: SqliteStore) -> None:
    t = store.create_theme("T", "", [])
    s1 = store.add_source(t.id, "https://ex.org/rss", "Ex")
    assert s1.kind == "rss"
    assert s1.active
    s2 = store.add_source(t.id, "https://other.org/feed", "")
    with pytest.raises(ValueError, match="Source"):
        store.add_source(t.id, "https://ex.org/rss", "dup")
    with pytest.raises(ValueError, match="Source"):
        store.add_source(999, "https://ex.org/rss", "no theme")
    assert store.list_sources(t.id) == [s1, s2]
    store.connection.execute("UPDATE sources SET active = 0 WHERE id = ?", (s2.id,))
    assert store.list_sources(t.id, active_only=True) == [s1]
    store.delete_source(s1.id)
    assert [s.id for s in store.list_sources(t.id)] == [s2.id]


def test_delete_theme_cascades(store: SqliteStore) -> None:
    t = store.create_theme("Cascade", "", [])
    keep = store.create_theme("Garde", "", [])
    store.add_source(t.id, "https://ex.org/rss", "")
    store.save_article(
        make_draft(t.id, body="unique cascadeword", links=(("https://a",),)),
        [make_item("https://a")],
        force=False,
    )
    store.save_article(make_draft(keep.id, body="autre"), [], force=False)
    assert store.search("cascadeword")
    store.delete_theme(t.id)
    conn = store.connection
    for table in ("sources", "items", "articles", "article_entries", "entry_sources"):
        n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE 0 = 0").fetchone()[0]
        assert n == (1 if table == "articles" else 0), table
    assert store.search("cascadeword") == []
    assert store.get_theme(t.id) is None
    conn.execute("INSERT INTO articles_fts(articles_fts) VALUES ('integrity-check')")


def test_known_canonical_urls(store: SqliteStore) -> None:
    t = store.create_theme("T", "", [])
    other = store.create_theme("O", "", [])
    old = make_item("https://old", fetched_at=NOW - timedelta(days=2))
    today = make_item("https://today")
    store.save_article(make_draft(t.id), [old, today], force=False)
    urls = ["https://old", "https://today", "https://never"]
    assert store.known_canonical_urls(t.id, urls) == {"https://old", "https://today"}
    assert store.known_canonical_urls(t.id, iter(urls), before=NOW - timedelta(hours=1)) == {
        "https://old"
    }
    assert store.known_canonical_urls(other.id, urls) == set()
    assert store.known_canonical_urls(t.id, []) == set()
    many = [f"https://x/{i}" for i in range(1200)] + ["https://old"]
    assert store.known_canonical_urls(t.id, many) == {"https://old"}


def test_purge_old_content(store: SqliteStore) -> None:
    t = store.create_theme("T", "", [])
    store.save_article(
        make_draft(t.id),
        [make_item("https://old", fetched_at=NOW - timedelta(days=40)), make_item("https://new")],
        force=False,
    )
    assert store.purge_old_content(NOW - timedelta(days=30)) == 1
    assert store.purge_old_content(NOW - timedelta(days=30)) == 0
    rows = dict(store.connection.execute("SELECT canonical_url, content_text FROM items"))
    assert rows == {"https://old": "", "https://new": "texte"}
    assert store.known_canonical_urls(t.id, ["https://old"]) == {"https://old"}


def test_concurrent_writer_waits_not_fails(db_path: Path) -> None:
    with open_store(db_path) as a, open_store(db_path) as b:
        a.create_theme("A", "", [])
        b.create_theme("B", "", [])
        assert {t.name for t in a.list_themes()} == {"A", "B"}


def test_foreign_keys_enforced(store: SqliteStore) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.connection.execute("INSERT INTO sources (theme_id, url) VALUES (424242, 'https://x')")
