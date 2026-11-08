from __future__ import annotations

from datetime import date, timedelta

import pytest

from veille.contracts import (
    ArticleStatus,
    ItemStatus,
)
from veille.db import SqliteStore
from veille.db.store import unpack_embedding

from .conftest import DAY, NOW, make_draft, make_item


def _count(store: SqliteStore, table: str) -> int:
    n: int = store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return n


def test_save_article_full_graph(store: SqliteStore) -> None:
    t = store.create_theme("Climat", "", [])
    items = [make_item("https://a"), make_item("https://b"), make_item("https://c")]
    draft = make_draft(t.id, links=(("https://a", "https://b"), ("https://c",)))
    assert store.save_article(draft, items, force=False) is True

    art = store.get_article(t.id, DAY)
    assert art is not None
    assert art.theme_name == "Climat"
    assert art.day == DAY
    assert art.entry_count == 2
    assert art.status is ArticleStatus.OK
    assert art.generated_at == NOW
    conn = store.connection
    entries = conn.execute(
        "SELECT id, position, headline_fr FROM article_entries ORDER BY position"
    ).fetchall()
    assert [(e["position"], e["headline_fr"]) for e in entries] == [(0, "Titre 0"), (1, "Titre 1")]
    mapping = conn.execute(
        "SELECT e.position, i.canonical_url FROM entry_sources s "
        "JOIN article_entries e ON e.id = s.entry_id JOIN items i ON i.id = s.item_id "
        "ORDER BY e.position, i.canonical_url"
    ).fetchall()
    assert [tuple(r) for r in mapping] == [(0, "https://a"), (0, "https://b"), (1, "https://c")]
    blob = conn.execute("SELECT embedding FROM items WHERE canonical_url='https://a'").fetchone()[0]
    assert unpack_embedding(blob) == (0.5, 0.25)


def test_save_article_without_force_writes_nothing(store: SqliteStore) -> None:
    t = store.create_theme("T", "", [])
    assert store.save_article(make_draft(t.id, body="first"), [make_item("https://a")], force=False)
    before = {tbl: _count(store, tbl) for tbl in ("items", "articles", "article_entries")}
    again = make_draft(t.id, body="second", links=(("https://z",),))
    assert store.save_article(again, [make_item("https://z")], force=False) is False
    assert {tbl: _count(store, tbl) for tbl in before} == before
    art = store.get_article(t.id, DAY)
    assert art is not None
    assert art.body_md == "first"


def test_save_article_force_replaces(store: SqliteStore) -> None:
    t = store.create_theme("T", "", [])
    store.save_article(
        make_draft(t.id, body="ancienword", links=(("https://a",), ("https://b",))),
        [make_item("https://a"), make_item("https://b")],
        force=False,
    )
    first = store.get_article(t.id, DAY)
    assert first is not None

    new_items = [
        make_item("https://a", status=ItemStatus.DROPPED, embedding=None),
        make_item("https://c"),
    ]
    forced = make_draft(t.id, title="Nouveau", body="nouveauword", links=(("https://c",),))
    assert store.save_article(forced, new_items, force=True) is True

    art = store.get_article(t.id, DAY)
    assert art is not None
    assert art.id == first.id
    assert art.title == "Nouveau"
    assert art.entry_count == 1
    assert _count(store, "article_entries") == 1
    assert _count(store, "entry_sources") == 1
    assert _count(store, "items") == 3
    row = store.connection.execute(
        "SELECT status, embedding FROM items WHERE canonical_url = 'https://a'"
    ).fetchone()
    assert row["status"] == "dropped"
    assert row["embedding"] is not None  # COALESCE keeps the previous vector
    assert store.search("ancienword") == []
    assert [h.article.id for h in store.search("nouveauword")] == [art.id]


def test_upsert_keeps_first_fetched_at(store: SqliteStore) -> None:
    t = store.create_theme("T", "", [])
    old = NOW - timedelta(days=3)
    store.save_article(make_draft(t.id), [make_item("https://a", fetched_at=old)], force=False)
    store.save_article(
        make_draft(t.id, day=DAY + timedelta(days=1)), [make_item("https://a")], force=False
    )
    assert store.known_canonical_urls(t.id, ["https://a"], before=NOW) == {"https://a"}


def test_unknown_source_link_rolls_back(store: SqliteStore) -> None:
    t = store.create_theme("T", "", [])
    draft = make_draft(t.id, links=(("https://a",), ("https://missing",)))
    with pytest.raises(ValueError, match="unknown item"):
        store.save_article(draft, [make_item("https://a")], force=False)
    for table in ("items", "articles", "article_entries", "entry_sources"):
        assert _count(store, table) == 0
    assert not store.connection.in_transaction


def test_error_inside_transaction_rolls_back(store: SqliteStore) -> None:
    draft = make_draft(424242, links=(("https://a",),))  # unknown theme -> FK error
    with pytest.raises(Exception, match="FOREIGN KEY"):
        store.save_article(draft, [make_item("https://a")], force=False)
    assert _count(store, "items") == 0
    assert not store.connection.in_transaction


def test_day_listing(store: SqliteStore) -> None:
    b = store.create_theme("B-theme", "", [])
    a = store.create_theme("A-theme", "", [])
    d1, d2, d3 = date(2026, 11, 1), date(2026, 11, 3), date(2026, 11, 5)
    for theme, day in ((a, d1), (b, d1), (a, d2), (b, d3)):
        store.save_article(make_draft(theme.id, day=day), [], force=False)

    assert store.list_days() == [d3, d2, d1]
    assert store.list_days(limit=1, offset=1) == [d2]
    assert store.list_days(theme_id=a.id) == [d2, d1]
    assert store.count_days() == 3
    assert store.count_days(theme_id=b.id) == 2
    assert store.adjacent_days(d2) == (d1, d3)
    assert store.adjacent_days(d1) == (None, d2)
    assert store.adjacent_days(d3) == (d2, None)
    assert store.adjacent_days(date(2026, 11, 4)) == (d2, d3)

    assert [x.theme_name for x in store.list_articles_for_day(d1)] == ["A-theme", "B-theme"]
    assert store.list_articles_for_day(date(2020, 1, 1)) == []
    assert [(x.day, x.theme_name) for x in store.list_articles()] == [
        (d3, "B-theme"),
        (d2, "A-theme"),
        (d1, "A-theme"),
        (d1, "B-theme"),
    ]
    assert [x.day for x in store.list_articles(theme_id=a.id)] == [d2, d1]
    assert [x.theme_id for x in store.list_articles(theme_id=b.id, day=d1)] == [b.id]
    assert store.get_article(a.id, d3) is None
