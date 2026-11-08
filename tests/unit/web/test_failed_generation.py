"""Regression test: a failed generation is shown on the home page."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

from fastapi.testclient import TestClient

from veille.contracts import RunStats, RunStatus, RunTrigger, ThemeStats

from .conftest import Env


def _finish_run(env: Env, started: datetime, theme_id: int) -> None:
    run_id = env.store.start_run(RunTrigger.CRON, started)
    assert run_id is not None
    env.store.finish_run(
        run_id,
        status=RunStatus.PARTIAL,
        stats=RunStats(themes=(ThemeStats(theme_id, "x", status="error", error="KO"),)),
        finished_at=started + timedelta(minutes=5),
    )


def test_failed_generation_is_shown(env: Env, auth_client: TestClient) -> None:
    failed = env.store.create_theme("Climat", "", [])
    env.store.create_theme("Énergie", "", [])
    _finish_run(env, datetime(2026, 11, 8, 4, 30, tzinfo=UTC), failed.id)
    html = auth_client.get("/").text
    assert "La génération a échoué lors du dernier passage" in html
    assert 'href="/admin/runs"' in html
    assert html.count("échoué") == 1  # only for the failed theme
    assert "Pas encore généré." in html  # the other theme


def test_failed_generation_not_shown_for_other_days(env: Env, auth_client: TestClient) -> None:
    theme = env.store.create_theme("Climat", "", [])
    env.store.add_article(theme, date(2026, 11, 6))
    # Yesterday's run failed; today has not been generated yet.
    _finish_run(env, datetime(2026, 11, 7, 4, 30, tzinfo=UTC), theme.id)
    html = auth_client.get("/").text
    assert "Pas encore généré." in html
    assert "échoué" not in html
    assert "échoué" in auth_client.get("/?day=2026-11-07").text
    assert "échoué" not in auth_client.get("/?day=2026-11-06").text


def test_failed_status_ignored_when_article_exists(env: Env, auth_client: TestClient) -> None:
    theme = env.store.create_theme("Climat", "", [])
    env.store.add_article(theme, date(2026, 11, 8), title="Titre du jour")
    _finish_run(env, datetime(2026, 11, 8, 4, 30, tzinfo=UTC), theme.id)
    html = auth_client.get("/").text
    assert "Titre du jour" in html
    assert "échoué" not in html


def test_odd_run_stats_are_ignored(env: Env, auth_client: TestClient) -> None:
    env.store.create_theme("Climat", "", [])
    run_id = env.store.start_run(RunTrigger.CRON, datetime(2026, 11, 8, 4, 30, tzinfo=UTC))
    assert run_id is not None
    assert "Pas encore généré." in auth_client.get("/").text  # running run, no stats
    env.store.runs[0] = replace(env.store.runs[0], stats={"themes": "x"})
    assert "Pas encore généré." in auth_client.get("/").text
