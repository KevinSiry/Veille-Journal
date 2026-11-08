"""Safe rendering helpers: Markdown articles, search snippets, French dates."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from markdown_it import MarkdownIt
from markdown_it.renderer import RendererHTML
from markdown_it.token import Token
from markdown_it.utils import EnvType, OptionsDict
from markupsafe import Markup, escape

from veille.contracts import HIGHLIGHT_END, HIGHLIGHT_START

ALLOWED_LINK_SCHEMES = ("http://", "https://")
LINK_REL = "noopener noreferrer nofollow"

_WEEKDAYS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
_MONTHS = (
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


def _validate_link(url: str) -> bool:
    return url.strip().lower().startswith(ALLOWED_LINK_SCHEMES)


def _render_link_open(
    self: RendererHTML,
    tokens: Sequence[Token],
    idx: int,
    options: OptionsDict,
    env: EnvType,
) -> str:
    token = tokens[idx]
    token.attrSet("rel", LINK_REL)
    token.attrSet("target", "_blank")
    return self.renderToken(tokens, idx, options, env)


def _build_markdown() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": False})
    md.validateLink = _validate_link  # type: ignore[method-assign]
    md.disable("image")  # remote images are blocked by the CSP anyway
    md.add_render_rule("link_open", _render_link_open)
    return md


_MD = _build_markdown()

# Generated entries render as ``<li><strong>headline</strong> — summary (<a>…</a> · …)</li>``.
# These patterns only add presentation hooks; markdown-it has already escaped every text
# part, so no ``<`` can occur inside the captured groups except the anchors themselves.
_ENTRY_HEAD_RE = re.compile(r"<li><strong>([^<]*)</strong> — ")
_ENTRY_SOURCES_RE = re.compile(r" \(((?:<a [^>]*>[^<]*</a>)(?: · <a [^>]*>[^<]*</a>)*)\)</li>")


def _entry_sources(match: re.Match[str]) -> str:
    links = match.group(1).replace("</a> · <a ", "</a><a ")
    return f'<span class="entry-sources">{links}</span></li>'


def _decorate_entries(html: str) -> str:
    html = _ENTRY_HEAD_RE.sub(r'<li><strong class="entry-title">\1</strong> ', html)
    return _ENTRY_SOURCES_RE.sub(_entry_sources, html)


def render_markdown(body_md: str) -> Markup:
    """Render article Markdown with raw HTML disabled; the output is safe to embed.

    The leading ``## title`` line of the stored body is dropped: pages show the title
    themselves.
    """
    if body_md.startswith("## "):
        body_md = body_md.split("\n", 1)[1] if "\n" in body_md else ""
    html = _decorate_entries(_MD.render(body_md))
    return Markup(html)  # noqa: S704 - markdown-it escapes, html disabled


def highlight_snippet(snippet: str) -> Markup:
    """Escape the FTS snippet FIRST, then turn the highlight markers into <mark>."""
    escaped = str(escape(snippet))
    escaped = escaped.replace(HIGHLIGHT_START, "<mark>").replace(HIGHLIGHT_END, "</mark>")
    return Markup(escaped)  # noqa: S704 - built from escaped text


def fr_date(value: date) -> str:
    return f"{_WEEKDAYS[value.weekday()]} {value.day} {_MONTHS[value.month - 1]} {value.year}"


def fr_weekday(value: date) -> str:
    return _WEEKDAYS[value.weekday()]


def fr_month_year(value: date) -> str:
    return f"{_MONTHS[value.month - 1]} {value.year}"


def group_by_month(days: Sequence[date]) -> list[list[date]]:
    """Split an ordered list of days into runs of the same calendar month."""
    groups: list[list[date]] = []
    for day in days:
        if groups and (groups[-1][0].year, groups[-1][0].month) == (day.year, day.month):
            groups[-1].append(day)
        else:
            groups.append([day])
    return groups


def fr_time(value: datetime | None, tz: ZoneInfo) -> str:
    if value is None:
        return "—"
    return value.astimezone(tz).strftime("%H:%M")


def fr_datetime(value: datetime | None, tz: ZoneInfo) -> str:
    if value is None:
        return "—"
    return value.astimezone(tz).strftime("%d/%m/%Y %H:%M")


def jinja_filters(tz: ZoneInfo) -> dict[str, Any]:
    return {
        "markdown": render_markdown,
        "highlight": highlight_snippet,
        "fr_date": fr_date,
        "fr_datetime": lambda value: fr_datetime(value, tz),
        "fr_time": lambda value: fr_time(value, tz),
        "fr_weekday": fr_weekday,
        "fr_month_year": fr_month_year,
        "group_by_month": group_by_month,
    }
