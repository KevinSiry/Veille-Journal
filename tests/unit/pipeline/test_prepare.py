from datetime import timedelta

from veille.contracts import ExtractedPage, ItemStatus
from veille.pipeline.dedupe import Cluster
from veille.pipeline.prepare import content_hash, enrich_clusters, enrich_item, prepare_items

from .fakes import NOW, FakeExtractor, FakeStore, identity_canonicalize, raw

LONG = "x" * 450


def run(raws, *, store=None, known_before=None, canonicalize=identity_canonicalize):
    return prepare_items(
        raws,
        theme_id=1,
        store=store or FakeStore(),
        canonicalize=canonicalize,
        now=NOW,
        lookback_hours=36,
        content_max_chars=3000,
        known_before=known_before,
    )


def test_window_filter_36h() -> None:
    items = run(
        [
            raw("https://a/1", "in", published_at=NOW - timedelta(hours=35), snippet=LONG),
            raw("https://a/2", "old", published_at=NOW - timedelta(hours=37), snippet=LONG),
            raw("https://a/3", "nodate", published_at=None, snippet=LONG),
        ]
    )
    assert [i.title for i in items] == ["in", "nodate"]
    assert all(i.fetched_at == NOW and i.status is ItemStatus.NEW for i in items)


def test_canonical_dedupe_and_known_exclusion() -> None:
    store = FakeStore(known={1: {"https://a/known"}})
    before = NOW - timedelta(hours=5)
    items = run(
        [
            raw("https://a/1?utm=x", "one", snippet=LONG),
            raw("https://A/1", "dup", snippet=LONG),
            raw("https://a/known/", "known", snippet=LONG),
        ],
        store=store,
        known_before=before,
    )
    assert [(i.title, i.canonical_url, i.url) for i in items] == [
        ("one", "https://a/1", "https://a/1?utm=x")
    ]
    assert store.known_calls == [(1, ["https://a/1", "https://a/known"], before)]


def test_bad_url_skipped() -> None:
    def canon(url: str) -> str:
        if "bad" in url:
            raise ValueError("bad")
        return url

    assert [i.url for i in run([raw("bad", "b"), raw("https://ok", "ok")], canonicalize=canon)] == [
        "https://ok"
    ]


def test_prepare_is_metadata_only_content_is_snippet() -> None:
    items = run([raw("https://a/1", "t", snippet="  " + "s" * 3500)])
    assert items[0].content_text == "s" * 3000
    assert items[0].content_hash == content_hash("s" * 3000)


def enrich(item, extractor):  # type: ignore[no-untyped-def]
    return enrich_item(item, extractor, content_max_chars=3000)


def test_extractor_only_for_short_snippets_and_truncation() -> None:
    page_text = "p" * 5000
    extractor = FakeExtractor({"https://a/short": ExtractedPage("https://a/short", page_text)})
    short, long_ = run(
        [
            raw("https://a/short", "short", snippet="tiny"),
            raw("https://a/long", "long", snippet=LONG),
        ]
    )
    short, long_ = enrich(short, extractor), enrich(long_, extractor)
    assert extractor.calls == ["https://a/short"]
    assert short.content_text == "p" * 3000
    assert short.content_hash == content_hash("p" * 3000)
    assert long_.content_text == LONG


def test_extractor_shorter_text_keeps_snippet_and_errors_ignored() -> None:
    extractor = FakeExtractor({"https://a/1": ExtractedPage("https://a/1", "x")})
    (item,) = run([raw("https://a/1", "t", snippet="snippet")])
    assert enrich(item, extractor).content_text == "snippet"
    failing = FakeExtractor(error=RuntimeError("boom"))
    assert enrich(item, failing) == item
    assert enrich(item, FakeExtractor()) == item  # extractor returned None


def test_redirect_keeps_canonical_identity_and_links_final_url() -> None:
    extractor = FakeExtractor(
        {"https://news.example/r1": ExtractedPage("https://site/article", "body " * 100)}
    )
    (item,) = run([raw("https://news.example/r1", "a")])
    enriched = enrich(item, extractor)
    assert enriched.canonical_url == "https://news.example/r1"  # identity unchanged
    assert enriched.url == "https://site/article"


def test_enrich_clusters_only_touches_primaries() -> None:
    a1, a2, b1 = run(
        [
            raw("https://a/1", "a1", snippet="court"),
            raw("https://a/2", "a2", snippet="court"),
            raw("https://b/1", "b1", snippet="court"),
        ]
    )
    extractor = FakeExtractor({"https://a/1": ExtractedPage("https://a/1", "page " * 200)})
    out = enrich_clusters([Cluster((a1, a2)), Cluster((b1,))], extractor, content_max_chars=50)
    assert extractor.calls == ["https://a/1", "https://b/1"]
    assert out[0].items[1] is a2 and len(out[0].primary.content_text) == 50
    assert out[1].primary == b1
