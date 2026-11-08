from __future__ import annotations

import logging
from datetime import UTC, datetime

import pytest
import respx

from veille.collect.net_safety import SafeHttp
from veille.collect.rss import (
    MSG_HTTP_ERROR,
    MSG_INVALID,
    MSG_NO_ANSWER,
    MSG_NOT_FOUND,
    MSG_TIMEOUT,
    MSG_TOO_LARGE,
    MSG_UNSAFE,
    RssCollector,
    validate_feed,
)
from veille.config import Settings
from veille.contracts import CollectError, Collector, Origin, Source, Theme, UnsafeURLError

from .conftest import fixture_bytes

THEME = Theme(
    id=1,
    name="Tech",
    description="",
    keywords=("python",),
    active=True,
    created_at=datetime(2024, 1, 1, tzinfo=UTC),
)
RSS_URL = "https://journal.example.com/feed.xml"
ATOM_URL = "https://atom.example.org/atom"


def source(sid: int, url: str, label: str = "", active: bool = True) -> Source:
    return Source(id=sid, theme_id=1, url=url, label=label, active=active)


@pytest.fixture
def feeds(mock_router: respx.MockRouter) -> respx.MockRouter:
    mock_router.get(RSS_URL).respond(
        200, content=fixture_bytes("rss2.xml"), headers={"Content-Type": "application/rss+xml"}
    )
    mock_router.get(ATOM_URL).respond(
        200, content=fixture_bytes("atom.xml"), headers={"Content-Type": "application/atom+xml"}
    )
    mock_router.get("https://broken.example/feed").respond(
        200, content=fixture_bytes("broken.xml"), headers={"Content-Type": "text/html"}
    )
    mock_router.head("https://t.co/abc123").respond(
        301, headers={"Location": "https://dest.example/story"}
    )
    mock_router.head("https://dest.example/story").respond(200)
    return mock_router


def test_is_collector(settings: Settings, http: SafeHttp) -> None:
    collector = RssCollector(settings, http)
    assert isinstance(collector, Collector)
    assert collector.name == "rss"


def test_collect_rss2(settings: Settings, http: SafeHttp, feeds: respx.MockRouter) -> None:
    items = RssCollector(settings, http).collect(THEME, [source(7, RSS_URL)])
    assert len(items) == 3  # the entry without link is skipped
    first, relative, short = items
    assert first.url == "https://journal.example.com/articles/python-313?utm_source=rss"
    assert first.title == "Sortie de Python 3.13"
    assert (
        first.snippet == "La nouvelle version améliore les performances. Deuxième paragraphe & fin."
    )
    assert first.published_at == datetime(2024, 10, 8, 12, 30, tzinfo=UTC)
    assert first.published_at.tzinfo is UTC
    assert first.origin is Origin.RSS
    assert first.source_id == 7
    assert first.source_name == "Le Journal Tech"
    assert relative.url == "https://journal.example.com/articles/relatif"
    assert relative.published_at is None
    assert relative.snippet == "Texte simple"
    assert short.url == "https://dest.example/story"
    assert short.published_at == datetime(2024, 10, 9, 8, 0, tzinfo=UTC)


def test_collect_atom_with_label(
    settings: Settings, http: SafeHttp, feeds: respx.MockRouter
) -> None:
    items = RssCollector(settings, http).collect(THEME, [source(3, ATOM_URL, label=" Mon blog ")])
    assert len(items) == 1
    item = items[0]
    assert item.url == "https://atom.example.org/2024/10/entree"
    assert item.title == "Entrée Atom"
    assert item.snippet == "Contenu riche"
    assert item.published_at == datetime(2024, 10, 8, 10, 15, tzinfo=UTC)
    assert item.source_name == "Mon blog"


def test_bad_feeds_skipped(
    settings: Settings,
    http: SafeHttp,
    feeds: respx.MockRouter,
    caplog: pytest.LogCaptureFixture,
) -> None:
    feeds.get("https://gone.example/feed").respond(500)
    sources = [
        source(1, "https://broken.example/feed"),
        source(2, "http://127.0.0.1/feed"),
        source(3, "https://gone.example/feed"),
        source(4, ATOM_URL),
        source(5, RSS_URL, active=False),
    ]
    with caplog.at_level(logging.WARNING):
        items = RssCollector(settings, http).collect(THEME, sources)
    assert [i.source_id for i in items] == [4]
    assert caplog.text.count("skipped") == 3


def test_unexpected_error_is_logged(
    settings: Settings, http: SafeHttp, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("bug")

    monkeypatch.setattr(http, "get", boom)
    with pytest.raises(CollectError, match="all 1 RSS feeds failed"):
        RssCollector(settings, http).collect(THEME, [source(1, RSS_URL)])


def test_no_active_source_means_no_work_not_failure(settings: Settings, http: SafeHttp) -> None:
    collector = RssCollector(settings, http)
    inactive = [source(1, RSS_URL, active=False)]
    assert collector.collect(THEME, []) == []
    assert collector.collect(THEME, inactive) == []
    assert not collector.has_work(THEME, []) and not collector.has_work(THEME, inactive)
    assert collector.has_work(THEME, [source(1, RSS_URL)])


def test_every_feed_failing_raises(
    settings: Settings, http: SafeHttp, feeds: respx.MockRouter
) -> None:
    feeds.get("https://gone.example/feed").respond(500)
    sources = [source(1, "https://broken.example/feed"), source(2, "https://gone.example/feed")]
    with pytest.raises(CollectError, match="all 2 RSS feeds failed"):
        RssCollector(settings, http).collect(THEME, sources)


def test_feed_title_fallback_to_host(
    settings: Settings, http: SafeHttp, mock_router: respx.MockRouter
) -> None:
    feed = b"""<?xml version="1.0"?><rss version="2.0"><channel>
    <item><title>T</title><link>https://www.notitle.example/a</link></item>
    <item><title></title><link>javascript:alert(1)</link></item>
    <item><link>https://www.notitle.example/b</link></item>
    </channel></rss>"""
    mock_router.get("https://www.notitle.example/rss").respond(200, content=feed)
    items = RssCollector(settings, http).collect(
        THEME, [source(1, "https://www.notitle.example/rss")]
    )
    assert [i.source_name for i in items] == ["notitle.example", "notitle.example"]
    assert items[1].title == "https://www.notitle.example/b"


# ------------------------------------------------------------------ validate_feed


def test_validate_feed_returns_title(
    settings: Settings, http: SafeHttp, feeds: respx.MockRouter
) -> None:
    assert validate_feed(RSS_URL, settings, http) == "Le Journal Tech"
    assert validate_feed(ATOM_URL, settings, http) == "Blog Atom"


def test_validate_feed_title_fallback(
    settings: Settings, http: SafeHttp, mock_router: respx.MockRouter
) -> None:
    mock_router.get("https://www.anon.example/rss").respond(
        200, content=b'<rss version="2.0"><channel><item><title>x</title></item></channel></rss>'
    )
    assert validate_feed("https://www.anon.example/rss", settings, http) == "anon.example"


@pytest.mark.parametrize(
    ("url", "setup", "exc", "message"),
    [
        ("http://192.168.0.1/rss", None, UnsafeURLError, MSG_UNSAFE),
        ("file:///etc/passwd", None, UnsafeURLError, MSG_UNSAFE),
        ("https://nxdomain.example.com/rss", None, CollectError, MSG_NOT_FOUND),
        ("https://broken.example/feed", None, CollectError, MSG_INVALID),
        ("https://e.example/timeout", "timeout", CollectError, MSG_TIMEOUT),
        ("https://e.example/down", "down", CollectError, MSG_NO_ANSWER),
        ("https://e.example/big", "big", CollectError, MSG_TOO_LARGE),
        ("https://e.example/404", "404", CollectError, MSG_HTTP_ERROR.format(status=404)),
    ],
)
def test_validate_feed_errors(
    settings: Settings,
    http: SafeHttp,
    feeds: respx.MockRouter,
    url: str,
    setup: str | None,
    exc: type[Exception],
    message: str,
) -> None:
    import httpx

    if setup == "timeout":
        feeds.get(url).mock(side_effect=httpx.TimeoutException("t"))
    elif setup == "down":
        feeds.get(url).mock(side_effect=httpx.ConnectError("c"))
    elif setup == "big":
        feeds.get(url).respond(200, headers={"Content-Length": str(6 * 1024 * 1024)}, content=b"")
    elif setup == "404":
        feeds.get(url).respond(404)
    with pytest.raises(exc) as info:
        validate_feed(url, settings, http)
    assert str(info.value) == message
    assert type(info.value) is exc


def test_validate_feed_builds_own_client(settings: Settings) -> None:
    with pytest.raises(UnsafeURLError, match=MSG_UNSAFE):
        validate_feed("http://127.0.0.1/rss", settings)
