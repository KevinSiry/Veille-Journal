"""RSS/Atom collector and feed validation for the web UI."""

from __future__ import annotations

import calendar
import logging
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin, urlsplit

import feedparser

from veille.collect.canonical import host_key, resolve_short
from veille.collect.net_safety import (
    FetchTimeoutError,
    HostNotFoundError,
    ResponseTooLargeError,
    SafeHttp,
)
from veille.collect.text import html_to_text
from veille.config import Settings
from veille.contracts import (
    CollectError,
    Origin,
    RawItem,
    Source,
    Theme,
    UnsafeURLError,
    VeilleError,
)

logger = logging.getLogger(__name__)

MAX_FEED_BYTES = 5 * 1024 * 1024
MAX_SNIPPET_CHARS = 2000
FEED_ACCEPT = (
    "application/rss+xml, application/atom+xml, application/rdf+xml;q=0.9, "
    "application/xml;q=0.9, text/xml;q=0.9, */*;q=0.5"
)

# User-facing (French) messages for validate_feed.
MSG_UNSAFE = "Adresse non autorisée"
MSG_NO_ANSWER = "Le flux ne répond pas"
MSG_TIMEOUT = "Le flux ne répond pas (délai dépassé)"
MSG_NOT_FOUND = "Adresse introuvable (nom de domaine inconnu)"
MSG_TOO_LARGE = "Le flux est trop volumineux"
MSG_HTTP_ERROR = "Le flux a répondu avec une erreur (HTTP {status})"
MSG_INVALID = "Ce n'est pas un flux RSS/Atom valide"


def _parse(content: bytes, content_type: str | None) -> Any:
    headers = {"content-type": content_type} if content_type else {}
    return feedparser.parse(content, response_headers=headers)


def _is_feed(parsed: Any) -> bool:
    return bool(parsed.get("version")) or bool(parsed.get("entries"))


def _entry_datetime(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if isinstance(value, time.struct_time):
            try:
                return datetime.fromtimestamp(calendar.timegm(value), tz=UTC)
            except (OverflowError, ValueError, OSError):
                continue
    return None


def _entry_snippet(entry: Any) -> str:
    raw = entry.get("summary") or entry.get("description") or ""
    if not raw:
        content = entry.get("content") or []
        if content and isinstance(content[0], dict):
            raw = content[0].get("value", "")
    text = html_to_text(str(raw))
    return text[:MAX_SNIPPET_CHARS]


def _host(url: str) -> str:
    return host_key(url) or url.lower()


class RssCollector:
    """Fetches the theme's pinned RSS/Atom sources."""

    name = "rss"

    def __init__(self, settings: Settings, http: SafeHttp) -> None:
        self._settings = settings
        self._http = http

    def has_work(self, theme: Theme, sources: Sequence[Source]) -> bool:
        """False when the theme has no active feed: an empty result is then no signal."""
        return any(source.active for source in sources)

    def collect(self, theme: Theme, sources: Sequence[Source]) -> list[RawItem]:
        """Fetch every active feed; a bad feed is logged and skipped.

        Raises CollectError when there was at least one active feed and all failed.
        """
        items: list[RawItem] = []
        attempted = 0
        failures: list[str] = []
        for source in sources:
            if not source.active:
                continue
            attempted += 1
            try:
                items.extend(self._collect_source(source))
            except VeilleError as exc:
                logger.warning("theme %s: feed %s skipped: %s", theme.id, source.url, exc)
                failures.append(f"{source.url}: {exc}")
            except Exception as exc:
                logger.exception("theme %s: unexpected error on feed %s", theme.id, source.url)
                failures.append(f"{source.url}: {exc}")
        if attempted and len(failures) == attempted:
            raise CollectError(f"all {attempted} RSS feeds failed ({'; '.join(failures)})")
        return items

    def _collect_source(self, source: Source) -> list[RawItem]:
        response = self._http.get(source.url, max_bytes=MAX_FEED_BYTES, accept=FEED_ACCEPT)
        if not response.ok:
            raise CollectError(f"HTTP {response.status_code}")
        parsed = _parse(response.content, response.headers.get("content-type"))
        if not _is_feed(parsed):
            raise CollectError("not a RSS/Atom feed")
        feed_title = html_to_text(parsed.get("feed", {}).get("title"))
        source_name = source.label.strip() or feed_title or _host(response.final_url)
        items: list[RawItem] = []
        for entry in parsed.get("entries", []):
            link = str(entry.get("link") or "").strip()
            if not link:
                continue
            link = urljoin(response.final_url, link)
            if urlsplit(link).scheme not in ("http", "https"):
                continue
            title = html_to_text(entry.get("title")) or link
            items.append(
                RawItem(
                    url=resolve_short(link, self._http),
                    title=title,
                    published_at=_entry_datetime(entry),
                    snippet=_entry_snippet(entry),
                    source_name=source_name,
                    origin=Origin.RSS,
                    source_id=source.id,
                )
            )
        logger.info("feed %s: %d items", source.url, len(items))
        return items


def validate_feed(url: str, settings: Settings, http: SafeHttp | None = None) -> str:
    """Check that ``url`` is a reachable RSS/Atom feed and return its title.

    Raises UnsafeURLError or CollectError with a French, user-facing message.
    """
    owned = http is None
    client = http or SafeHttp(settings)
    try:
        response = client.get(url, max_bytes=MAX_FEED_BYTES, accept=FEED_ACCEPT)
    except UnsafeURLError as exc:
        raise UnsafeURLError(MSG_UNSAFE) from exc
    except FetchTimeoutError as exc:
        raise CollectError(MSG_TIMEOUT) from exc
    except HostNotFoundError as exc:
        raise CollectError(MSG_NOT_FOUND) from exc
    except ResponseTooLargeError as exc:
        raise CollectError(MSG_TOO_LARGE) from exc
    except CollectError as exc:
        raise CollectError(MSG_NO_ANSWER) from exc
    finally:
        if owned:
            client.close()
    if not response.ok:
        raise CollectError(MSG_HTTP_ERROR.format(status=response.status_code))
    parsed = _parse(response.content, response.headers.get("content-type"))
    if not _is_feed(parsed):
        raise CollectError(MSG_INVALID)
    title = html_to_text(parsed.get("feed", {}).get("title"))
    return title or _host(response.final_url)
