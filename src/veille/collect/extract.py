"""Main-text extraction of article pages with trafilatura."""

from __future__ import annotations

import codecs
import logging

import trafilatura

from veille.collect.net_safety import SafeHttp
from veille.config import Settings
from veille.contracts import ExtractedPage, VeilleError

logger = logging.getLogger(__name__)

HTML_ACCEPT = "text/html, application/xhtml+xml;q=0.9, */*;q=0.1"
HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
MAX_TEXT_CHARS = 20_000


def _decode(content: bytes, charset: str | None) -> str:
    if charset:
        try:
            codecs.lookup(charset)
            return content.decode(charset, errors="replace")
        except LookupError:
            pass
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError:
        return content.decode("cp1252", errors="replace")


class TrafilaturaExtractor:
    """SSRF-guarded download + trafilatura extraction. Never raises."""

    def __init__(self, settings: Settings, http: SafeHttp) -> None:
        self._settings = settings
        self._http = http

    def extract(self, url: str) -> ExtractedPage | None:
        try:
            response = self._http.get(
                url, max_bytes=self._settings.max_page_bytes, accept=HTML_ACCEPT
            )
        except VeilleError as exc:
            logger.info("extract %s: fetch failed: %s", url, exc)
            return None
        if not response.ok:
            logger.info("extract %s: HTTP %s", url, response.status_code)
            return None
        if response.content_type not in HTML_TYPES:
            logger.info("extract %s: not HTML (%s)", url, response.content_type or "none")
            return None
        try:
            html = _decode(response.content, response.charset)
            text = trafilatura.extract(
                html,
                url=response.final_url,
                include_comments=False,
                include_tables=False,
                favor_precision=True,
            )
        except Exception:
            logger.warning("extract %s: trafilatura failed", url, exc_info=True)
            return None
        return ExtractedPage(final_url=response.final_url, text=(text or "")[:MAX_TEXT_CHARS])
