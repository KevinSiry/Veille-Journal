"""SearXNG (self-hosted, JSON API) collector.

``settings.searxng_url`` is trusted configuration (typically the internal docker host
``http://searxng:8080``), so it is queried with a plain httpx client, NOT through the
SSRF guard. Only the result URLs (user-facing, untrusted) go through SafeHttp when
shorteners are resolved.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlsplit

import httpx

from veille.collect.canonical import host_key, resolve_short
from veille.collect.net_safety import SafeHttp
from veille.collect.text import html_to_text
from veille.config import Settings
from veille.contracts import CollectError, Origin, RawItem, Source, Theme

logger = logging.getLogger(__name__)

MAX_SNIPPET_CHARS = 2000


def parse_published(value: object) -> datetime | None:
    """Parse SearXNG ``publishedDate`` (ISO 8601, may be null/naive) to aware UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def host_name(url: str) -> str:
    return host_key(url)


def theme_queries(theme: Theme, limit: int) -> list[str]:
    """Up to ``limit`` distinct keywords (case-insensitive), else the theme name."""
    queries: list[str] = []
    seen: set[str] = set()
    for keyword in theme.keywords:
        cleaned = " ".join(keyword.split())
        if cleaned and cleaned.casefold() not in seen:
            seen.add(cleaned.casefold())
            queries.append(cleaned)
        if len(queries) >= limit:
            break
    if not queries and theme.name.strip():
        queries.append(" ".join(theme.name.split()))
    return queries


class SearxngCollector:
    """Queries SearXNG's news category for each theme keyword."""

    name = "searxng"

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
        http: SafeHttp | None = None,
    ) -> None:
        self._settings = settings
        self._http = http
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(settings.http_timeout_s),
            headers={"User-Agent": settings.user_agent, "Accept": "application/json"},
            trust_env=False,
        )
        self._endpoint = settings.searxng_url.rstrip("/") + "/search"

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @staticmethod
    def build_params(query: str) -> dict[str, str]:
        return {
            "q": query,
            "format": "json",
            "categories": "news",
            "time_range": "day",
            "language": "all",
        }

    def has_work(self, theme: Theme, sources: Sequence[Source]) -> bool:
        return bool(theme_queries(theme, self._settings.max_keywords_queries))

    def collect(self, theme: Theme, sources: Sequence[Source]) -> list[RawItem]:
        """Run the theme queries sequentially.

        A failing query is logged and skipped; CollectError is raised only when every
        query failed (SearXNG down).
        """
        queries = theme_queries(theme, self._settings.max_keywords_queries)
        items: list[RawItem] = []
        seen: set[str] = set()
        failures = 0
        for query in queries:
            try:
                results = self._query(query)
            except CollectError as exc:
                failures += 1
                logger.warning("theme %s: searxng query %r failed: %s", theme.id, query, exc)
                continue
            for result in results:
                item = self._to_item(result)
                if item is not None and item.url not in seen:
                    seen.add(item.url)
                    items.append(item)
        if queries and failures == len(queries):
            raise CollectError(f"all {failures} SearXNG queries failed")
        return items

    def _query(self, query: str) -> list[dict[str, Any]]:
        try:
            response = self._client.get(self._endpoint, params=self.build_params(query))
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException as exc:
            raise CollectError("timeout") from exc
        except httpx.HTTPError as exc:
            raise CollectError(str(exc)) from exc
        except ValueError as exc:
            raise CollectError("invalid JSON") from exc
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            raise CollectError("unexpected JSON shape")
        valid = [r for r in results if isinstance(r, dict) and self._usable_url(r.get("url"))]
        return valid[: self._settings.searxng_results_per_query]

    @staticmethod
    def _usable_url(value: object) -> bool:
        return (
            isinstance(value, str)
            and urlsplit(value).scheme in ("http", "https")
            and bool(host_name(value))
        )

    def _to_item(self, result: dict[str, Any]) -> RawItem | None:
        url = str(result["url"]).strip()
        if self._http is not None:
            url = resolve_short(url, self._http)
        title = html_to_text(str(result.get("title") or "")) or url
        snippet = html_to_text(str(result.get("content") or ""))[:MAX_SNIPPET_CHARS]
        return RawItem(
            url=url,
            title=title,
            published_at=parse_published(result.get("publishedDate")),
            snippet=snippet,
            source_name=host_name(url),
            origin=Origin.SEARXNG,
        )
