"""Composition root: builds concrete implementations from Settings."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import ExitStack
from typing import TYPE_CHECKING

from veille.clock import SystemClock
from veille.config import Settings
from veille.contracts import RunTrigger, Store

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


def configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def store_factory(settings: Settings) -> Callable[[], Store]:
    """Per-request store: a plain connection, no migration (``build_web_app`` and the
    worker migrate once at startup)."""
    from veille.db import SqliteStore, connect

    path = settings.database_path

    def factory() -> Store:
        return SqliteStore(connect(path))

    return factory


def build_web_app(settings: Settings) -> FastAPI:
    from veille.collect.net_safety import SafeHttp
    from veille.collect.rss import validate_feed
    from veille.db import migrate
    from veille.llm.ollama_client import OllamaClient
    from veille.web.app import create_app

    migrate(settings.database_path)
    http = SafeHttp(settings)

    def feed_validator(url: str) -> str:
        return validate_feed(url, settings, http)

    return create_app(
        settings,
        store_factory=store_factory(settings),
        llm=OllamaClient(settings, timeout_s=settings.keyword_suggest_timeout_s),
        feed_validator=feed_validator,
        clock=SystemClock(),
    )


def run_once(
    settings: Settings,
    *,
    trigger: RunTrigger,
    force: bool = False,
    theme_ids: list[int] | None = None,
) -> int:
    """Run the pipeline once. Returns a process exit code."""
    from veille.collect.canonical import canonicalize
    from veille.collect.extract import TrafilaturaExtractor
    from veille.collect.net_safety import SafeHttp
    from veille.collect.rss import RssCollector
    from veille.collect.searxng import SearxngCollector
    from veille.contracts import RunStatus
    from veille.db import open_store
    from veille.llm.ollama_client import OllamaClient
    from veille.pipeline.run import run_pipeline

    with ExitStack() as stack:
        http = SafeHttp(settings)
        stack.callback(http.close)
        searxng = SearxngCollector(settings, http=http)
        stack.callback(searxng.close)
        llm = OllamaClient(settings)
        stack.callback(llm.close)
        store = open_store(settings.database_path)
        stack.callback(store.close)
        result = run_pipeline(
            store=store,
            collectors=[RssCollector(settings, http), searxng],
            extractor=TrafilaturaExtractor(settings, http),
            llm=llm,
            clock=SystemClock(),
            settings=settings,
            canonicalize=canonicalize,
            trigger=trigger,
            force=force,
            theme_ids=theme_ids,
        )
    if result.skipped_locked:
        logger.warning("another run is in progress, nothing done")
        return 3
    return 0 if result.status in (RunStatus.OK, RunStatus.PARTIAL) else 1
