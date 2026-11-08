from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from veille import wiring
from veille.config import Settings
from veille.contracts import RunStatus, RunTrigger


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        database_path=tmp_path / "v.db",
        session_secret=SecretStr("wiring-T3st-s3cret-9f8e7d6c5b4a3210zyxw"),
        cookie_secure=False,
    )


def test_build_web_app_serves_healthz(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    app = wiring.build_web_app(_settings(tmp_path))
    response = TestClient(app).get("/healthz")
    assert response.status_code == 200
    assert response.json()["db"] is True


@pytest.mark.parametrize(
    ("status", "locked", "code"),
    [
        (RunStatus.OK, False, 0),
        (RunStatus.PARTIAL, False, 0),
        (RunStatus.ERROR, False, 1),
        (RunStatus.RUNNING, True, 3),
    ],
)
def test_run_once_exit_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: RunStatus, locked: bool, code: int
) -> None:
    from veille.contracts import RunStats
    from veille.pipeline import run as run_module
    from veille.pipeline.run import RunResult

    def fake(**kwargs: object) -> RunResult:
        assert kwargs["trigger"] is RunTrigger.CLI
        return RunResult(run_id=1, status=status, stats=RunStats(), skipped_locked=locked)

    monkeypatch.setattr(run_module, "run_pipeline", fake)
    assert wiring.run_once(_settings(tmp_path), trigger=RunTrigger.CLI) == code


def test_store_factory_does_not_migrate(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    store = wiring.store_factory(settings)()
    try:
        assert store.ping()
        tables = store.connection.execute(  # type: ignore[attr-defined]
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        assert tables == []  # no migration on the per-request path
    finally:
        store.close()


def test_run_once_closes_every_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from veille.collect.net_safety import SafeHttp
    from veille.collect.searxng import SearxngCollector
    from veille.contracts import RunStats
    from veille.llm.ollama_client import OllamaClient
    from veille.pipeline import run as run_module
    from veille.pipeline.run import RunResult

    closed: list[str] = []
    for cls in (SafeHttp, SearxngCollector, OllamaClient):
        original = cls.close

        def close(self: object, _name: str = cls.__name__, _orig: object = original) -> None:
            closed.append(_name)
            _orig(self)  # type: ignore[operator]

        monkeypatch.setattr(cls, "close", close)

    def boom(**kwargs: object) -> RunResult:
        raise KeyboardInterrupt

    monkeypatch.setattr(run_module, "run_pipeline", boom)
    with pytest.raises(KeyboardInterrupt):
        wiring.run_once(_settings(tmp_path), trigger=RunTrigger.CLI)
    assert sorted(closed) == ["OllamaClient", "SafeHttp", "SearxngCollector"]

    closed.clear()
    monkeypatch.setattr(
        run_module,
        "run_pipeline",
        lambda **kw: RunResult(1, RunStatus.OK, RunStats(), skipped_locked=False),
    )
    assert wiring.run_once(_settings(tmp_path), trigger=RunTrigger.CLI) == 0
    assert sorted(closed) == ["OllamaClient", "SafeHttp", "SearxngCollector"]
