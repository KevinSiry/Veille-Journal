from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from argon2 import PasswordHasher

from veille import cli
from veille.config import Settings
from veille.doctor import run_checks


def _settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, database_path=tmp_path / "v.db")  # type: ignore[call-arg]


def test_hash_password(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["hash-password", "--password", "un-mot-de-passe"]) == 0
    hashed = capsys.readouterr().out.strip()
    assert PasswordHasher().verify(hashed, "un-mot-de-passe")


def test_hash_password_too_short() -> None:
    assert cli.main(["hash-password", "--password", "court"]) == 1


def test_hash_password_prompt_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter(["motdepasse1", "autre-chose"])
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": next(answers))
    assert cli.main(["hash-password"]) == 1


def test_migrate_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: _settings(tmp_path))
    assert cli.main(["migrate"]) == 0
    assert "001" in capsys.readouterr().out
    assert cli.main(["migrate"]) == 0
    assert "aucune" in capsys.readouterr().out


def _handler(ok: bool) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        if not ok:
            raise httpx.ConnectError("down", request=request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen3:8b"}]})
        return httpx.Response(200, json={"results": [{}, {}]})

    return httpx.MockTransport(handle)


def test_doctor_checks(tmp_path: Path) -> None:
    with httpx.Client(transport=_handler(True)) as client:
        checks = {c.name: c for c in run_checks(_settings(tmp_path), client)}
    assert checks["Base de données"].ok
    assert checks["Ollama"].ok
    assert checks["Modèle qwen3:8b"].ok
    assert not checks["Modèle bge-m3"].ok
    assert checks["SearXNG"].ok and "2 résultats" in checks["SearXNG"].detail


def test_doctor_services_down(tmp_path: Path) -> None:
    with httpx.Client(transport=_handler(False)) as client:
        checks = run_checks(_settings(tmp_path), client)
    assert [c.ok for c in checks] == [True, False, False]


def test_doctor_command_exit_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: _settings(tmp_path))
    import veille.doctor

    real = veille.doctor.run_checks

    def fake(settings: Settings) -> list[veille.doctor.Check]:
        with httpx.Client(transport=_handler(False)) as client:
            return real(settings, client)

    monkeypatch.setattr(veille.doctor, "run_checks", fake)
    assert cli.main(["doctor"]) == 1
    assert "[KO] Ollama" in capsys.readouterr().out


def test_run_command_uses_wiring(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "get_settings", lambda: _settings(tmp_path))
    seen: dict[str, object] = {}

    def fake_run_once(settings: Settings, **kwargs: object) -> int:
        seen.update(kwargs)
        return 0

    import veille.wiring

    monkeypatch.setattr(veille.wiring, "run_once", fake_run_once)
    assert cli.main(["run", "--force", "--theme", "3"]) == 0
    assert seen["force"] is True and seen["theme_ids"] == [3]


def test_stop_handler_idle_sets_event_busy_raises() -> None:
    import threading

    stop, busy = threading.Event(), threading.Event()
    handler = cli.make_stop_handler(stop, busy)
    handler(15, None)
    assert stop.is_set()
    stop.clear()
    busy.set()
    with pytest.raises(KeyboardInterrupt):
        handler(15, None)
    assert stop.is_set()


def _patch_worker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, object]:
    import os
    import signal

    import veille.scheduler
    import veille.wiring

    monkeypatch.setattr(cli, "get_settings", lambda: _settings(tmp_path))
    if hasattr(os, "nice"):
        monkeypatch.setattr(os, "nice", lambda n: 0)
    handlers: dict[int, object] = {}

    def fake_signal(sig: int, handler: object) -> object:  # records, never installs
        previous = handlers.get(sig, signal.SIG_DFL)
        handlers[sig] = handler
        return previous

    monkeypatch.setattr("signal.signal", fake_signal)
    seen: dict[str, object] = {"handlers": handlers}

    def fake_run_forever(job, **kwargs):  # type: ignore[no-untyped-def]
        seen.update(kwargs)
        seen["result"] = job()

    monkeypatch.setattr(veille.scheduler, "run_forever", fake_run_forever)
    monkeypatch.setattr(veille.wiring, "run_once", lambda settings, **kw: 0)
    return seen


def test_worker_sigterm_during_run_interrupts_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import signal

    import veille.wiring

    seen = _patch_worker(monkeypatch, tmp_path)

    def run_once(settings: Settings, **kwargs: object) -> int:
        handler = seen["handlers"][signal.SIGTERM]  # type: ignore[index]
        handler(signal.SIGTERM, None)  # SIGTERM arrives while the job runs
        return 0  # pragma: no cover - not reached

    monkeypatch.setattr(veille.wiring, "run_once", run_once)
    assert cli.main(["worker"]) == cli.EXIT_INTERRUPTED
    assert seen["stop"].is_set()  # type: ignore[attr-defined]


def test_worker_catch_up_when_today_was_missed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _patch_worker(monkeypatch, tmp_path)
    monkeypatch.setattr("veille.scheduler.needs_catch_up", lambda now, at, tz, last: last is None)
    assert cli.main(["worker"]) == 0
    assert seen["catch_up"] is True and seen["result"] == 0


def test_last_run_started(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from veille.contracts import RunTrigger
    from veille.db import migrate, open_store

    settings = _settings(tmp_path)
    migrate(settings.database_path)
    assert cli._last_run_started(settings) is None
    started = datetime(2026, 11, 8, 4, 30, tzinfo=UTC)
    with open_store(settings.database_path) as store:
        store.start_run(RunTrigger.CRON, started)
    assert cli._last_run_started(settings) == started


def test_run_command_ctrl_c_returns_130(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import signal

    import veille.wiring

    monkeypatch.setattr(cli, "get_settings", lambda: _settings(tmp_path))
    before = signal.getsignal(signal.SIGTERM)

    def interrupted(settings: Settings, **kwargs: object) -> int:
        assert signal.getsignal(signal.SIGTERM) is cli._raise_interrupt
        with pytest.raises(KeyboardInterrupt):
            cli._raise_interrupt(signal.SIGTERM, None)
        raise KeyboardInterrupt

    monkeypatch.setattr(veille.wiring, "run_once", interrupted)
    assert cli.main(["run"]) == cli.EXIT_INTERRUPTED
    assert signal.getsignal(signal.SIGTERM) is before  # restored


def test_doctor_searxng_without_results_is_ko(tmp_path: Path) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": []})
        return httpx.Response(
            200, json={"results": [], "unresponsive_engines": [["bing news", "timeout"]]}
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        checks = {c.name: c for c in run_checks(_settings(tmp_path), client)}
    assert not checks["SearXNG"].ok
    assert "bing news" in checks["SearXNG"].detail
