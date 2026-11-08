"""Tiny HTML-to-plain-text helper (no external dependency)."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

_WS = re.compile(r"\s+")
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "head", "title"})
_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "br",
        "li",
        "ul",
        "ol",
        "tr",
        "td",
        "th",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "blockquote",
        "section",
        "article",
        "figure",
        "figcaption",
        "hr",
    }
)


class _TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(value: str | None) -> str:
    """Drop tags (and script/style content), unescape entities, collapse whitespace."""
    if not value:
        return ""
    if "<" not in value and "&" not in value:
        return _WS.sub(" ", value).strip()
    parser = _TextParser()
    try:
        parser.feed(value)
        parser.close()
        text = "".join(parser.parts)
    except Exception:  # pragma: no cover - HTMLParser is very lenient
        text = html.unescape(re.sub(r"<[^>]*>", " ", value))
    return _WS.sub(" ", text).strip()
