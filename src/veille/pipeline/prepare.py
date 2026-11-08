"""Step 1: turn collected ``RawItem``s into new ``Item``s for one theme, and (after
top-N selection) enrich the few items that will be summarised.

``prepare_items`` is metadata only (no network): window filter -> canonical URL ->
in-batch dedupe -> exclude already-known URLs -> content = snippet (truncated + hash).

``enrich_clusters`` runs after ``select_top``: only the primary item of each selected
cluster whose snippet is shorter than ``MIN_SNIPPET_CHARS`` gets its page downloaded and
extracted, so at most ``max_items_per_theme`` pages are fetched per theme.

Identity after a redirect: the item keeps the canonical URL computed from the collected
URL (its identity key, already checked against the known URLs and the batch), only its
``url`` (the link shown) follows the final URL. A redirect target that is already known
is therefore not detected; this is accepted, re-checking would mean dropping an item that
already passed relevance and clustering.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from veille.contracts import ContentExtractor, ExtractedPage, Item, RawItem, Store
from veille.pipeline.dedupe import Cluster

logger = logging.getLogger(__name__)

MIN_SNIPPET_CHARS = 400


@dataclass(frozen=True, slots=True)
class _Candidate:
    raw: RawItem
    url: str
    canonical_url: str


def in_window(raw: RawItem, *, now: datetime, lookback_hours: int) -> bool:
    """Published within ``lookback_hours`` of ``now``. A missing date counts as "now"."""
    if raw.published_at is None:
        return True
    return raw.published_at >= now - timedelta(hours=lookback_hours)


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _safe_canonical(canonicalize: Callable[[str], str], url: str) -> str | None:
    try:
        return canonicalize(url)
    except Exception:  # an odd URL must not kill the theme
        logger.warning("Cannot canonicalize %r, item skipped", url, exc_info=True)
        return None


def _extract(extractor: ContentExtractor, url: str) -> ExtractedPage | None:
    try:
        return extractor.extract(url)
    except Exception:
        logger.info("Extraction failed for %s, using the snippet", url, exc_info=True)
        return None


def prepare_items(
    raw_items: Sequence[RawItem],
    *,
    theme_id: int,
    store: Store,
    canonicalize: Callable[[str], str],
    now: datetime,
    lookback_hours: int,
    content_max_chars: int,
    known_before: datetime | None = None,
) -> list[Item]:
    """Return the new items of the batch, in collection order, status NEW.

    ``content_text`` is the (truncated) snippet; see :func:`enrich_clusters`.
    """
    seen: set[str] = set()
    candidates: list[_Candidate] = []
    for raw in raw_items:
        if not in_window(raw, now=now, lookback_hours=lookback_hours):
            continue
        canonical = _safe_canonical(canonicalize, raw.url)
        if canonical is None or canonical in seen:
            continue
        seen.add(canonical)
        candidates.append(_Candidate(raw, raw.url, canonical))

    known = store.known_canonical_urls(
        theme_id, [c.canonical_url for c in candidates], before=known_before
    )
    items: list[Item] = []
    for cand in candidates:
        if cand.canonical_url in known:
            continue
        raw = cand.raw
        text = raw.snippet.strip()[:content_max_chars]
        items.append(
            Item(
                url=cand.url,
                canonical_url=cand.canonical_url,
                title=" ".join(raw.title.split()),
                published_at=raw.published_at,
                fetched_at=now,
                snippet=raw.snippet,
                source_name=raw.source_name,
                origin=raw.origin,
                content_text=text,
                content_hash=content_hash(text),
                source_id=raw.source_id,
            )
        )
    return items


def enrich_item(item: Item, extractor: ContentExtractor, *, content_max_chars: int) -> Item:
    """Replace a short snippet by the extracted page text (never raises)."""
    snippet = item.snippet.strip()
    if len(snippet) >= MIN_SNIPPET_CHARS:
        return item
    page = _extract(extractor, item.url)
    if page is None:
        return item
    content = snippet
    if len(page.text.strip()) > len(content):
        content = page.text.strip()
    text = content[:content_max_chars]
    url = page.final_url or item.url
    return replace(item, url=url, content_text=text, content_hash=content_hash(text))


def enrich_clusters(
    clusters: Sequence[Cluster], extractor: ContentExtractor, *, content_max_chars: int
) -> list[Cluster]:
    """Enrich the primary item of each (already selected) cluster."""
    enriched: list[Cluster] = []
    for cluster in clusters:
        primary = enrich_item(cluster.primary, extractor, content_max_chars=content_max_chars)
        enriched.append(Cluster((primary, *cluster.items[1:])))
    return enriched
