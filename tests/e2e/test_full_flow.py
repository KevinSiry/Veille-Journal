"""End to end: real SQLite + real collectors over mocked HTTP + FakeLLM -> run -> web pages."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest
import respx
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from pydantic import SecretStr

from veille.clock import FixedClock
from veille.collect.canonical import canonicalize
from veille.collect.extract import TrafilaturaExtractor
from veille.collect.net_safety import SafeHttp
from veille.collect.rss import RssCollector
from veille.collect.searxng import SearxngCollector
from veille.config import Settings
from veille.contracts import RunStatus, RunTrigger
from veille.db import open_store
from veille.llm.fake import FakeLLM
from veille.pipeline.run import run_pipeline
from veille.web.app import create_app

pytestmark = pytest.mark.e2e

NOW = datetime(2026, 11, 8, 4, 30, tzinfo=UTC)  # 05:30 Europe/Paris
PASSWORD = "e2e-password-123"
TITLE = "Microsoft annonce une panne Azure majeure en Europe de l'Ouest"
BODY = (
    "Microsoft a confirmé dimanche une panne de son service Azure dans la région Europe de "
    "l'Ouest. Selon l'entreprise, des machines virtuelles et des bases de données ont été "
    "indisponibles pendant plusieurs heures. " * 4
)
RSS = f"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Blog Azure</title><link>https://azure.example.com/</link>
<item><title>{TITLE}</title><link>https://azure.example.com/blog/outage?utm_source=rss</link>
<description>{BODY}</description><pubDate>{format_datetime(NOW)}</pubDate></item>
</channel></rss>"""


def _resolver(host: str, port: int) -> list[str]:
    return ["93.184.216.34"]  # any public IP: hosts are mocked by respx


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_path=tmp_path / "data" / "veille.db",
        app_password_hash=SecretStr(
            PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(PASSWORD)
        ),
        session_secret=SecretStr("e2e-T3st-s3cret-0a1b2c3d4e5f6789-qrstuvwxyz"),
        cookie_secure=False,
        searxng_url="http://searxng:8080",
        relevance_threshold=0.05,
        version="0.0.0-e2e",
    )


def test_run_then_read_on_the_site(settings: Settings) -> None:
    clock = FixedClock(NOW)
    with open_store(settings.database_path) as store:
        theme = store.create_theme("Azure Cloud", "Actualités du cloud Microsoft", ["Azure"])
        store.add_source(theme.id, "https://azure.example.com/feed.xml", "Blog Azure")

    with respx.mock(assert_all_called=False) as router:
        router.get("https://azure.example.com/feed.xml").mock(
            return_value=httpx.Response(
                200, text=RSS, headers={"content-type": "application/rss+xml"}
            )
        )
        router.get(url__startswith="http://searxng:8080/search").mock(
            return_value=httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "url": "https://www.news.example.org/azure-panne",
                            "title": TITLE,
                            "content": BODY,
                            "publishedDate": NOW.isoformat(),
                        }
                    ]
                },
            )
        )
        client = httpx.Client()
        http = SafeHttp(settings, client=client, resolver=_resolver, sleep=lambda s: None)
        llm = FakeLLM()
        with open_store(settings.database_path) as store:
            result = run_pipeline(
                store=store,
                collectors=[
                    RssCollector(settings, http),
                    SearxngCollector(settings, client=client),
                ],
                extractor=TrafilaturaExtractor(settings, http),
                llm=llm,
                clock=clock,
                settings=settings,
                canonicalize=canonicalize,
                trigger=RunTrigger.CLI,
                force=True,
            )
        client.close()

    assert result.status is RunStatus.OK, result.stats
    (stats,) = result.stats.themes
    assert stats.kept == 1, stats  # same event from 2 sources -> one entry

    app = create_app(
        settings,
        store_factory=lambda: open_store(settings.database_path),
        llm=None,
        feed_validator=lambda url: "x",
        clock=clock,
    )
    web = TestClient(app, follow_redirects=False)
    token = re.search(r'name="csrf_token" value="([^"]+)"', web.get("/login").text)
    assert token
    assert (
        web.post("/login", data={"password": PASSWORD, "csrf_token": token.group(1)}).status_code
        == 303
    )

    home = web.get("/").text
    assert "Azure Cloud" in home
    assert "https://azure.example.com/blog/outage" in home
    assert 'href="https://www.news.example.org/azure-panne"' in home
    assert 'rel="noopener noreferrer nofollow"' in home

    assert "2026-11-08" in web.get("/archive").text
    assert "Azure Cloud" in web.get("/archive/2026-11-08").text
    search = web.get("/search", params={"q": "panne"}).text
    assert "<mark>" in search

    health = web.get("/healthz").json()
    assert health["db"] is True
    assert health["last_run"]["status"] == "ok"
    assert "Azure Cloud" in web.get("/admin/runs").text

    # A second non-forced run the same day changes nothing (idempotent).
    with open_store(settings.database_path) as store:
        again = run_pipeline(
            store=store,
            collectors=[],
            extractor=TrafilaturaExtractor(settings, http),
            llm=llm,
            clock=clock,
            settings=settings,
            canonicalize=canonicalize,
            trigger=RunTrigger.CLI,
        )
        assert again.stats.themes[0].status == "skipped"
