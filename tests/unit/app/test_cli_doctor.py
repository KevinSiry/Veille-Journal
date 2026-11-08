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
