"""End to end failure semantics on a real SQLite database: outages and interruptions are
recorded as errors, never saved as an empty day."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx

from veille.clock import FixedClock
from veille.collect.canonical import canonicalize
from veille.collect.extract import TrafilaturaExtractor
from veille.collect.net_safety import SafeHttp
from veille.collect.rss import RssCollector
from veille.collect.searxng import SearxngCollector
from veille.config import Settings
from veille.contracts import RawItem, RunStatus, RunTrigger, Source, Theme
from veille.db import open_store
from veille.llm.fake import FakeLLM
from veille.pipeline.run import run_pipeline

pytestmark = pytest.mark.e2e

NOW = datetime(2026, 11, 8, 4, 30, tzinfo=UTC)
FEED = "https://azure.example.com/feed.xml"


def _resolver(host: str, port: int) -> list[str]:
    return ["93.184.216.34"]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_path=tmp_path / "data" / "veille.db",
        searxng_url="http://searxng:8080",
    )


def test_total_outage_is_recorded_as_error(settings: Settings) -> None:
    with open_store(settings.database_path) as store:
        theme = store.create_theme("Azure Cloud", "", ["Azure"])
        store.add_source(theme.id, FEED, "Blog Azure")

    with respx.mock(assert_all_called=False) as router:
        router.get(FEED).respond(503)
        router.get(url__startswith="http://searxng:8080/search").mock(
            side_effect=httpx.ConnectError("refused")
        )
        client = httpx.Client()
        http = SafeHttp(settings, client=client, resolver=_resolver, sleep=lambda s: None)
        with open_store(settings.database_path) as store:
            result = run_pipeline(
                store=store,
                collectors=[
                    RssCollector(settings, http),
                    SearxngCollector(settings, client=client),
                ],
                extractor=TrafilaturaExtractor(settings, http),
                llm=FakeLLM(),
                clock=FixedClock(NOW),
                settings=settings,
                canonicalize=canonicalize,
                trigger=RunTrigger.CRON,
            )
            assert result.status is RunStatus.ERROR
            assert store.list_articles() == []
            last = store.last_run()
            assert last is not None and last.status is RunStatus.ERROR
        client.close()


class _Interrupting:
    name = "interrupting"

    def collect(self, theme: Theme, sources: list[Source]) -> list[RawItem]:
        raise KeyboardInterrupt  # what the worker's SIGTERM handler raises


def test_interrupted_run_is_finished_as_error(settings: Settings) -> None:
    with open_store(settings.database_path) as store:
        store.create_theme("Azure Cloud", "", ["Azure"])
        with pytest.raises(KeyboardInterrupt):
            run_pipeline(
                store=store,
                collectors=[_Interrupting()],
                extractor=TrafilaturaExtractor(settings, SafeHttp(settings)),
                llm=FakeLLM(),
                clock=FixedClock(NOW),
                settings=settings,
                canonicalize=canonicalize,
                trigger=RunTrigger.CLI,
            )
        last = store.last_run()
        assert last is not None and last.status is RunStatus.ERROR
        assert last.finished_at == NOW and "interrompue" in (last.error or "")
        # The lock is released: a new run can start right away.
        assert store.start_run(RunTrigger.CLI, NOW) is not None
