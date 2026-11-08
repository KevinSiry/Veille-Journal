"""Safe rendering helpers: Markdown articles, search snippets, French dates."""

from __future__ import annotations

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


def render_markdown(body_md: str) -> Markup:
    """Render article Markdown with raw HTML disabled; the output is safe to embed.

    The leading ``## title`` line of the stored body is dropped: pages show the title
    themselves.
    """
    if body_md.startswith("## "):
        body_md = body_md.split("\n", 1)[1] if "\n" in body_md else ""
    return Markup(_MD.render(body_md))  # noqa: S704 - markdown-it escapes, html disabled


def highlight_snippet(snippet: str) -> Markup:
    """Escape the FTS snippet FIRST, then turn the highlight markers into <mark>."""
    escaped = str(escape(snippet))
    escaped = escaped.replace(HIGHLIGHT_START, "<mark>").replace(HIGHLIGHT_END, "</mark>")
    return Markup(escaped)  # noqa: S704 - built from escaped text


def fr_date(value: date) -> str:
    return f"{_WEEKDAYS[value.weekday()]} {value.day} {_MONTHS[value.month - 1]} {value.year}"


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
    }
