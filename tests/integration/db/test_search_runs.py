from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from veille.contracts import (
    HIGHLIGHT_END,
    HIGHLIGHT_START,
    RunStats,
    RunStatus,
    RunTrigger,
    ThemeStats,
)
from veille.db import SqliteStore

from .conftest import NOW, make_draft


@pytest.fixture
def corpus(store: SqliteStore) -> tuple[int, int]:
    pol = store.create_theme("Politique", "", [])
    sci = store.create_theme("Sciences", "", [])
    store.save_article(
        make_draft(
            pol.id,
            day=date(2026, 11, 1),
            title="Élection",
            body="La campagne Présidentielle s'accélère avec un débat télévisé.",
        ),
        [],
        force=False,
    )
    store.save_article(
        make_draft(
            pol.id,
            day=date(2026, 11, 2),
            title="Débat",
            body="Nouveau débat sur la réforme des retraites.",
        ),
        [],
        force=False,
    )
    store.save_article(
        make_draft(
            sci.id,
            day=date(2026, 11, 2),
            title="Espace",
            body="Le télescope observe une exoplanète ; débat chez les astronomes.",
        ),
        [],
        force=False,
    )
    return pol.id, sci.id


def test_search_accent_insensitive_with_snippet(
    store: SqliteStore, corpus: tuple[int, int]
) -> None:
    hits = store.search("presidentielle")
    assert len(hits) == 1
    hit = hits[0]
    assert hit.article.theme_name == "Politique"
    assert f"{HIGHLIGHT_START}Présidentielle{HIGHLIGHT_END}" in hit.snippet
    assert isinstance(hit.rank, float)
    assert store.count_search("presidentielle") == 1
    assert len(store.search("ÉLECTION")) == 1  # title match, case/accents folded


def test_search_implicit_and_prefix_and_filter(store: SqliteStore, corpus: tuple[int, int]) -> None:
    pol, sci = corpus
    assert store.count_search("debat") == 3
    assert store.count_search("debat retraites") == 1
    assert store.count_search("exoplan*") == 1
    assert store.count_search("exoplan") == 0
    assert {h.article.theme_id for h in store.search("débat", theme_id=sci)} == {sci}
    assert store.count_search("debat", theme_id=pol) == 2
    page = store.search("debat", limit=2)
    assert len(page) == 2
    assert len(store.search("debat", limit=2, offset=2)) == 1
    ranks = [h.rank for h in store.search("debat")]
    assert ranks == sorted(ranks)


@pytest.mark.parametrize(
    "odd", ['"', "AND", "*", "(", ")", "NEAR(", "a OR", '"débat', "^", "-", "", "  ", "col:x"]
)
def test_search_never_raises(store: SqliteStore, corpus: tuple[int, int], odd: str) -> None:
    hits = store.search(odd)
    assert isinstance(hits, list)
    assert store.count_search(odd) >= 0


def test_search_odd_quote_still_matches(store: SqliteStore, corpus: tuple[int, int]) -> None:
    assert store.count_search('"débat') == 3
    assert store.count_search("(") == 0


def test_runs_lock_and_finish(store: SqliteStore) -> None:
    assert store.last_run() is None
    run_id = store.start_run(RunTrigger.CRON, NOW)
    assert run_id is not None
    assert store.start_run(RunTrigger.CLI, NOW + timedelta(hours=2, minutes=59)) is None

    stats = RunStats(themes=(ThemeStats(theme_id=1, theme_name="Climat", kept=3),), duration_s=1.5)
    store.finish_run(
        run_id, status=RunStatus.OK, stats=stats, finished_at=NOW + timedelta(minutes=5)
    )
    run = store.last_run()
    assert run is not None
    assert run.id == run_id
    assert run.status is RunStatus.OK
    assert run.trigger is RunTrigger.CRON
    assert run.finished_at == NOW + timedelta(minutes=5)
    assert run.stats == json.loads(json.dumps(stats.to_json_dict()))
    assert run.stats["themes"][0]["theme_name"] == "Climat"
    assert run.error is None

    second = store.start_run(RunTrigger.CLI, NOW + timedelta(minutes=10))
    assert second is not None
    store.finish_run(
        second,
        status=RunStatus.ERROR,
        stats=RunStats(),
        finished_at=NOW + timedelta(minutes=11),
        error="boom",
    )
    assert [r.id for r in store.list_runs()] == [second, run_id]
    assert store.list_runs(limit=1)[0].error == "boom"


def test_stale_run_lock_is_taken_over(store: SqliteStore) -> None:
    stale = store.start_run(RunTrigger.CRON, NOW)
    assert stale is not None
    fresh = store.start_run(RunTrigger.CLI, NOW + timedelta(hours=3, seconds=1))
    assert fresh is not None
    runs = {r.id: r for r in store.list_runs()}
    assert runs[stale].status is RunStatus.ERROR
    assert runs[fresh].status is RunStatus.RUNNING
    assert runs[fresh].started_at == NOW + timedelta(hours=3, seconds=1)
    assert runs[fresh].finished_at is None
