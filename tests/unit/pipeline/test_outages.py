"""Outage semantics with the REAL collectors: an outage is a theme error, never an empty
"Rien de notable aujourd'hui." article."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx

from veille.clock import FixedClock
from veille.collect.net_safety import SafeHttp
from veille.collect.rss import RssCollector
from veille.collect.searxng import SearxngCollector
from veille.config import Settings
from veille.contracts import RunStatus, RunTrigger, Source
from veille.llm import FakeLLM
from veille.pipeline.run import run_pipeline

from .fakes import NOW, FakeExtractor, FakeStore, identity_canonicalize, make_theme

SEARX = "http://searxng.test:8080"
FEED = "https://feeds.example/rss.xml"


def _public(host: str, port: int) -> list[str]:
    return ["93.184.216.34"]


@pytest.fixture
def cfg() -> Settings:
    return Settings(_env_file=None, searxng_url=SEARX, relevance_threshold=0.1)  # type: ignore[call-arg]


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as mock:
        yield mock


@pytest.fixture
def http(cfg: Settings, router: respx.MockRouter) -> Iterator[SafeHttp]:
    client = httpx.Client()
    yield SafeHttp(cfg, client=client, resolver=_public, sleep=lambda s: None)
    client.close()


def _run(store: FakeStore, cfg: Settings, http: SafeHttp) -> RunStatus:
    searx_client = httpx.Client()
    try:
        result = run_pipeline(
            store=store,
            collectors=[
                RssCollector(cfg, http),
                SearxngCollector(cfg, client=searx_client, http=http),
            ],
            extractor=FakeExtractor(),
            llm=FakeLLM(),
            clock=FixedClock(NOW),
            settings=cfg,
            canonicalize=identity_canonicalize,
            trigger=RunTrigger.CRON,
        )
    finally:
        searx_client.close()
    return result.status


def test_searxng_down_and_no_rss_source_is_an_error(
    cfg: Settings, http: SafeHttp, router: respx.MockRouter
) -> None:
    router.get(f"{SEARX}/search").mock(side_effect=httpx.ConnectError("refused"))
    store = FakeStore(themes=[make_theme()])
    assert _run(store, cfg, http) is RunStatus.ERROR
    assert not store.saved
    assert store.runs[1]["status"] is RunStatus.ERROR
    assert "searxng" in str(store.runs[1]["error"])


def test_searxng_down_and_every_feed_failing_is_an_error(
    cfg: Settings, http: SafeHttp, router: respx.MockRouter
) -> None:
    router.get(f"{SEARX}/search").respond(502)
    router.get(FEED).mock(side_effect=httpx.ConnectError("refused"))
    store = FakeStore(
        themes=[make_theme()],
        sources={1: [Source(id=1, theme_id=1, url=FEED, label="Flux", active=True)]},
    )
    assert _run(store, cfg, http) is RunStatus.ERROR
    assert not store.saved
    error = str(store.runs[1]["error"])
    assert "rss" in error and "searxng" in error


def test_searxng_up_with_no_results_is_a_quiet_day(
    cfg: Settings, http: SafeHttp, router: respx.MockRouter
) -> None:
    router.get(f"{SEARX}/search").respond(200, json={"results": []})
    store = FakeStore(themes=[make_theme()])
    assert _run(store, cfg, http) is RunStatus.OK
    assert store.article_for(1).draft.entry_count == 0
