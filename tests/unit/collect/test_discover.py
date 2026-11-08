from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import respx

from veille.collect.discover import FeedDiscoverer, find_feed_links, rank_sites
from veille.collect.net_safety import SafeHttp
from veille.config import Settings
from veille.contracts import Theme

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>DevOps Blog</title>
<item><title>Pipelines news</title><link>https://devblogs.example.com/devops/a</link></item>
</channel></rss>"""
PAGE = b"""<html><head>
<link rel="alternate" type="application/rss+xml" href="/devops/feed/">
<link rel="alternate" type="application/rss+xml" href="/devops/a/comments/feed/">
</head><body>x</body></html>"""


def _theme() -> Theme:
    return Theme(1, "Azure DevOps", "", ("azure devops",), True, datetime(2026, 1, 1, tzinfo=UTC))


def test_find_feed_links_resolves_and_skips_comments() -> None:
    html = PAGE.decode() + '<link rel="stylesheet" href="x.css"><link rel="alternate" href="/x">'
    assert find_feed_links(html, "https://site.example.com/devops/a") == [
        "https://site.example.com/devops/feed/"
    ]


def test_find_feed_links_tolerates_bad_html() -> None:
    assert find_feed_links(
        "<link rel=alternate type='application/atom+xml' href=a.xml><<", "https://h.example/"
    ) == ["https://h.example/a.xml"]


def test_rank_sites_by_frequency_then_order() -> None:
    urls = [
        "https://a.example/1",
        "https://b.example/1",
        "https://b.example/2",
        "ftp://x/y",
        "https://c.example/",
    ]
    assert rank_sites(urls, 2) == [
        ("b.example", "https://b.example/1"),
        ("a.example", "https://a.example/1"),
    ]


def _discoverer(settings: Settings, router: respx.MockRouter, **kw: Any) -> FeedDiscoverer:
    client = httpx.Client()
    http = SafeHttp(
        settings, client=client, resolver=lambda h, p: ["93.184.216.34"], sleep=lambda s: None
    )
    return FeedDiscoverer(settings, http, client=client, **kw)


def test_discover_finds_advertised_feed(settings: Settings) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith=f"{settings.searxng_url}/search").respond(
            200,
            json={
                "results": [
                    {"url": "https://devblogs.example.com/devops/a"},
                    {"url": "https://other.example/x"},
                ]
            },
        )
        router.get("https://devblogs.example.com/devops/a").respond(
            200, content=PAGE, headers={"content-type": "text/html"}
        )
        router.get("https://devblogs.example.com/devops/feed/").respond(
            200, content=RSS, headers={"content-type": "application/rss+xml"}
        )
        router.get("https://other.example/feed").respond(
            200, content=RSS, headers={"content-type": "application/rss+xml"}
        )
        router.route().respond(404)
        found = _discoverer(settings, router).discover(
            _theme(), exclude=["https://other.example/feed"]
        )
    assert [c.url for c in found] == ["https://devblogs.example.com/devops/feed/"]
    assert found[0].title == "DevOps Blog"
    assert found[0].latest_title == "Pipelines news"
    assert found[0].site == "devblogs.example.com"


def test_discover_falls_back_to_common_paths_and_respects_budget(settings: Settings) -> None:
    ticks = iter(
        [0.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 999.0] + [999.0] * 50
    )
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith=f"{settings.searxng_url}/search").respond(
            200, json={"results": [{"url": "https://a.example/p"}, {"url": "https://b.example/p"}]}
        )
        router.get("https://a.example/rss").respond(
            200, content=RSS, headers={"content-type": "application/xml"}
        )
        router.route().respond(404)
        found = _discoverer(settings, router, budget_s=50, clock=lambda: next(ticks)).discover(
            _theme()
        )
    assert [c.url for c in found] == ["https://a.example/rss"]


def test_discover_survives_search_failure(settings: Settings) -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(url__startswith=f"{settings.searxng_url}/search").mock(
            side_effect=httpx.ConnectError("down")
        )
        d = _discoverer(settings, router)
        assert d.discover(_theme()) == []
        d.close()
