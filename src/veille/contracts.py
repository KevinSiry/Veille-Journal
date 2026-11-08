"""Shared contracts between packages (db, collect, llm, pipeline, web).

This module is the single integration surface. Changing it requires the
orchestrator's approval: report the need instead of editing it.

Conventions:
- Every ``datetime`` is timezone-aware and in UTC. Conversion to Europe/Paris only
  happens for the ``day`` key (``datetime.date``) and for display.
- ``day`` is the local calendar date in ``settings.timezone`` (Europe/Paris).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

# --------------------------------------------------------------------------- enums


class Origin(StrEnum):
    RSS = "rss"
    SEARXNG = "searxng"


class ItemStatus(StrEnum):
    NEW = "new"
    DROPPED = "dropped"
    SELECTED = "selected"
    USED = "used"


class ArticleStatus(StrEnum):
    OK = "ok"
    EMPTY = "empty"
    ERROR = "error"


class RunTrigger(StrEnum):
    CRON = "cron"
    CLI = "cli"


class RunStatus(StrEnum):
    RUNNING = "running"
    OK = "ok"
    PARTIAL = "partial"
    ERROR = "error"


# FTS5 snippet() highlight markers returned by Store.search(). The web layer must
# HTML-escape the snippet first, then replace these markers with <mark>/</mark>.
HIGHLIGHT_START = "\x02"
HIGHLIGHT_END = "\x03"

EMPTY_ARTICLE_TEXT = "Rien de notable aujourd'hui."

# ---------------------------------------------------------------------- exceptions


class VeilleError(Exception):
    """Base class for all domain errors."""


class UnsafeURLError(VeilleError):
    """URL rejected by the SSRF guard (private/loopback/link-local, bad scheme...)."""


class CollectError(VeilleError):
    """A collector or extractor failed (network, parsing, timeout)."""


class LLMError(VeilleError):
    """LLM call failed (timeout, HTTP error, invalid JSON after retries)."""


class LLMUnavailableError(LLMError):
    """Ollama is unreachable or busy."""


class ThemeConflictError(VeilleError):
    """Theme name already exists (UNIQUE violation)."""


# --------------------------------------------------------------------- dataclasses


@dataclass(frozen=True, slots=True)
class Theme:
    id: int
    name: str
    description: str
    keywords: tuple[str, ...]
    active: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Source:
    id: int
    theme_id: int
    url: str
    label: str
    active: bool
    kind: Literal["rss"] = "rss"


@dataclass(frozen=True, slots=True)
class RawItem:
    """An item as returned by a collector, before enrichment."""

    url: str
    title: str
    published_at: datetime | None
    snippet: str
    source_name: str
    origin: Origin
    # Set when the item comes from a pinned RSS source of the theme (relevance stage 1).
    source_id: int | None = None


@dataclass(frozen=True, slots=True)
class Item:
    """A RawItem enriched by the pipeline. ``canonical_url`` is the identity key per theme."""

    url: str
    canonical_url: str
    title: str
    published_at: datetime | None
    fetched_at: datetime
    snippet: str
    source_name: str
    origin: Origin
    content_text: str  # already truncated to <= 3000 chars
    content_hash: str  # sha256 hex of content_text
    source_id: int | None = None
    embedding: tuple[float, ...] | None = None
    relevance: float | None = None
    status: ItemStatus = ItemStatus.NEW


@dataclass(frozen=True, slots=True)
class SourceLink:
    name: str
    url: str
    # canonical_url of the Item this link comes from (maps to entry_sources.item_id).
    item_canonical_url: str


@dataclass(frozen=True, slots=True)
class Entry:
    headline_fr: str
    summary_fr: str
    source_links: tuple[SourceLink, ...]


@dataclass(frozen=True, slots=True)
class ArticleDraft:
    theme_id: int
    day: date
    title: str
    body_md: str
    model: str
    generated_at: datetime
    status: ArticleStatus
    entries: tuple[Entry, ...] = ()

    @property
    def entry_count(self) -> int:
        return len(self.entries)


@dataclass(frozen=True, slots=True)
class Article:
    """An article as read back from the database."""

    id: int
    theme_id: int
    theme_name: str
    day: date
    title: str
    body_md: str
    model: str
    generated_at: datetime
    entry_count: int
    status: ArticleStatus


@dataclass(frozen=True, slots=True)
class SearchHit:
    article: Article
    snippet: str  # raw text with HIGHLIGHT_START/HIGHLIGHT_END markers, NOT escaped
    rank: float  # bm25, lower is better


@dataclass(frozen=True, slots=True)
class ThemeStats:
    theme_id: int
    theme_name: str
    collected: int = 0
    new: int = 0  # after excluding already-known canonical URLs
    clusters: int = 0  # after near-duplicate grouping
    kept: int = 0  # entries in the final article
    dropped: int = 0
    llm_calls: int = 0
    tokens_estimated: int = 0
    duration_s: float = 0.0
    status: Literal["ok", "empty", "error", "skipped"] = "ok"
    error: str | None = None


@dataclass(frozen=True, slots=True)
class RunStats:
    themes: tuple[ThemeStats, ...] = ()
    duration_s: float = 0.0

    def to_json_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)


@dataclass(frozen=True, slots=True)
class Run:
    id: int
    started_at: datetime
    finished_at: datetime | None
    trigger: RunTrigger
    status: RunStatus
    stats: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ExtractedPage:
    final_url: str  # after redirects
    text: str  # extracted main text (may be empty)


# ----------------------------------------------------------------------- protocols


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """Current time, timezone-aware UTC."""
        ...


@runtime_checkable
class Collector(Protocol):
    """Collects raw items for a theme. ``sources`` are the theme's active RSS sources.

    Must never raise for a single bad feed/query: log and skip. May raise CollectError
    only for a total failure.
    """

    name: str

    def collect(self, theme: Theme, sources: Sequence[Source]) -> list[RawItem]: ...


@runtime_checkable
class ContentExtractor(Protocol):
    """Downloads a page (SSRF-guarded, 15 s timeout, 2 MB max) and extracts main text."""

    def extract(self, url: str) -> ExtractedPage | None: ...


@runtime_checkable
class LLM(Protocol):
    model: str  # generation model name, stored in articles.model

    def chat_json(self, system: str, user: str, schema: Mapping[str, Any]) -> dict[str, Any]:
        """One chat completion constrained by a JSON schema. Raises LLMError."""
        ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embeddings, one vector per input text, same order. Raises LLMError."""
        ...


@runtime_checkable
class Store(Protocol):
    """Persistence API implemented by ``veille.db`` (SqliteStore)."""

    # themes
    def list_themes(self, *, active_only: bool = False) -> list[Theme]: ...
    def get_theme(self, theme_id: int) -> Theme | None: ...
    def create_theme(self, name: str, description: str, keywords: Sequence[str]) -> Theme:
        """Raises ThemeConflictError on duplicate name."""
        ...

    def update_theme(
        self,
        theme_id: int,
        *,
        name: str,
        description: str,
        keywords: Sequence[str],
        active: bool,
    ) -> Theme: ...
    def delete_theme(self, theme_id: int) -> None:
        """Deletes the theme and everything attached to it (sources, items, articles)."""
        ...

    # sources
    def list_sources(self, theme_id: int, *, active_only: bool = False) -> list[Source]: ...
    def add_source(self, theme_id: int, url: str, label: str) -> Source: ...
    def delete_source(self, source_id: int) -> None: ...

    # items
    def known_canonical_urls(
        self, theme_id: int, urls: Iterable[str], *, before: datetime | None = None
    ) -> set[str]:
        """Subset of ``urls`` already stored for this theme.

        If ``before`` is given, only items with fetched_at < before count as known
        (used by ``--force`` re-runs so today's items are reconsidered).
        """
        ...

    def purge_old_content(self, older_than: datetime) -> int:
        """Set content_text to '' for items fetched before ``older_than``. Returns rows."""
        ...

    # articles
    def get_article(self, theme_id: int, day: date) -> Article | None: ...
    def save_article(self, draft: ArticleDraft, items: Sequence[Item], *, force: bool) -> bool:
        """Persist article + entries + entry_sources + items (upsert on
        (theme_id, canonical_url), with their status) in ONE transaction.

        Returns False (and writes nothing) if an article exists for (theme_id, day)
        and ``force`` is False. With ``force``, the existing article and its entries
        are replaced. Every SourceLink.item_canonical_url must be in ``items``.
        """
        ...

    def list_articles_for_day(self, day: date) -> list[Article]: ...
    def list_days(
        self, *, theme_id: int | None = None, limit: int = 30, offset: int = 0
    ) -> list[date]:
        """Distinct days having at least one article, newest first."""
        ...

    def count_days(self, *, theme_id: int | None = None) -> int: ...
    def list_articles(
        self, *, theme_id: int | None = None, day: date | None = None
    ) -> list[Article]: ...
    def adjacent_days(self, day: date) -> tuple[date | None, date | None]:
        """(previous day with articles, next day with articles) around ``day``."""
        ...

    def search(
        self, query: str, *, theme_id: int | None = None, limit: int = 20, offset: int = 0
    ) -> list[SearchHit]:
        """FTS5 search over title + body_md, ordered by rank then day desc.

        ``query`` is user input: the implementation must make it safe for FTS5 MATCH
        (quote terms) and must never raise on odd input (returns [] instead).
        """
        ...

    def count_search(self, query: str, *, theme_id: int | None = None) -> int: ...

    # runs
    def start_run(self, trigger: RunTrigger, now: datetime) -> int | None:
        """Insert a 'running' run and return its id, or None if another run is
        'running' and started less than 3 hours ago (acts as the run lock)."""
        ...

    def finish_run(
        self,
        run_id: int,
        *,
        status: RunStatus,
        stats: RunStats,
        finished_at: datetime,
        error: str | None = None,
    ) -> None: ...
    def last_run(self) -> Run | None: ...
    def list_runs(self, limit: int = 20) -> list[Run]: ...

    def ping(self) -> bool:
        """True if the database answers ``SELECT 1``."""
        ...

    def close(self) -> None: ...
