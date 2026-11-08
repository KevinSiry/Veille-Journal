"""Reading pages: today, archive, search."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from starlette.responses import Response

from veille.clock import local_day
from veille.contracts import EMPTY_ARTICLE_TEXT, Article, ArticleStatus, Run, Theme
from veille.web.deps import (
    SettingsDep,
    StoreDep,
    parse_day,
    parse_page,
    parse_theme_id,
    today,
)
from veille.web.pagination import Page
from veille.web.templating import render

router = APIRouter()

DAYS_PER_PAGE = 30
RESULTS_PER_PAGE = 20


def _theme_filter(themes: list[Theme], theme: str | None) -> int | None:
    theme_id = parse_theme_id(theme)
    if theme_id is not None and all(t.id != theme_id for t in themes):
        return None
    return theme_id


def _failed_theme_ids(run: Run | None, day: date, tz: ZoneInfo) -> set[int]:
    """Themes whose generation failed in ``run``, if that run produced ``day``."""
    if run is None or local_day(run.started_at, tz) != day:
        return set()
    themes = run.stats.get("themes") if isinstance(run.stats, Mapping) else None
    if not isinstance(themes, list | tuple):
        return set()
    return {
        item["theme_id"]
        for item in themes
        if isinstance(item, Mapping)
        and item.get("status") == "error"
        and isinstance(item.get("theme_id"), int)
    }


@router.get("/")
def today_page(
    request: Request, store: StoreDep, settings: SettingsDep, day: str | None = None
) -> Response:
    current = today(request)
    shown = parse_day(day) if day else current
    failed = _failed_theme_ids(store.last_run(), shown, settings.tz)
    blocks: list[dict[str, Any]] = []
    for theme in store.list_themes(active_only=True):
        article = store.get_article(theme.id, shown)
        if article is None:
            state = "failed" if theme.id in failed else "missing"
        elif article.status == ArticleStatus.OK:
            state = "ok"
        elif article.status == ArticleStatus.EMPTY:
            state = "empty"
        else:
            state = "error"
        blocks.append({"theme": theme, "article": article, "state": state})
    previous_day, next_day = store.adjacent_days(shown)
    return render(
        request,
        "today.html",
        {
            "day": shown,
            "is_today": shown == current,
            "today": current,
            "blocks": blocks,
            "previous_day": previous_day,
            "next_day": next_day,
            "empty_text": EMPTY_ARTICLE_TEXT,
        },
    )


@router.get("/archive")
def archive(
    request: Request, store: StoreDep, theme: str | None = None, page: str | None = None
) -> Response:
    themes = store.list_themes()
    theme_id = _theme_filter(themes, theme)
    pager = Page(parse_page(page), DAYS_PER_PAGE, store.count_days(theme_id=theme_id))
    days = store.list_days(theme_id=theme_id, limit=pager.size, offset=pager.offset)
    return render(
        request,
        "archive.html",
        {"themes": themes, "theme_id": theme_id, "days": days, "pager": pager},
    )


@router.get("/archive/{day}")
def archive_day(request: Request, store: StoreDep, day: str, theme: str | None = None) -> Response:
    shown: date = parse_day(day)
    themes = store.list_themes()
    theme_id = _theme_filter(themes, theme)
    articles: list[Article] = store.list_articles(theme_id=theme_id, day=shown)
    return render(
        request,
        "archive_day.html",
        {
            "day": shown,
            "themes": themes,
            "theme_id": theme_id,
            "articles": articles,
            "empty_text": EMPTY_ARTICLE_TEXT,
        },
    )


@router.get("/search")
def search(
    request: Request,
    store: StoreDep,
    q: str = "",
    theme: str | None = None,
    page: str | None = None,
) -> Response:
    themes = store.list_themes()
    theme_id = _theme_filter(themes, theme)
    query = q.strip()[:200]
    hits = []
    pager = Page(1, RESULTS_PER_PAGE, 0)
    if query:
        pager = Page(
            parse_page(page), RESULTS_PER_PAGE, store.count_search(query, theme_id=theme_id)
        )
        hits = store.search(query, theme_id=theme_id, limit=pager.size, offset=pager.offset)
    return render(
        request,
        "search.html",
        {"q": query, "themes": themes, "theme_id": theme_id, "hits": hits, "pager": pager},
    )
