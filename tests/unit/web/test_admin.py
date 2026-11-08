from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from veille.contracts import RunStats, RunStatus, RunTrigger, ThemeStats
from veille.web.routes.admin import _theme_rows

from .conftest import Env

START = datetime(2026, 11, 8, 4, 30, tzinfo=UTC)


def _add_runs(env: Env, count: int) -> None:
    for index in range(count):
        run_id = env.store.start_run(RunTrigger.CRON, START + timedelta(days=index))
        assert run_id is not None
        env.store.finish_run(
            run_id,
            status=RunStatus.OK,
            stats=RunStats(
                themes=(
                    ThemeStats(1, f"Thème <{index}>", collected=12, kept=3, duration_s=4.25),
                    ThemeStats(2, "Erreur", status="error", error="Ollama KO"),
                ),
                duration_s=5.0,
            ),
            finished_at=START + timedelta(days=index, minutes=5),
        )


def test_runs_page(auth_client: TestClient, env: Env) -> None:
    assert "Aucune exécution" in auth_client.get("/admin/runs").text
    _add_runs(env, 25)
    html = auth_client.get("/admin/runs").text
    assert html.count("<h2>Exécution n°") == 20
    assert "Exécution n° 25" in html
    assert "Exécution n° 5 " not in html
    assert "Thème &lt;24&gt;" in html
    assert "<td>12</td>" in html
    assert "4.2" in html or "4.3" in html
    assert "Ollama KO" in html
    assert "08/11/2026 05:30" not in html  # oldest runs are not shown
    assert "02/12/2026 05:30" in html  # local (Paris) time of the newest run


def test_running_run_and_odd_stats(auth_client: TestClient, env: Env) -> None:
    env.store.start_run(RunTrigger.CLI, START)
    html = auth_client.get("/admin/runs").text
    assert "running" in html
    assert "fin —" in html
    assert _theme_rows({"themes": "x"}) == []
    rows = _theme_rows({"themes": [1, {"theme_id": 3, "collected": "bad"}]})
    assert rows == [
        {
            "theme_name": "3",
            "status": "",
            "error": "",
            "collected": 0,
            "new": 0,
            "clusters": 0,
            "kept": 0,
            "dropped": 0,
            "llm_calls": 0,
            "tokens_estimated": 0,
            "duration_s": 0,
        }
    ]


def test_healthz_ok_without_auth(client: TestClient, env: Env) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "db": True,
        "last_run": None,
        "version": "1.2.3-test",
    }
    _add_runs(env, 1)
    last = client.get("/healthz").json()["last_run"]
    assert last == {
        "id": 1,
        "status": "ok",
        "started_at": "2026-11-08T04:30:00+00:00",
        "finished_at": "2026-11-08T04:35:00+00:00",
    }
    assert env.store.closed >= 2


def test_healthz_db_down(client: TestClient, env: Env) -> None:
    env.store.ping_ok = False
    response = client.get("/healthz")
    assert response.status_code == 503
    assert response.json() == {
        "status": "degraded",
        "db": False,
        "last_run": None,
        "version": "1.2.3-test",
    }


def test_healthz_ping_raises(client: TestClient, env: Env) -> None:
    env.store.ping_ok = RuntimeError("disk")  # type: ignore[assignment]
    assert client.get("/healthz").status_code == 503


def test_healthz_factory_raises(env: Env) -> None:
    from veille.web.app import create_app

    def broken() -> object:
        raise RuntimeError("cannot open")

    app = create_app(
        env.settings,
        store_factory=broken,  # type: ignore[arg-type]
        llm=None,
        feed_validator=lambda url: "x",
    )
    response = TestClient(app).get("/healthz")
    assert response.status_code == 503
    assert response.json()["db"] is False


def test_store_closed_after_each_request(auth_client: TestClient, env: Env) -> None:
    before = env.store.closed
    auth_client.get("/archive")
    assert env.store.closed == before + 1
