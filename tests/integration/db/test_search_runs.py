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
    assert store.start_run(RunTrigger.CLI, NOW + timedelta(minutes=29)) is None

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
    fresh = store.start_run(RunTrigger.CLI, NOW + timedelta(minutes=30, seconds=1))
    assert fresh is not None
    runs = {r.id: r for r in store.list_runs()}
    assert runs[stale].status is RunStatus.ERROR
    assert runs[fresh].status is RunStatus.RUNNING
    assert runs[fresh].started_at == NOW + timedelta(minutes=30, seconds=1)
    assert runs[fresh].finished_at is None


def test_heartbeat_keeps_a_long_run_locked(store: SqliteStore) -> None:
    run_id = store.start_run(RunTrigger.CRON, NOW)
    assert run_id is not None
    beat_at = NOW + timedelta(hours=2)
    store.heartbeat_run(run_id, beat_at)
    # 2 h 29 after the start, but only 29 min after the last heartbeat: still locked.
    assert store.start_run(RunTrigger.CLI, beat_at + timedelta(minutes=29)) is None
    # 31 min without heartbeat: stale, taken over.
    taken = store.start_run(RunTrigger.CLI, beat_at + timedelta(minutes=31))
    assert taken is not None and taken != run_id
    runs = {r.id: r for r in store.list_runs()}
    assert runs[run_id].status is RunStatus.ERROR
    # A heartbeat of the abandoned run does not resurrect it.
    store.heartbeat_run(run_id, beat_at + timedelta(minutes=32))
    row = store.connection.execute(
        "SELECT status, heartbeat_at FROM runs WHERE id = ?", (run_id,)
    ).fetchone()
    assert row["status"] == "error"


def test_legacy_running_row_without_heartbeat_uses_started_at(store: SqliteStore) -> None:
    from veille.db.store import to_db_datetime

    store.connection.execute(
        "INSERT INTO runs (started_at, trigger, status) VALUES (?, 'cron', 'running')",
        (to_db_datetime(NOW),),
    )
    assert store.start_run(RunTrigger.CLI, NOW + timedelta(minutes=10)) is None
    assert store.start_run(RunTrigger.CLI, NOW + timedelta(minutes=31)) is not None


def test_finish_run_retries_when_locked(
    store: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    from veille.db import store as store_module

    run_id = store.start_run(RunTrigger.CRON, NOW)
    assert run_id is not None
    sleeps: list[float] = []
    monkeypatch.setattr(store_module, "_sleep", sleeps.append)
    original = store._write_finished_run
    failures = iter([sqlite3.OperationalError("database is locked")] * 2)

    def flaky(values: object) -> None:
        error = next(failures, None)
        if error is not None:
            raise error
        original(values)  # type: ignore[arg-type]

    monkeypatch.setattr(store, "_write_finished_run", flaky)
    store.finish_run(run_id, status=RunStatus.OK, stats=RunStats(), finished_at=NOW)
    assert sleeps == [0.5, 1.0]
    last = store.last_run()
    assert last is not None and last.status is RunStatus.OK


def test_finish_run_gives_up_and_other_errors_not_retried(
    store: SqliteStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    from veille.db import store as store_module

    monkeypatch.setattr(store_module, "_sleep", lambda s: None)
    calls: list[str] = []

    def locked(values: object) -> None:
        calls.append("locked")
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "_write_finished_run", locked)
    with pytest.raises(sqlite3.OperationalError):
        store.finish_run(1, status=RunStatus.OK, stats=RunStats(), finished_at=NOW)
    assert len(calls) == store_module.FINISH_RUN_ATTEMPTS

    def broken(values: object) -> None:
        calls.append("broken")
        raise sqlite3.OperationalError("no such table")

    calls.clear()
    monkeypatch.setattr(store, "_write_finished_run", broken)
    with pytest.raises(sqlite3.OperationalError):
        store.finish_run(1, status=RunStatus.OK, stats=RunStats(), finished_at=NOW)
    assert calls == ["broken"]
