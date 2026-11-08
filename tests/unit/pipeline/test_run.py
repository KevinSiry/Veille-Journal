from datetime import timedelta
from pathlib import Path

import pytest

from veille.clock import FixedClock, day_start_utc
from veille.config import Settings
from veille.contracts import (
    EMPTY_ARTICLE_TEXT,
    ArticleStatus,
    CollectError,
    ItemStatus,
    LLMUnavailableError,
    RunStatus,
    RunTrigger,
)
from veille.llm import FakeLLM
from veille.pipeline.run import run_pipeline

from .fakes import (
    NOW,
    FakeCollector,
    FakeExtractor,
    FakeStore,
    identity_canonicalize,
    make_theme,
    raw,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "pipeline"
OUTAGE = (FIXTURES / "azure_outage.txt").read_text(encoding="utf-8")
DAY = NOW.date()


def settings(**overrides: object) -> Settings:
    return Settings(relevance_threshold=0.1, **overrides)  # type: ignore[arg-type]


def go(store, collectors, llm, *, extractor=None, force=False, theme_ids=None, cfg=None):
    return run_pipeline(
        store=store,
        collectors=collectors,
        extractor=extractor or FakeExtractor(),
        llm=llm,
        clock=FixedClock(NOW),
        settings=cfg or settings(),
        canonicalize=identity_canonicalize,
        trigger=RunTrigger.CLI,
        force=force,
        theme_ids=theme_ids,
    )


def outage_items() -> list:
    return [
        raw(
            "https://a.example/panne",
            "Microsoft Azure panne en Europe de l'Ouest",
            snippet=OUTAGE,
            source_name="Le Monde Informatique",
        ),
        raw(
            "https://b.example/outage",
            "Microsoft Azure : panne en Europe de l'Ouest",
            snippet=OUTAGE,
            source_name="ZDNet",
        ),
        raw("https://c.example/tarte", "Recette de la tarte aux pommes", snippet="x" * 500),
    ]


def test_happy_path_two_sources_one_entry() -> None:
    store = FakeStore(themes=[make_theme()])
    llm = FakeLLM()
    result = go(store, [FakeCollector(outage_items())], llm)
    assert result.status is RunStatus.OK and not result.skipped_locked
    assert result.run_id == 1
    saved = store.article_for(1)
    draft = saved.draft
    assert draft.status is ArticleStatus.OK and draft.entry_count == 1
    assert draft.title == "Azure Cloud — dimanche 8 novembre 2026"
    assert draft.model == "fake-llm" and draft.day == DAY
    entry = draft.entries[0]
    assert [link.url for link in entry.source_links] == [
        "https://a.example/panne",
        "https://b.example/outage",
    ]
    assert draft.body_md.startswith("## Azure Cloud — dimanche 8 novembre 2026\n\n- **")
    assert (
        "[Le Monde Informatique](https://a.example/panne) · [ZDNet](https://b.example/outage)"
        in draft.body_md
    )
    statuses = {i.canonical_url: i.status for i in saved.items}
    assert statuses == {
        "https://a.example/panne": ItemStatus.USED,
        "https://b.example/outage": ItemStatus.USED,
        "https://c.example/tarte": ItemStatus.DROPPED,
    }
    assert len(llm.calls) == 1
    stats = result.stats.themes[0]
    assert (stats.collected, stats.new, stats.clusters, stats.kept, stats.dropped) == (
        3,
        3,
        1,
        1,
        1,
    )
    assert stats.llm_calls == 1 and stats.tokens_estimated > 0 and stats.status == "ok"
    run = store.runs[1]
    assert run["status"] is RunStatus.OK and run["finished_at"] == NOW
    assert store.purged_before == [NOW - timedelta(days=30)]
    assert store.known_calls[0][2] is None  # no force -> no "before"


def test_invalid_json_retry_then_drop() -> None:
    store = FakeStore(themes=[make_theme()])
    llm = FakeLLM(["{oops", "nope"])
    result = go(store, [FakeCollector(outage_items())], llm)
    draft = store.article_for(1).draft
    assert draft.status is ArticleStatus.EMPTY and EMPTY_ARTICLE_TEXT in draft.body_md
    assert len(llm.calls) == 2 and result.status is RunStatus.OK
    assert all(i.status is ItemStatus.DROPPED for i in store.article_for(1).items)
    assert result.stats.themes[0].status == "empty"


def test_invalid_json_retry_success() -> None:
    store = FakeStore(themes=[make_theme()])
    good = {"relevant": True, "headline_fr": "Panne Azure", "summary_fr": "Une panne."}
    llm = FakeLLM(["{oops", good])
    go(store, [FakeCollector(outage_items())], llm)
    assert store.article_for(1).draft.entries[0].headline_fr == "Panne Azure"
    assert len(llm.calls) == 2


def test_not_relevant_dropped() -> None:
    store = FakeStore(themes=[make_theme()])
    llm = FakeLLM([{"relevant": False, "headline_fr": "x", "summary_fr": "y"}])
    go(store, [FakeCollector(outage_items())], llm)
    assert store.article_for(1).draft.status is ArticleStatus.EMPTY


def test_empty_day_zero_llm_calls() -> None:
    store = FakeStore(themes=[make_theme()])
    llm = FakeLLM()
    result = go(store, [FakeCollector([])], llm)
    draft = store.article_for(1).draft
    assert draft.body_md == f"## {draft.title}\n\n{EMPTY_ARTICLE_TEXT}\n"
    assert draft.body_md.endswith("Rien de notable aujourd'hui.\n")
    assert llm.calls == [] and llm.embed_calls == []
    assert result.stats.themes[0].llm_calls == 0


def test_theme_error_isolation_collector() -> None:
    themes = [make_theme(1), make_theme(2, name="Autre")]
    store = FakeStore(themes=themes)
    collector = FakeCollector({1: outage_items(), 2: []}, error=CollectError("down"), fail_for=[1])
    result = go(store, [collector], FakeLLM())
    assert result.status is RunStatus.PARTIAL
    by_id = {t.theme_id: t for t in result.stats.themes}
    assert by_id[1].status == "error" and "down" in (by_id[1].error or "")
    assert by_id[2].status == "empty"
    assert [s.draft.theme_id for s in store.saved] == [2]
    assert store.runs[1]["status"] is RunStatus.PARTIAL and "down" in str(store.runs[1]["error"])


def test_theme_error_isolation_llm_unavailable() -> None:
    themes = [make_theme(1), make_theme(2, name="Azure bis")]
    store = FakeStore(themes=themes)
    llm = FakeLLM([LLMUnavailableError("busy"), LLMUnavailableError("busy")])
    result = go(store, [FakeCollector(outage_items())], llm)
    assert [t.status for t in result.stats.themes] == ["error", "ok"]
    assert result.status is RunStatus.PARTIAL
    assert store.article_for(2).draft.entry_count == 1


def test_all_themes_fail_is_error_and_one_collector_failure_tolerated() -> None:
    store = FakeStore(themes=[make_theme(1)])
    result = go(store, [FakeCollector(error=RuntimeError("x"))], FakeLLM())
    assert result.status is RunStatus.ERROR
    store = FakeStore(themes=[make_theme(1)])
    result = go(
        store, [FakeCollector(error=RuntimeError("x")), FakeCollector(outage_items())], FakeLLM()
    )
    assert result.status is RunStatus.OK and store.article_for(1).draft.entry_count == 1


def test_embed_failure_is_theme_error() -> None:
    store = FakeStore(themes=[make_theme()])
    result = go(
        store, [FakeCollector(outage_items())], FakeLLM(embed_error=LLMUnavailableError("x"))
    )
    assert result.status is RunStatus.ERROR and not store.saved


def test_lock_skipped() -> None:
    store = FakeStore(themes=[make_theme()], locked=True)
    collector = FakeCollector(outage_items())
    result = go(store, [collector], FakeLLM())
    assert result.skipped_locked and result.run_id is None
    assert collector.calls == [] and not store.runs and not store.purged_before


def test_existing_article_skipped_without_force_and_force_passes_before() -> None:
    store = FakeStore(themes=[make_theme()], existing_articles={(1, DAY)})
    collector = FakeCollector(outage_items())
    result = go(store, [collector], FakeLLM())
    assert result.stats.themes[0].status == "skipped" and collector.calls == []
    assert result.status is RunStatus.OK

    cfg = settings()
    result = go(store, [collector], FakeLLM(), force=True, cfg=cfg)
    assert result.stats.themes[0].status == "ok"
    assert store.known_calls[0][2] == day_start_utc(DAY, cfg.tz)
    assert store.saved[-1].force is True


def test_save_refused_counts_as_skipped() -> None:
    class RacingStore(FakeStore):
        def save_article(self, draft, items, *, force):  # type: ignore[no-untyped-def]
            return False

    store = RacingStore(themes=[make_theme()])
    result = go(store, [FakeCollector(outage_items())], FakeLLM())
    assert result.stats.themes[0].status == "skipped"


def test_top_n_cap_and_selected_status() -> None:
    events = ["lance Foo", "ferme Bar", "rachète Baz", "annonce Qux"]
    items = [
        raw(f"https://n/{i}", f"Azure {e}", snippet=f"Azure {e}. " + OUTAGE, source_name=f"S{i}")
        for i, e in enumerate(events)
    ]
    # Same outlet twice on the first event -> second copy is SELECTED, not linked.
    items.append(
        raw(
            "https://n/dup",
            "Azure lance Foo",
            snippet="Azure lance Foo. " + OUTAGE,
            source_name="S0",
        )
    )
    store = FakeStore(themes=[make_theme()])
    llm = FakeLLM()
    result = go(store, [FakeCollector(items)], llm, cfg=settings(max_items_per_theme=2))
    saved = store.article_for(1)
    assert saved.draft.entry_count == 2 and len(llm.calls) == 2
    statuses = sorted(i.status.value for i in saved.items)
    assert statuses.count("dropped") == 2 and statuses.count("selected") == 1
    assert statuses.count("used") == 2
    assert result.stats.themes[0].clusters == 4


def test_pinned_source_bypasses_keywords_and_theme_filter() -> None:
    themes = [make_theme(1, keywords=("kubernetes",)), make_theme(2, name="Other")]
    store = FakeStore(themes=themes)
    pinned = raw("https://p/1", "Microsoft Azure panne Europe", snippet=OUTAGE, source_id=7)
    result = go(store, [FakeCollector({1: [pinned]})], FakeLLM(), theme_ids=[1])
    assert [t.theme_id for t in result.stats.themes] == [1]
    assert store.article_for(1).draft.entry_count == 1


def test_inactive_theme_ignored_and_purge_error_ignored() -> None:
    store = FakeStore(themes=[make_theme(active=False)], purge_error=RuntimeError("locked"))
    result = go(store, [FakeCollector(outage_items())], FakeLLM())
    assert result.status is RunStatus.OK and result.stats.themes == ()


def test_store_failure_marks_run_error() -> None:
    class BrokenStore(FakeStore):
        def list_themes(self, *, active_only=False):  # type: ignore[no-untyped-def]
            raise RuntimeError("db gone")

    store = BrokenStore()
    result = go(store, [], FakeLLM())
    assert result.status is RunStatus.ERROR
    assert store.runs[1]["status"] is RunStatus.ERROR and store.runs[1]["error"] == "db gone"


def test_extractor_used_in_pipeline() -> None:
    from veille.contracts import ExtractedPage

    store = FakeStore(themes=[make_theme()])
    extractor = FakeExtractor({"https://a/1": ExtractedPage("https://a/1", OUTAGE * 5)})
    go(
        store,
        [FakeCollector([raw("https://a/1", "Azure panne Europe", snippet="court")])],
        FakeLLM(),
        extractor=extractor,
    )
    item = store.article_for(1).items[0]
    assert extractor.calls == ["https://a/1"] and len(item.content_text) == 3000


@pytest.mark.parametrize("hours, kept", [(35, 1), (37, 0)])
def test_window_in_pipeline(hours: int, kept: int) -> None:
    store = FakeStore(themes=[make_theme()])
    item = raw(
        "https://a/1",
        "Azure panne Europe",
        snippet=OUTAGE,
        published_at=NOW - timedelta(hours=hours),
    )
    go(store, [FakeCollector([item])], FakeLLM())
    assert store.article_for(1).draft.entry_count == kept
