"""Step 2: relevance filtering.

Stage 1 (cheap): a theme keyword appears in title + snippet (accent- and
case-insensitive, on word boundaries), or the item comes from a pinned RSS source.
A theme without keywords lets every item through stage 1 (embeddings decide).

Stage 2: cosine similarity between the theme embedding (name + description +
keywords) and the item embedding (title + first 300 characters of content) must be
>= ``threshold``. Items keep their embedding and relevance score.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from veille.contracts import LLM, Item, ItemStatus, Theme
from veille.pipeline.text import cosine, normalize_words

ITEM_EMBED_CONTENT_CHARS = 300


def theme_text(theme: Theme) -> str:
    return " ".join(p for p in (theme.name, theme.description, " ".join(theme.keywords)) if p)


def item_text(item: Item) -> str:
    return f"{item.title}\n{item.content_text[:ITEM_EMBED_CONTENT_CHARS]}"


def matches_keywords(text: str, keywords: Sequence[str]) -> bool:
    haystack = normalize_words(text)
    for keyword in keywords:
        needle = normalize_words(keyword)
        if needle.strip() and needle in haystack:
            return True
    return False


def keyword_stage(items: Sequence[Item], theme: Theme) -> tuple[list[Item], list[Item]]:
    """Return (passed, dropped); dropped items get status DROPPED."""
    keywords = [k for k in theme.keywords if k.strip()]
    passed: list[Item] = []
    dropped: list[Item] = []
    for item in items:
        if (
            not keywords
            or item.source_id is not None
            or matches_keywords(f"{item.title} {item.snippet}", keywords)
        ):
            passed.append(item)
        else:
            dropped.append(replace(item, status=ItemStatus.DROPPED))
    return passed, dropped


def embedding_stage(
    items: Sequence[Item], theme: Theme, llm: LLM, *, threshold: float
) -> tuple[list[Item], list[Item]]:
    """Embed theme + items in one batch; return (kept, dropped) with relevance set."""
    if not items:
        return [], []
    vectors = llm.embed([theme_text(theme), *(item_text(i) for i in items)])
    theme_vec, item_vecs = vectors[0], vectors[1:]
    kept: list[Item] = []
    dropped: list[Item] = []
    for item, vec in zip(items, item_vecs, strict=True):
        score = cosine(theme_vec, vec)
        scored = replace(item, embedding=tuple(vec), relevance=score)
        if score >= threshold:
            kept.append(scored)
        else:
            dropped.append(replace(scored, status=ItemStatus.DROPPED))
    return kept, dropped
