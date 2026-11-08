"""In-memory fakes for pipeline tests (no veille.db / veille.collect dependency)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from veille.contracts import (
    Article,
    ArticleDraft,
    ArticleStatus,
    ExtractedPage,
    Item,
    Origin,
    RawItem,
    RunStats,
    RunStatus,
    RunTrigger,
    Source,
    Theme,
)

NOW = datetime(2026, 11, 8, 4, 30, tzinfo=UTC)  # 05:30 in Paris


def make_theme(
    theme_id: int = 1,
    name: str = "Azure Cloud",
    description: str = "Actualités de Microsoft Azure",
    keywords: Sequence[str] = ("azure",),
    active: bool = True,
) -> Theme:
    return Theme(theme_id, name, description, tuple(keywords), active, NOW)


def raw(
    url: str,
    title: str,
    *,
    snippet: str = "",
    source_name: str = "Source",
    published_at: datetime | None = NOW,
    origin: Origin = Origin.SEARXNG,
    source_id: int | None = None,
) -> RawItem:
    return RawItem(url, title, published_at, snippet, source_name, origin, source_id)


@dataclass
class SavedArticle:
    draft: ArticleDraft
    items: list[Item]
    force: bool


@dataclass
class FakeStore:
    themes: list[Theme] = field(default_factory=list)
    sources: dict[int, list[Source]] = field(default_factory=dict)
    known: dict[int, set[str]] = field(default_factory=dict)
    existing_articles: set[tuple[int, date]] = field(default_factory=set)
    locked: bool = False
    purge_error: Exception | None = None
    saved: list[SavedArticle] = field(default_factory=list)
    known_calls: list[tuple[int, list[str], datetime | None]] = field(default_factory=list)
    runs: dict[int, dict[str, object]] = field(default_factory=dict)
    purged_before: list[datetime] = field(default_factory=list)
    heartbeats: list[tuple[int, datetime]] = field(default_factory=list)

    def list_themes(self, *, active_only: bool = False) -> list[Theme]:
        return [t for t in self.themes if t.active or not active_only]

    def list_sources(self, theme_id: int, *, active_only: bool = False) -> list[Source]:
        return [s for s in self.sources.get(theme_id, []) if s.active or not active_only]

    def known_canonical_urls(
        self, theme_id: int, urls: Iterable[str], *, before: datetime | None = None
    ) -> set[str]:
        urls = list(urls)
        self.known_calls.append((theme_id, urls, before))
        return {u for u in urls if u in self.known.get(theme_id, set())}

    def purge_old_content(self, older_than: datetime) -> int:
        self.purged_before.append(older_than)
        if self.purge_error is not None:
            raise self.purge_error
        return 0

    def get_article(self, theme_id: int, day: date) -> Article | None:
        if (theme_id, day) not in self.existing_articles:
            return None
        return Article(1, theme_id, "x", day, "t", "b", "m", NOW, 0, ArticleStatus.OK)

    def save_article(self, draft: ArticleDraft, items: Sequence[Item], *, force: bool) -> bool:
        key = (draft.theme_id, draft.day)
        if key in self.existing_articles and not force:
            return False
        item_urls = {i.canonical_url for i in items}
        for entry in draft.entries:
            for link in entry.source_links:
                assert link.item_canonical_url in item_urls
        self.existing_articles.add(key)
        self.saved.append(SavedArticle(draft, list(items), force))
        return True

    def start_run(self, trigger: RunTrigger, now: datetime) -> int | None:
        if self.locked:
            return None
        run_id = len(self.runs) + 1
        self.runs[run_id] = {"trigger": trigger, "started_at": now, "status": RunStatus.RUNNING}
        return run_id

    def heartbeat_run(self, run_id: int, now: datetime) -> None:
        self.heartbeats.append((run_id, now))

    def finish_run(
        self,
        run_id: int,
        *,
        status: RunStatus,
        stats: RunStats,
        finished_at: datetime,
        error: str | None = None,
    ) -> None:
        self.runs[run_id].update(status=status, stats=stats, finished_at=finished_at, error=error)

    def article_for(self, theme_id: int) -> SavedArticle:
        matches = [s for s in self.saved if s.draft.theme_id == theme_id]
        assert matches, f"no article saved for theme {theme_id}"
        return matches[-1]


class FakeCollector:
    def __init__(
        self,
        items: dict[int, list[RawItem]] | list[RawItem] | None = None,
        *,
        name: str = "fake",
        error: Exception | None = None,
        fail_for: Iterable[int] = (),
    ) -> None:
        self.name = name
        self.items = items if items is not None else []
        self.error = error
        self.fail_for = set(fail_for)
        self.calls: list[tuple[Theme, list[Source]]] = []

    def collect(self, theme: Theme, sources: Sequence[Source]) -> list[RawItem]:
        self.calls.append((theme, list(sources)))
        if self.error is not None and (not self.fail_for or theme.id in self.fail_for):
            raise self.error
        if isinstance(self.items, dict):
            return list(self.items.get(theme.id, []))
        return list(self.items)


class FakeExtractor:
    def __init__(
        self,
        pages: dict[str, ExtractedPage] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.pages = pages or {}
        self.error = error
        self.calls: list[str] = []

    def extract(self, url: str) -> ExtractedPage | None:
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        return self.pages.get(url)


def identity_canonicalize(url: str) -> str:
    return url.split("?", 1)[0].split("#", 1)[0].rstrip("/").lower()
