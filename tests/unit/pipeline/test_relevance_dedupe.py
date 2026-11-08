from dataclasses import replace

import pytest

from veille.contracts import Item, ItemStatus, Origin
from veille.llm import FakeLLM
from veille.pipeline.dedupe import Cluster, cluster_items, cluster_with_llm, select_top
from veille.pipeline.relevance import embedding_stage, keyword_stage, matches_keywords

from .fakes import NOW, make_theme


def item(
    title: str,
    *,
    url: str | None = None,
    source: str = "S",
    rel: float | None = None,
    snippet: str = "",
    source_id: int | None = None,
    content: str = "",
) -> Item:
    url = url or f"https://{source.lower()}.example/{title.replace(' ', '-')}"
    return Item(
        url,
        url,
        title,
        NOW,
        NOW,
        snippet,
        source,
        Origin.RSS,
        content,
        "h",
        source_id=source_id,
        relevance=rel,
    )


def test_keyword_match_accent_insensitive_word_boundary() -> None:
    assert matches_keywords("Les ÉLECTIONS régionales", ["elections"])
    assert matches_keywords("Une élection présidentielle", ["Élection Présidentielle"])
    assert not matches_keywords("Azurement bleu", ["azure"])
    assert matches_keywords("Azure's outage", ["azure"])
    assert not matches_keywords("rien", ["  "])


def test_keyword_stage_pinned_bypass_and_no_keywords() -> None:
    theme = make_theme(keywords=("élection",))
    items = [
        item("Election news"),
        item("Off topic", snippet="nothing"),
        item("Pinned off topic", source_id=3),
        item("Hidden", snippet="une ELECTION"),
    ]
    passed, dropped = keyword_stage(items, theme)
    assert [i.title for i in passed] == ["Election news", "Pinned off topic", "Hidden"]
    assert [(i.title, i.status) for i in dropped] == [("Off topic", ItemStatus.DROPPED)]
    passed, dropped = keyword_stage(items, make_theme(keywords=()))
    assert len(passed) == 4 and not dropped


def test_embedding_stage_threshold() -> None:
    theme = make_theme(name="Azure", description="cloud Microsoft", keywords=("azure",))
    llm = FakeLLM()
    items = [item("Azure cloud Microsoft panne"), item("Tarte aux pommes recette")]
    kept, dropped = embedding_stage(items, theme, llm, threshold=0.3)
    assert [i.title for i in kept] == ["Azure cloud Microsoft panne"]
    assert kept[0].relevance is not None and kept[0].embedding is not None
    assert dropped[0].status is ItemStatus.DROPPED and dropped[0].relevance < 0.3
    assert len(llm.embed_calls) == 1 and len(llm.embed_calls[0]) == 3
    assert embedding_stage([], theme, llm, threshold=0.3) == ([], [])


def test_clustering_groups_similar_titles_and_orders_by_relevance() -> None:
    llm = FakeLLM()
    items = [
        item("Recette tarte pommes", rel=0.5, source="C"),
        item("Microsoft annonce panne Azure Europe", rel=0.7, source="A"),
        item("Microsoft annonce panne Azure en Europe", rel=0.9, source="B"),
        item("Microsoft annonce une panne Azure Europe", rel=0.6, source="A"),
    ]
    clusters = cluster_with_llm(items, llm, threshold=0.88)
    assert [len(c.items) for c in clusters] == [3, 1]
    top = clusters[0]
    assert [i.relevance for i in top.items] == [0.9, 0.7, 0.6]
    assert top.relevance == 0.9
    links = top.source_links()
    assert [link.name for link in links] == ["B", "A"]  # one link per distinct source
    assert links[1].item_canonical_url == items[1].canonical_url
    assert cluster_with_llm([], llm, threshold=0.88) == []


def test_cluster_items_validation_and_best_group() -> None:
    with pytest.raises(ValueError):
        cluster_items([item("a")], [], threshold=0.5)
    items = [item("a", rel=0.9), item("b", rel=0.8), item("c", rel=0.7)]
    vectors = [[1.0, 0.0], [0.0, 1.0], [0.6, 0.8]]
    clusters = cluster_items(items, vectors, threshold=0.5)
    assert [[i.title for i in c.items] for c in clusters] == [["a"], ["b", "c"]]


def test_select_top() -> None:
    clusters = [Cluster((item(f"t{i}", rel=1 - i / 10),)) for i in range(5)]
    kept, dropped = select_top(clusters, 3)
    assert len(kept) == 3 and len(dropped) == 2
    assert all(i.status is ItemStatus.DROPPED for i in dropped)


def test_empty_source_name_link() -> None:
    c = Cluster((replace(item("x"), source_name=" "),))
    assert c.source_links()[0].name == "Source"


def test_links_deduped_per_outlet_host() -> None:
    rss = item("Panne", url="https://www.lemonde.fr/a", source="Le Monde", rel=0.9)
    searx = item("Panne bis", url="https://lemonde.fr/b", source="lemonde.fr", rel=0.8)
    other = item("Panne ter", url="https://zdnet.fr/c", source="ZDNet", rel=0.7)
    same_name = item("Panne 4", url="https://amp.zdnet.example/d", source="ZDNet", rel=0.6)
    links = Cluster((rss, searx, other, same_name)).source_links()
    assert [(link.name, link.url) for link in links] == [
        ("Le Monde", "https://www.lemonde.fr/a"),
        ("ZDNet", "https://zdnet.fr/c"),
    ]
