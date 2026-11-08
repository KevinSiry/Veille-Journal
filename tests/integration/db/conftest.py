from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from veille.clock import FixedClock
from veille.contracts import (
    ArticleDraft,
    ArticleStatus,
    Entry,
    Item,
    ItemStatus,
    Origin,
    SourceLink,
)
from veille.db import SqliteStore, open_store

NOW = datetime(2026, 11, 8, 4, 0, tzinfo=UTC)
DAY = date(2026, 11, 8)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "veille.db"


@pytest.fixture
def store(db_path: Path) -> Iterator[SqliteStore]:
    with open_store(db_path, clock=FixedClock(NOW)) as s:
        yield s


def make_item(
    url: str,
    *,
    fetched_at: datetime = NOW,
    status: ItemStatus = ItemStatus.USED,
    content: str = "texte",
    embedding: tuple[float, ...] | None = (0.5, 0.25),
) -> Item:
    return Item(
        url=url,
        canonical_url=url,
        title=f"Titre {url}",
        published_at=fetched_at,
        fetched_at=fetched_at,
        snippet="",
        source_name="Le Monde",
        origin=Origin.RSS,
        content_text=content,
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        embedding=embedding,
        relevance=0.9,
        status=status,
    )


def make_draft(
    theme_id: int,
    *,
    day: date = DAY,
    title: str = "Veille du jour",
    body: str = "Corps de l'article",
    links: tuple[tuple[str, ...], ...] = (),
) -> ArticleDraft:
    entries = tuple(
        Entry(
            headline_fr=f"Titre {i}",
            summary_fr=f"Résumé {i}",
            source_links=tuple(SourceLink(name="src", url=u, item_canonical_url=u) for u in urls),
        )
        for i, urls in enumerate(links)
    )
    return ArticleDraft(
        theme_id=theme_id,
        day=day,
        title=title,
        body_md=body,
        model="qwen3:8b",
        generated_at=NOW,
        status=ArticleStatus.OK if entries else ArticleStatus.EMPTY,
        entries=entries,
    )
