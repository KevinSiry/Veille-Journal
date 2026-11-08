"""Step 1: turn collected ``RawItem``s into new, enriched ``Item``s for one theme.

Window filter -> canonical URL -> in-batch dedupe -> exclude already-known URLs ->
content (snippet or extracted page) -> truncation + content hash.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from veille.contracts import ContentExtractor, ExtractedPage, Item, RawItem, Store

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
    extractor: ContentExtractor,
    canonicalize: Callable[[str], str],
    now: datetime,
    lookback_hours: int,
    content_max_chars: int,
    known_before: datetime | None = None,
) -> list[Item]:
    """Return the new items of the batch, in collection order, status NEW."""
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
    candidates = [c for c in candidates if c.canonical_url not in known]
    batch_urls = {c.canonical_url for c in candidates}

    items: list[Item] = []
    taken: set[str] = set()
    for cand in candidates:
        raw = cand.raw
        url, canonical, content = cand.url, cand.canonical_url, raw.snippet.strip()
        if len(content) < MIN_SNIPPET_CHARS:
            page = _extract(extractor, raw.url)
            if page is not None:
                if len(page.text.strip()) > len(content):
                    content = page.text.strip()
                if page.final_url and page.final_url != raw.url:
                    new_canonical = _safe_canonical(canonicalize, page.final_url)
                    if new_canonical is not None and new_canonical != canonical:
                        if new_canonical in batch_urls or new_canonical in taken:
                            continue  # redirect target already in this batch
                        if store.known_canonical_urls(
                            theme_id, [new_canonical], before=known_before
                        ):
                            continue  # redirect target already known
                        canonical = new_canonical
                    url = page.final_url
        if canonical in taken:
            continue
        taken.add(canonical)
        text = content[:content_max_chars]
        items.append(
            Item(
                url=url,
                canonical_url=canonical,
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
