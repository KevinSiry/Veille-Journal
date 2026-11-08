from datetime import timedelta

from veille.contracts import ExtractedPage, ItemStatus
from veille.pipeline.prepare import content_hash, prepare_items

from .fakes import NOW, FakeExtractor, FakeStore, identity_canonicalize, raw

LONG = "x" * 450


def run(raws, *, store=None, extractor=None, known_before=None, canonicalize=identity_canonicalize):
    return prepare_items(
        raws,
        theme_id=1,
        store=store or FakeStore(),
        extractor=extractor or FakeExtractor(),
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


def test_extractor_only_for_short_snippets_and_truncation() -> None:
    page_text = "p" * 5000
    extractor = FakeExtractor({"https://a/short": ExtractedPage("https://a/short", page_text)})
    items = run(
        [
            raw("https://a/short", "short", snippet="tiny"),
            raw("https://a/long", "long", snippet=LONG),
        ],
        extractor=extractor,
    )
    assert extractor.calls == ["https://a/short"]
    short, long_ = items
    assert short.content_text == "p" * 3000
    assert short.content_hash == content_hash("p" * 3000)
    assert long_.content_text == LONG


def test_extractor_shorter_text_keeps_snippet_and_errors_ignored() -> None:
    extractor = FakeExtractor({"https://a/1": ExtractedPage("https://a/1", "x")})
    assert (
        run([raw("https://a/1", "t", snippet="snippet")], extractor=extractor)[0].content_text
        == "snippet"
    )
    failing = FakeExtractor(error=RuntimeError("boom"))
    assert (
        run([raw("https://a/1", "t", snippet="snip")], extractor=failing)[0].content_text == "snip"
    )


def test_redirect_recanonicalized_and_deduped() -> None:
    extractor = FakeExtractor(
        {
            "https://news.example/r1": ExtractedPage("https://site/article", "body " * 100),
            "https://news.example/r2": ExtractedPage("https://site/article/", "body"),
            "https://news.example/r3": ExtractedPage("https://site/known", "body"),
            "https://news.example/r4": ExtractedPage("https://site/direct", "body"),
        }
    )
    store = FakeStore(known={1: {"https://site/known"}})
    items = run(
        [
            raw("https://news.example/r1", "a"),
            raw("https://news.example/r2", "a bis"),
            raw("https://news.example/r3", "known"),
            raw("https://news.example/r4", "direct"),
            raw("https://site/direct", "direct twin"),
        ],
        store=store,
        extractor=extractor,
    )
    assert [(i.title, i.canonical_url, i.url) for i in items] == [
        ("a", "https://site/article", "https://site/article"),
        ("direct twin", "https://site/direct", "https://site/direct"),
    ]
