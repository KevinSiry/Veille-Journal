"""In-memory fakes for the web tests (no veille.db dependency)."""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any

from veille.contracts import (
    HIGHLIGHT_END,
    HIGHLIGHT_START,
    Article,
    ArticleDraft,
    ArticleStatus,
    Item,
    LLMError,
    Run,
    RunStats,
    RunStatus,
    RunTrigger,
    SearchHit,
    Source,
    Theme,
    ThemeConflictError,
)

T0 = datetime(2026, 1, 1, tzinfo=UTC)


class FakeStore:
    """Implements the Store Protocol in memory. Shared state, so one instance can be
    returned by the factory for every request; ``closed`` counts close() calls."""

    def __init__(self) -> None:
        self.themes: dict[int, Theme] = {}
        self.sources: dict[int, Source] = {}
        self.articles: dict[tuple[int, date], Article] = {}
        self.runs: list[Run] = []
        self.ping_ok = True
        self.closed = 0
        self._ids = itertools.count(1)

    # themes
    def list_themes(self, *, active_only: bool = False) -> list[Theme]:
        themes = sorted(self.themes.values(), key=lambda t: t.name.casefold())
        return [t for t in themes if t.active or not active_only]

    def get_theme(self, theme_id: int) -> Theme | None:
        return self.themes.get(theme_id)

    def _check_name(self, name: str, exclude: int | None = None) -> None:
        if any(t.name == name and t.id != exclude for t in self.themes.values()):
            raise ThemeConflictError(name)

    def create_theme(self, name: str, description: str, keywords: Sequence[str]) -> Theme:
        self._check_name(name)
        theme = Theme(next(self._ids), name, description, tuple(keywords), True, T0)
        self.themes[theme.id] = theme
        return theme

    def update_theme(
        self,
        theme_id: int,
        *,
        name: str,
        description: str,
        keywords: Sequence[str],
        active: bool,
    ) -> Theme:
        self._check_name(name, exclude=theme_id)
        theme = replace(
            self.themes[theme_id],
            name=name,
            description=description,
            keywords=tuple(keywords),
            active=active,
        )
        self.themes[theme_id] = theme
        return theme

    def delete_theme(self, theme_id: int) -> None:
        self.themes.pop(theme_id, None)
        self.sources = {k: s for k, s in self.sources.items() if s.theme_id != theme_id}
        self.articles = {k: a for k, a in self.articles.items() if a.theme_id != theme_id}

    # sources
    def list_sources(self, theme_id: int, *, active_only: bool = False) -> list[Source]:
        return [s for s in self.sources.values() if s.theme_id == theme_id]

    def add_source(self, theme_id: int, url: str, label: str) -> Source:
        source = Source(next(self._ids), theme_id, url, label, True)
        self.sources[source.id] = source
        return source

    def delete_source(self, source_id: int) -> None:
        self.sources.pop(source_id, None)

    # items (unused by the web layer)
    def known_canonical_urls(
        self, theme_id: int, urls: Iterable[str], *, before: datetime | None = None
    ) -> set[str]:
        return set()

    def purge_old_content(self, older_than: datetime) -> int:
        return 0

    # articles
    def add_article(
        self,
        theme: Theme,
        day: date,
        *,
        title: str = "Titre",
        body_md: str = "Corps.",
        status: ArticleStatus = ArticleStatus.OK,
    ) -> Article:
        article = Article(
            next(self._ids), theme.id, theme.name, day, title, body_md, "m", T0, 1, status
        )
        self.articles[(theme.id, day)] = article
        return article

    def get_article(self, theme_id: int, day: date) -> Article | None:
        return self.articles.get((theme_id, day))

    def save_article(self, draft: ArticleDraft, items: Sequence[Item], *, force: bool) -> bool:
        raise NotImplementedError

    def list_articles_for_day(self, day: date) -> list[Article]:
        return self.list_articles(day=day)

    def _days(self, theme_id: int | None) -> list[date]:
        return sorted(
            {a.day for a in self.articles.values() if theme_id in (None, a.theme_id)},
            reverse=True,
        )

    def list_days(
        self, *, theme_id: int | None = None, limit: int = 30, offset: int = 0
    ) -> list[date]:
        return self._days(theme_id)[offset : offset + limit]

    def count_days(self, *, theme_id: int | None = None) -> int:
        return len(self._days(theme_id))

    def list_articles(
        self, *, theme_id: int | None = None, day: date | None = None
    ) -> list[Article]:
        return [
            a
            for a in self.articles.values()
            if theme_id in (None, a.theme_id) and day in (None, a.day)
        ]

    def adjacent_days(self, day: date) -> tuple[date | None, date | None]:
        days = self._days(None)
        previous = max((d for d in days if d < day), default=None)
        following = min((d for d in days if d > day), default=None)
        return previous, following

    def _matches(self, query: str, theme_id: int | None) -> list[Article]:
        q = query.casefold()
        return [
            a
            for a in sorted(self.articles.values(), key=lambda a: a.day, reverse=True)
            if theme_id in (None, a.theme_id)
            and (q in a.title.casefold() or q in a.body_md.casefold())
        ]

    def search(
        self, query: str, *, theme_id: int | None = None, limit: int = 20, offset: int = 0
    ) -> list[SearchHit]:
        hits = []
        for article in self._matches(query, theme_id)[offset : offset + limit]:
            text = f"{article.title} {article.body_md}"
            start = text.casefold().find(query.casefold())
            end = start + len(query)
            snippet = text[:start] + HIGHLIGHT_START + text[start:end] + HIGHLIGHT_END + text[end:]
            hits.append(SearchHit(article, snippet, -1.0))
        return hits

    def count_search(self, query: str, *, theme_id: int | None = None) -> int:
        return len(self._matches(query, theme_id))

    # runs
    def start_run(self, trigger: RunTrigger, now: datetime) -> int | None:
        run = Run(len(self.runs) + 1, now, None, trigger, RunStatus.RUNNING)
        self.runs.append(run)
        return run.id

    def finish_run(
        self,
        run_id: int,
        *,
        status: RunStatus,
        stats: RunStats,
        finished_at: datetime,
        error: str | None = None,
    ) -> None:
        index = run_id - 1
        self.runs[index] = replace(
            self.runs[index],
            status=status,
            stats=stats.to_json_dict(),
            finished_at=finished_at,
            error=error,
        )

    def heartbeat_run(self, run_id: int, now: datetime) -> None:
        return None

    def last_run(self) -> Run | None:
        return self.runs[-1] if self.runs else None

    def list_runs(self, limit: int = 20) -> list[Run]:
        return list(reversed(self.runs))[:limit]

    def ping(self) -> bool:
        if isinstance(self.ping_ok, Exception):
            raise self.ping_ok
        return self.ping_ok

    def close(self) -> None:
        self.closed += 1


class FakeLLM:
    model = "fake"

    def __init__(self, result: Mapping[str, Any] | Exception | None = None) -> None:
        self.result = result if result is not None else {"keywords": []}
        self.calls: list[tuple[str, str]] = []

    def chat_json(self, system: str, user: str, schema: Mapping[str, Any]) -> dict[str, Any]:
        self.calls.append((system, user))
        if isinstance(self.result, Exception):
            raise self.result
        return dict(self.result)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise LLMError("not used")
