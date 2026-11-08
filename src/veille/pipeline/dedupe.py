"""Step 3: near-duplicate clustering and top-N selection.

Items are visited by decreasing relevance (ties: original order). Each item joins the
existing cluster holding its most similar member (title-embedding cosine >=
``threshold``, single linkage) or starts a new cluster. Deterministic for a given input.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from veille.contracts import LLM, Item, ItemStatus, SourceLink
from veille.pipeline.text import cosine, fold


@dataclass(frozen=True, slots=True)
class Cluster:
    """Items about the same event, ordered by decreasing relevance."""

    items: tuple[Item, ...]

    @property
    def primary(self) -> Item:
        return self.items[0]

    @property
    def relevance(self) -> float:
        return max((i.relevance or 0.0) for i in self.items)

    def linked_items(self) -> list[Item]:
        """One item per distinct source name (first = most relevant)."""
        seen: set[str] = set()
        linked: list[Item] = []
        for item in self.items:
            key = fold(item.source_name.strip()) or item.canonical_url
            if key in seen:
                continue
            seen.add(key)
            linked.append(item)
        return linked

    def source_links(self) -> tuple[SourceLink, ...]:
        return tuple(
            SourceLink(
                name=i.source_name.strip() or "Source",
                url=i.url,
                item_canonical_url=i.canonical_url,
            )
            for i in self.linked_items()
        )


def _sorted_by_relevance(items: Sequence[Item]) -> list[int]:
    return sorted(range(len(items)), key=lambda k: (-(items[k].relevance or 0.0), k))


def cluster_items(
    items: Sequence[Item], title_vectors: Sequence[Sequence[float]], *, threshold: float
) -> list[Cluster]:
    """Group ``items`` (aligned with ``title_vectors``); clusters sorted by relevance."""
    if len(items) != len(title_vectors):
        raise ValueError("items and title_vectors must have the same length")
    groups: list[list[int]] = []
    for idx in _sorted_by_relevance(items):
        best_group, best_score = -1, float("-inf")
        for g, members in enumerate(groups):
            score = max(cosine(title_vectors[idx], title_vectors[m]) for m in members)
            if score >= threshold and score > best_score:
                best_group, best_score = g, score
        if best_group == -1:
            groups.append([idx])
        else:
            groups[best_group].append(idx)
    clusters = [Cluster(tuple(items[m] for m in members)) for members in groups]
    # Groups are created in relevance order, so they are already sorted; keep it explicit.
    return sorted(clusters, key=lambda c: -c.relevance)


def cluster_with_llm(items: Sequence[Item], llm: LLM, *, threshold: float) -> list[Cluster]:
    if not items:
        return []
    return cluster_items(items, llm.embed([i.title for i in items]), threshold=threshold)


def select_top(clusters: Sequence[Cluster], limit: int) -> tuple[list[Cluster], list[Item]]:
    """Keep the ``limit`` most relevant clusters; items of the others become DROPPED."""
    kept = list(clusters[:limit])
    dropped = [replace(i, status=ItemStatus.DROPPED) for c in clusters[limit:] for i in c.items]
    return kept, dropped
