"""Nightly pipeline orchestration: one sequential pass over the active themes.

Item statuses persisted with the article:
- ``USED``: linked from an entry of the article (one per distinct source of a cluster).
- ``SELECTED``: member of a rendered cluster but not linked (same source name as a linked
  item, e.g. the same outlet publishing twice).
- ``DROPPED``: everything else (keyword stage, embedding stage, beyond the top-N, or
  cluster rejected by the LLM / failed twice).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Literal

from veille.clock import day_start_utc, local_day
from veille.config import Settings
from veille.contracts import (
    LLM,
    Clock,
    CollectError,
    Collector,
    ContentExtractor,
    Entry,
    Item,
    ItemStatus,
    RawItem,
    RunStats,
    RunStatus,
    RunTrigger,
    Store,
    Theme,
    ThemeStats,
)
from veille.pipeline.dedupe import cluster_with_llm, select_top
from veille.pipeline.prepare import prepare_items
from veille.pipeline.relevance import embedding_stage, keyword_stage
from veille.pipeline.render import render_article
from veille.pipeline.summarize import summarize_cluster

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RunResult:
    run_id: int | None
    status: RunStatus
    stats: RunStats
    skipped_locked: bool


def collect_all(collectors: Sequence[Collector], theme: Theme, store: Store) -> list[RawItem]:
    """Run every collector; a failing collector is logged and skipped. Raises
    ``CollectError`` only when every collector failed."""
    sources = store.list_sources(theme.id, active_only=True)
    raw: list[RawItem] = []
    failures: list[str] = []
    for collector in collectors:
        try:
            raw.extend(collector.collect(theme, sources))
        except Exception as exc:
            name = getattr(collector, "name", type(collector).__name__)
            logger.warning("Collector %s failed for theme %r: %s", name, theme.name, exc)
            failures.append(f"{name}: {exc}")
    if collectors and len(failures) == len(collectors):
        raise CollectError("Toutes les sources ont échoué : " + "; ".join(failures))
    return raw


def _final_statuses(
    rendered: Sequence[tuple[Entry, Sequence[Item]]], dropped: Sequence[Item]
) -> list[Item]:
    items: list[Item] = list(dropped)
    for entry, members in rendered:
        linked = {link.item_canonical_url for link in entry.source_links}
        for item in members:
            status = ItemStatus.USED if item.canonical_url in linked else ItemStatus.SELECTED
            items.append(replace(item, status=status))
    return items


def process_theme(
    theme: Theme,
    *,
    day: date,
    store: Store,
    collectors: Sequence[Collector],
    extractor: ContentExtractor,
    llm: LLM,
    clock: Clock,
    settings: Settings,
    canonicalize: Callable[[str], str],
    force: bool,
) -> ThemeStats:
    """Collect -> prepare -> relevance -> dedupe -> summarize -> render -> save."""
    started = time.monotonic()
    now = clock.now()
    raw = collect_all(collectors, theme, store)
    items = prepare_items(
        raw,
        theme_id=theme.id,
        store=store,
        extractor=extractor,
        canonicalize=canonicalize,
        now=now,
        lookback_hours=settings.lookback_hours,
        content_max_chars=settings.content_max_chars,
        known_before=day_start_utc(day, settings.tz) if force else None,
    )
    passed, dropped = keyword_stage(items, theme)
    relevant, dropped_emb = embedding_stage(
        passed, theme, llm, threshold=settings.relevance_threshold
    )
    dropped += dropped_emb
    clusters = cluster_with_llm(relevant, llm, threshold=settings.dedupe_threshold)
    top, dropped_top = select_top(clusters, settings.max_items_per_theme)
    dropped += dropped_top

    rendered: list[tuple[Entry, Sequence[Item]]] = []
    llm_calls = tokens = 0
    for cluster in top:
        outcome = summarize_cluster(cluster, theme, llm)
        llm_calls += outcome.llm_calls
        tokens += outcome.tokens
        if outcome.entry is None:
            dropped += [replace(i, status=ItemStatus.DROPPED) for i in cluster.items]
        else:
            rendered.append((outcome.entry, cluster.items))

    entries = [entry for entry, _ in rendered]
    draft = render_article(theme, day, entries, model=llm.model, generated_at=clock.now())
    saved = store.save_article(draft, _final_statuses(rendered, dropped), force=force)
    status: Literal["ok", "empty", "skipped"] = (
        ("ok" if entries else "empty") if saved else "skipped"
    )
    return ThemeStats(
        theme_id=theme.id,
        theme_name=theme.name,
        collected=len(raw),
        new=len(items),
        clusters=len(clusters),
        kept=len(entries),
        dropped=len(dropped),
        llm_calls=llm_calls,
        tokens_estimated=tokens,
        duration_s=round(time.monotonic() - started, 3),
        status=status,
    )


def _log_theme(stats: ThemeStats) -> None:
    logger.info(
        "theme=%r status=%s collected=%d new=%d clusters=%d kept=%d dropped=%d "
        "llm_calls=%d tokens=%d duration=%.1fs%s",
        stats.theme_name,
        stats.status,
        stats.collected,
        stats.new,
        stats.clusters,
        stats.kept,
        stats.dropped,
        stats.llm_calls,
        stats.tokens_estimated,
        stats.duration_s,
        f" error={stats.error}" if stats.error else "",
    )


def _run_status(themes: Sequence[ThemeStats]) -> RunStatus:
    errors = sum(1 for t in themes if t.status == "error")
    if errors == 0:
        return RunStatus.OK
    return RunStatus.ERROR if errors == len(themes) else RunStatus.PARTIAL


def run_pipeline(
    *,
    store: Store,
    collectors: Sequence[Collector],
    extractor: ContentExtractor,
    llm: LLM,
    clock: Clock,
    settings: Settings,
    canonicalize: Callable[[str], str],
    trigger: RunTrigger,
    force: bool = False,
    theme_ids: Sequence[int] | None = None,
) -> RunResult:
    """Run the whole pipeline once. Never raises for a theme-level failure."""
    started_at: datetime = clock.now()
    run_id = store.start_run(trigger, started_at)
    if run_id is None:
        logger.info("Another run is in progress, skipping")
        return RunResult(None, RunStatus.RUNNING, RunStats(), skipped_locked=True)

    started = time.monotonic()
    day = local_day(started_at, settings.tz)
    theme_stats: list[ThemeStats] = []
    status = RunStatus.ERROR
    error: str | None = None
    try:
        themes = store.list_themes(active_only=True)
        if theme_ids is not None:
            wanted = set(theme_ids)
            themes = [t for t in themes if t.id in wanted]
        for theme in themes:
            theme_started = time.monotonic()
            try:
                if not force and store.get_article(theme.id, day) is not None:
                    stats = ThemeStats(theme_id=theme.id, theme_name=theme.name, status="skipped")
                else:
                    stats = process_theme(
                        theme,
                        day=day,
                        store=store,
                        collectors=collectors,
                        extractor=extractor,
                        llm=llm,
                        clock=clock,
                        settings=settings,
                        canonicalize=canonicalize,
                        force=force,
                    )
            except Exception as exc:
                logger.exception("Theme %r failed", theme.name)
                stats = ThemeStats(
                    theme_id=theme.id,
                    theme_name=theme.name,
                    duration_s=round(time.monotonic() - theme_started, 3),
                    status="error",
                    error=str(exc) or type(exc).__name__,
                )
            _log_theme(stats)
            theme_stats.append(stats)
        try:
            purged = store.purge_old_content(
                clock.now() - timedelta(days=settings.content_retention_days)
            )
            logger.info("Purged content of %d old items", purged)
        except Exception:
            logger.warning("Content purge failed", exc_info=True)
        status = _run_status(theme_stats)
        if status is not RunStatus.OK:
            error = "; ".join(f"{t.theme_name}: {t.error}" for t in theme_stats if t.error)
    except Exception as exc:
        logger.exception("Run %s failed", run_id)
        status, error = RunStatus.ERROR, str(exc) or type(exc).__name__
    finally:
        run_stats = RunStats(
            themes=tuple(theme_stats), duration_s=round(time.monotonic() - started, 3)
        )
        store.finish_run(
            run_id, status=status, stats=run_stats, finished_at=clock.now(), error=error
        )
    logger.info("Run %s finished: %s in %.1fs", run_id, status.value, run_stats.duration_s)
    return RunResult(run_id, status, run_stats, skipped_locked=False)
