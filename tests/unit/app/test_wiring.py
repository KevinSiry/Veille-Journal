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
        session_secret=SecretStr("k" * 40),
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
