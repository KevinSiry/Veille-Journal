"""Automatic RSS feed discovery for a theme.

Strategy (no LLM: a model would invent URLs):
1. query SearXNG with the theme keywords and rank the sites that come back most often;
2. for each site, look for feeds advertised in the HTML (``<link rel="alternate"
   type="application/rss+xml">``) on the result page and on the home page, then try a few
   common paths (``/feed``, ``/rss``…);
3. keep only URLs that really are parsable feeds (fetched through the SSRF guard).
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from veille.collect.canonical import strip_www
from veille.collect.net_safety import SafeHttp
from veille.collect.rss import FEED_ACCEPT, MAX_FEED_BYTES, is_feed, parse_feed
from veille.collect.searxng import theme_queries
from veille.collect.text import html_to_text
from veille.config import Settings
from veille.contracts import CollectError, FeedCandidate, Theme, UnsafeURLError

logger = logging.getLogger(__name__)

FEED_TYPES = frozenset(
    {"application/rss+xml", "application/atom+xml", "application/feed+xml", "text/xml"}
)
COMMON_PATHS = ("/feed", "/rss", "/feed.xml", "/rss.xml", "/atom.xml", "/index.xml")
MAX_PAGE_BYTES = 1024 * 1024
MAX_LINKS_PER_PAGE = 5


class _AlternateLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "link":
            return
        values = {k.lower(): (v or "") for k, v in attrs}
        rels = values.get("rel", "").lower().split()
        kind = values.get("type", "").lower().split(";")[0].strip()
        if "alternate" in rels and kind in FEED_TYPES and values.get("href"):
            self.hrefs.append(values["href"].strip())


def find_feed_links(html: str, base_url: str) -> list[str]:
    """Absolute http(s) URLs of the feeds advertised by an HTML page, in page order."""
    parser = _AlternateLinkParser()
    try:
        parser.feed(html)
    except Exception:  # malformed HTML: keep what was parsed
        logger.debug("HTML parsing stopped early for %s", base_url, exc_info=True)
    links: list[str] = []
    for href in parser.hrefs:
        url = urljoin(base_url, href)
        parts = urlsplit(url)
        if "comments" in parts.path.lower():  # per-article comment feeds are noise
            continue
        if parts.scheme in ("http", "https") and url not in links:
            links.append(url)
    return links[:MAX_LINKS_PER_PAGE]


def rank_sites(urls: Iterable[str], limit: int) -> list[tuple[str, str]]:
    """(site, first result URL) for the ``limit`` hosts seen most often, ties by first seen."""
    counts: Counter[str] = Counter()
    first: dict[str, str] = {}
    for url in urls:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            continue
        host = parts.hostname.lower()
        counts[host] += 1
        first.setdefault(host, url)
    order = list(first)
    ranked = sorted(order, key=lambda h: (-counts[h], order.index(h)))
    return [(host, first[host]) for host in ranked[:limit]]


class FeedDiscoverer:
    """Finds candidate feeds for a theme. Callable from the web layer."""

    def __init__(
        self,
        settings: Settings,
        http: SafeHttp,
        *,
        client: httpx.Client | None = None,
        max_sites: int = 10,
        max_feeds: int = 10,
        budget_s: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._http = http
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=settings.http_timeout_s, headers={"User-Agent": settings.user_agent}
        )
        self._max_sites = max_sites
        self._max_feeds = max_feeds
        self._budget_s = budget_s
        self._clock = clock

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # ------------------------------------------------------------------ search

    def _search_urls(self, theme: Theme) -> list[str]:
        endpoint = f"{self._settings.searxng_url.rstrip('/')}/search"
        urls: list[str] = []
        for query in theme_queries(theme, self._settings.max_keywords_queries):
            for category in ("news", "general"):
                try:
                    response = self._client.get(
                        endpoint,
                        params={"q": query, "format": "json", "categories": category},
                    )
                    response.raise_for_status()
                    results = response.json().get("results", [])
                except (httpx.HTTPError, ValueError, AttributeError) as exc:
                    logger.warning("feed discovery: search %r failed: %s", query, exc)
                    continue
                urls += [str(r["url"]) for r in results if isinstance(r, dict) and r.get("url")]
        return urls

    # ------------------------------------------------------------------- fetch

    def _get(self, url: str, *, max_bytes: int, accept: str) -> tuple[str, bytes, str] | None:
        try:
            response = self._http.get(url, max_bytes=max_bytes, accept=accept)
        except (CollectError, UnsafeURLError) as exc:
            logger.debug("feed discovery: %s skipped: %s", url, exc)
            return None
        if not response.ok:
            return None
        return response.final_url, response.content, response.headers.get("content-type", "")

    def inspect_feed(self, url: str) -> FeedCandidate | None:
        """A FeedCandidate if ``url`` is a reachable, parsable feed with entries."""
        fetched = self._get(url, max_bytes=MAX_FEED_BYTES, accept=FEED_ACCEPT)
        if fetched is None:
            return None
        final_url, content, content_type = fetched
        parsed = parse_feed(content, content_type)
        if not is_feed(parsed) or not parsed.get("entries"):
            return None
        site = strip_www(urlsplit(final_url).hostname or "")
        title = html_to_text(parsed.get("feed", {}).get("title")) or site
        latest = html_to_text(parsed["entries"][0].get("title")) or None
        return FeedCandidate(url=url, title=title[:200], site=site, latest_title=latest)

    def _page_links(self, url: str) -> list[str]:
        fetched = self._get(url, max_bytes=MAX_PAGE_BYTES, accept="text/html")
        if fetched is None:
            return []
        final_url, content, content_type = fetched
        if "html" not in content_type.lower():
            return []
        return find_feed_links(content.decode("utf-8", errors="replace"), final_url)

    def _site_candidates(self, host: str, result_url: str) -> Iterable[str]:
        yield from self._page_links(result_url)
        home = f"https://{host}/"
        yield from self._page_links(home)
        for path in COMMON_PATHS:
            yield f"https://{host}{path}"

    # -------------------------------------------------------------------- main

    def discover(self, theme: Theme, exclude: Sequence[str] = ()) -> list[FeedCandidate]:
        deadline = self._clock() + self._budget_s
        excluded = set(exclude)
        found: list[FeedCandidate] = []
        sites = rank_sites(self._search_urls(theme), self._max_sites)
        logger.info("feed discovery for %r: %d candidate sites", theme.name, len(sites))
        for host, result_url in sites:
            if len(found) >= self._max_feeds or self._clock() > deadline:
                break
            tried: set[str] = set()
            for url in self._site_candidates(host, result_url):
                if self._clock() > deadline:
                    break
                if url in tried or url in excluded:
                    continue
                tried.add(url)
                candidate = self.inspect_feed(url)
                if candidate is not None:
                    found.append(candidate)
                    excluded.add(url)
                    break  # one feed per site is enough
        return found
