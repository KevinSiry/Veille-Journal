"""Composition root: builds concrete implementations from Settings."""

from __future__ import annotations

import logging
from collections.abc import Callable
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
    from veille.db import open_store

    path = settings.database_path

    def factory() -> Store:
        return open_store(path)

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

    http = SafeHttp(settings)
    store = open_store(settings.database_path)
    try:
        result = run_pipeline(
            store=store,
            collectors=[RssCollector(settings, http), SearxngCollector(settings, http=http)],
            extractor=TrafilaturaExtractor(settings, http),
            llm=OllamaClient(settings),
            clock=SystemClock(),
            settings=settings,
            canonicalize=canonicalize,
            trigger=trigger,
            force=force,
            theme_ids=theme_ids,
        )
    finally:
        store.close()
    if result.skipped_locked:
        logger.warning("another run is in progress, nothing done")
        return 3
    return 0 if result.status in (RunStatus.OK, RunStatus.PARTIAL) else 1
