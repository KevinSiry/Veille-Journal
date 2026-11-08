"""Step 5: deterministic Markdown rendering of one theme's daily article.

Text coming from the LLM or from feeds (headline, summary, source names) is escaped so
it can never create links, emphasis or HTML. URLs come only from ``SourceLink``s (i.e.
from collected items), are restricted to http(s) and percent-encode characters that
could break out of the Markdown link destination.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime
from urllib.parse import urlsplit

from veille.contracts import EMPTY_ARTICLE_TEXT, ArticleDraft, ArticleStatus, Entry, Theme

FRENCH_WEEKDAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
FRENCH_MONTHS = (
    "janvier",
    "février",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "août",
    "septembre",
    "octobre",
    "novembre",
    "décembre",
)

_MD_SPECIAL_RE = re.compile(r"([\\`*_\[\]<>()#!|~])")
_URL_ESCAPES = {
    " ": "%20",
    "(": "%28",
    ")": "%29",
    "<": "%3C",
    ">": "%3E",
    "\\": "%5C",
    '"': "%22",
    "`": "%60",
}


def french_long_date(day: date) -> str:
    """``dimanche 8 novembre 2026`` (``1er`` for the first day of the month)."""
    number = "1er" if day.day == 1 else str(day.day)
    return f"{FRENCH_WEEKDAYS[day.weekday()]} {number} {FRENCH_MONTHS[day.month - 1]} {day.year}"


def article_title(theme_name: str, day: date) -> str:
    return f"{theme_name} — {french_long_date(day)}"


def escape_md(text: str) -> str:
    """Escape inline Markdown/HTML syntax; newlines collapse to spaces."""
    return _MD_SPECIAL_RE.sub(r"\\\1", " ".join(text.split()))


def safe_url(url: str) -> str | None:
    """http(s) URL safe to put in ``[text](url)``, or None."""
    url = url.strip()
    if any(ord(ch) < 32 for ch in url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return None
    return "".join(_URL_ESCAPES.get(ch, ch) for ch in url)


def render_entry(entry: Entry) -> str:
    links = []
    for link in entry.source_links:
        url = safe_url(link.url)
        if url is not None:
            links.append(f"[{escape_md(link.name) or 'Source'}]({url})")
    line = f"- **{escape_md(entry.headline_fr)}** — {escape_md(entry.summary_fr)}"
    if links:
        line += f" ({' · '.join(links)})"
    return line


def render_body(title: str, entries: Sequence[Entry]) -> str:
    heading = f"## {escape_md(title)}\n\n"
    if not entries:
        return f"{heading}{EMPTY_ARTICLE_TEXT}\n"
    return heading + "".join(f"{render_entry(e)}\n" for e in entries)


def render_article(
    theme: Theme, day: date, entries: Sequence[Entry], *, model: str, generated_at: datetime
) -> ArticleDraft:
    title = article_title(theme.name, day)
    return ArticleDraft(
        theme_id=theme.id,
        day=day,
        title=title,
        body_md=render_body(title, entries),
        model=model,
        generated_at=generated_at,
        status=ArticleStatus.OK if entries else ArticleStatus.EMPTY,
        entries=tuple(entries),
    )
