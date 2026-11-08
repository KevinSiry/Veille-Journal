from __future__ import annotations

import pytest
import respx

from veille.collect.canonical import (
    SHORTENER_HOSTS,
    canonicalize,
    host_key,
    is_shortened,
    resolve_short,
    strip_www,
)
from veille.collect.net_safety import SafeHttp
from veille.collect.rss import _host
from veille.collect.searxng import host_name
from veille.collect.text import html_to_text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://Example.COM/Path", "https://example.com/Path"),
        ("https://www.example.com/a/", "https://example.com/a"),
        ("https://example.com", "https://example.com/"),
        ("https://example.com/", "https://example.com/"),
        ("https://example.com/a#section", "https://example.com/a"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("http://example.com:80/a", "https://example.com/a"),
        ("https://example.com:8443/a", "https://example.com:8443/a"),
        (
            "https://example.com/a?utm_source=x&utm_Medium=y&b=2&a=1",
            "https://example.com/a?a=1&b=2",
        ),
        (
            "https://example.com/a?fbclid=1&gclid=2&mc_cid=3&mc_eid=4&ref_src=5&igshid=6",
            "https://example.com/a",
        ),
        ("https://example.com/a?b=2&a=1&a=0", "https://example.com/a?a=0&a=1&b=2"),
        ("https://example.com/a?empty=", "https://example.com/a?empty="),
        ("https://user:pw@example.com/a", "https://example.com/a"),
        ("https://bücher.de/x", "https://xn--bcher-kva.de/x"),
        ("https://www.EXAMPLE.com./a//", "https://example.com/a"),
        ("  https://example.com/a  ", "https://example.com/a"),
        ("https://[2001:DB8::1]:8080/a", "https://[2001:db8::1]:8080/a"),
        ("https://example.com/?utm_source=x", "https://example.com/"),
        ("ftp://example.com/a", "ftp://example.com/a"),
        ("not a url", "not a url"),
        ("https://example.com:bad/", "https://example.com:bad/"),
    ],
)
def test_canonicalize(raw: str, expected: str) -> None:
    assert canonicalize(raw) == expected


def test_canonicalize_idempotent() -> None:
    url = "http://www.Example.com/a/?z=1&utm_campaign=c&a=2#f"
    once = canonicalize(url)
    assert canonicalize(once) == once


def test_single_www_helper_shared() -> None:
    assert strip_www("WWW.LeMonde.fr") == "lemonde.fr"
    assert strip_www("wwwx.example") == "wwwx.example"
    for fn in (host_key, host_name, _host):
        assert fn("https://www.lemonde.fr/x") == "lemonde.fr"
    assert host_key("not a url") == "" and host_key("http://[::1") == ""
    assert _host("not a url") == "not a url"
    assert canonicalize("https://WWW.LeMonde.fr/a") == "https://lemonde.fr/a"


def test_shortener_hosts() -> None:
    assert {"t.co", "bit.ly", "lnkd.in", "dlvr.it"} <= SHORTENER_HOSTS
    assert is_shortened("https://bit.ly/x")
    assert is_shortened("https://www.bit.ly/x")
    assert not is_shortened("https://news.google.com/rss/articles/abc")
    assert not is_shortened("http://[bad")


def test_resolve_short_follows_redirects(http: SafeHttp, mock_router: respx.MockRouter) -> None:
    mock_router.head("https://bit.ly/abc").respond(
        301, headers={"Location": "https://news.example/article"}
    )
    mock_router.head("https://news.example/article").respond(403)
    assert resolve_short("https://bit.ly/abc", http) == "https://news.example/article"


def test_resolve_short_falls_back_to_get(http: SafeHttp, mock_router: respx.MockRouter) -> None:
    mock_router.head("https://t.co/x").respond(405)
    mock_router.get("https://t.co/x").respond(302, headers={"Location": "https://dest.example/"})
    mock_router.get("https://dest.example/").respond(200, content=b"big page")
    assert resolve_short("https://t.co/x", http) == "https://dest.example/"


def test_resolve_short_failure_returns_input(http: SafeHttp, mock_router: respx.MockRouter) -> None:
    mock_router.head("https://bit.ly/priv").respond(302, headers={"Location": "http://10.0.0.1/"})
    assert resolve_short("https://bit.ly/priv", http) == "https://bit.ly/priv"
    mock_router.head("https://bit.ly/dead").respond(404)
    mock_router.get("https://bit.ly/dead").respond(404)
    assert resolve_short("https://bit.ly/dead", http) == "https://bit.ly/dead"


def test_resolve_short_ignores_other_hosts(http: SafeHttp, mock_router: respx.MockRouter) -> None:
    route = mock_router.route().respond(200)
    url = "https://news.google.com/rss/articles/CBMi?oc=5"
    assert resolve_short(url, http) == url
    assert not route.called


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        (None, ""),
        ("plain   text\n here", "plain text here"),
        ("<p>Hello <b>world</b></p><p>Next</p>", "Hello world Next"),
        ("A &amp; B &eacute;t&#233; &nbsp;x", "A & B été x"),
        ("<script>var a = '<p>';</script>Visible<style>p{}</style>", "Visible"),
        ("line<br>break", "line break"),
        ("&amp;lt;tag&amp;gt;", "&lt;tag&gt;"),
        ("1 < 2 and 3 > 2", "1 < 2 and 3 > 2"),
    ],
)
def test_html_to_text(raw: str | None, expected: str) -> None:
    assert html_to_text(raw) == expected
