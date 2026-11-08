from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
import respx

from veille.collect.net_safety import SafeHttp
from veille.collect.searxng import SearxngCollector, parse_published, theme_queries
from veille.config import Settings
from veille.contracts import CollectError, Collector, Origin, Theme

from .conftest import fixture_bytes

SEARCH = "http://searxng:8080/search"


def theme(keywords: tuple[str, ...] = ("intelligence artificielle",), name: str = "IA") -> Theme:
    return Theme(
        id=2,
        name=name,
        description="",
        keywords=keywords,
        active=True,
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )


@pytest.fixture
def collector(settings: Settings, mock_router: respx.MockRouter) -> SearxngCollector:
    return SearxngCollector(settings, client=httpx.Client(timeout=settings.http_timeout_s))


def test_is_collector(collector: SearxngCollector) -> None:
    assert isinstance(collector, Collector)
    assert collector.name == "searxng"


def test_params_and_mapping(collector: SearxngCollector, mock_router: respx.MockRouter) -> None:
    route = mock_router.get(SEARCH).respond(200, content=fixture_bytes("searxng.json"))
    items = collector.collect(theme(), [])
    params = dict(route.calls.last.request.url.params)
    assert params == {
        "q": "intelligence artificielle",
        "format": "json",
        "categories": "news",
        "language": "all",
    }
    assert [i.url for i in items] == [
        "https://www.lemonde.fr/tech/article/2024/10/08/ia.html",
        "https://example.net/aware",
        "https://example.org/nodate",
        "https://example.com/baddate",
    ]
    first = items[0]
    assert first.title == 'L\'IA en "France"'
    assert first.snippet == "Plan annoncé"
    assert first.source_name == "lemonde.fr"
    assert first.origin is Origin.SEARXNG
    assert first.source_id is None
    assert first.published_at == datetime(2024, 10, 8, 9, 0, tzinfo=UTC)
    assert items[1].published_at == datetime(2024, 10, 8, 7, 0, tzinfo=UTC)
    assert items[2].published_at is None and items[2].snippet == ""
    assert items[3].published_at is None


def test_theme_name_when_no_keywords(
    collector: SearxngCollector, mock_router: respx.MockRouter
) -> None:
    route = mock_router.get(SEARCH).respond(200, json={"results": []})
    assert collector.collect(theme(keywords=(), name="  Climat  "), []) == []
    assert route.calls.last.request.url.params["q"] == "Climat"


def test_keyword_cap_and_dedup() -> None:
    kws = ("a", "A", " b ", "", "c", "d", "e", "f", "g")
    assert theme_queries(theme(keywords=kws), 5) == ["a", "b", "c", "d", "e"]


def test_keyword_cap_five_queries(
    collector: SearxngCollector, mock_router: respx.MockRouter
) -> None:
    route = mock_router.get(SEARCH).respond(200, json={"results": []})
    collector.collect(theme(keywords=tuple(f"k{i}" for i in range(8))), [])
    assert route.call_count == 5
    assert [c.request.url.params["q"] for c in route.calls] == ["k0", "k1", "k2", "k3", "k4"]


def test_results_cap_ten(collector: SearxngCollector, mock_router: respx.MockRouter) -> None:
    results = [{"url": f"https://ex.example/{i}", "title": f"T{i}"} for i in range(25)]
    mock_router.get(SEARCH).respond(200, json={"results": results})
    items = collector.collect(theme(), [])
    assert [i.url for i in items] == [f"https://ex.example/{i}" for i in range(10)]


def test_dedup_across_queries(collector: SearxngCollector, mock_router: respx.MockRouter) -> None:
    mock_router.get(SEARCH).respond(200, json={"results": [{"url": "https://x.example/a"}]})
    items = collector.collect(theme(keywords=("a", "b")), [])
    assert len(items) == 1 and items[0].title == "https://x.example/a"


def test_per_query_errors_tolerated(
    collector: SearxngCollector, mock_router: respx.MockRouter
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        q = request.url.params["q"]
        if q == "timeout":
            raise httpx.ReadTimeout("slow")
        if q == "http500":
            return httpx.Response(500)
        if q == "badjson":
            return httpx.Response(200, content=b"<html>")
        if q == "shape":
            return httpx.Response(200, json=["not", "a", "dict"])
        if q == "down":
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"results": [{"url": "https://ok.example/1"}]})

    mock_router.get(SEARCH).mock(side_effect=handler)
    collector._settings = collector._settings.model_copy(update={"max_keywords_queries": 6})
    items = collector.collect(
        theme(keywords=("timeout", "http500", "badjson", "shape", "down", "ok")), []
    )
    assert [i.url for i in items] == ["https://ok.example/1"]


def test_has_work(collector: SearxngCollector) -> None:
    assert collector.has_work(theme(), [])
    assert not collector.has_work(theme(keywords=(), name=" "), [])


def test_total_failure_raises(collector: SearxngCollector, mock_router: respx.MockRouter) -> None:
    mock_router.get(SEARCH).mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(CollectError):
        collector.collect(theme(keywords=("a", "b")), [])


def test_internal_searxng_not_ssrf_guarded(
    settings: Settings, mock_router: respx.MockRouter
) -> None:
    """searxng:8080 is a private docker host: it must be queried anyway."""
    s = settings.model_copy(update={"searxng_url": "http://10.0.0.2:8080/"})
    route = mock_router.get("http://10.0.0.2:8080/search").respond(200, json={"results": []})
    with SearxngCollector(s) as c:
        assert c.collect(theme(), []) == []
    assert route.called
    assert route.calls.last.request.headers["user-agent"] == s.user_agent


def test_resolves_shorteners_with_safe_http(
    settings: Settings, http: SafeHttp, mock_router: respx.MockRouter
) -> None:
    mock_router.get(SEARCH).respond(
        200, content=json.dumps({"results": [{"url": "https://bit.ly/z", "title": "t"}]})
    )
    mock_router.head("https://bit.ly/z").respond(302, headers={"Location": "https://www.dest.fr/a"})
    mock_router.head("https://www.dest.fr/a").respond(200)
    c = SearxngCollector(settings, client=httpx.Client(), http=http)
    items = c.collect(theme(), [])
    assert items[0].url == "https://www.dest.fr/a"
    assert items[0].source_name == "dest.fr"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        (123, None),
        ("garbage", None),
        ("2024-10-08T09:00:00", datetime(2024, 10, 8, 9, tzinfo=UTC)),
        ("2024-10-08 09:00:00", datetime(2024, 10, 8, 9, tzinfo=UTC)),
        ("2024-10-08T09:00:00Z", datetime(2024, 10, 8, 9, tzinfo=UTC)),
        ("2024-10-08T11:00:00+02:00", datetime(2024, 10, 8, 9, tzinfo=UTC)),
    ],
)
def test_parse_published(value: object, expected: datetime | None) -> None:
    result = parse_published(value)
    assert result == expected
    if result is not None:
        assert result.tzinfo is UTC


def test_time_range_is_opt_in(settings: Settings) -> None:
    with_range = SearxngCollector(settings.model_copy(update={"searxng_time_range": "week"}))
    assert with_range.build_params("x")["time_range"] == "week"
    assert "time_range" not in SearxngCollector(settings).build_params("x")
