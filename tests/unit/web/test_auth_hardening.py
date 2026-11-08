"""Regression tests: secrets checks, proxy trust, login limits, session revocation."""

from __future__ import annotations

import re
import secrets
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from veille.clock import FixedClock
from veille.config import Settings
from veille.web.app import create_app
from veille.web.auth import SESSION_MAX_AGE_S, LoginRateLimiter, VerifyGate

from .conftest import Env, csrf_from, login, make_settings, ok_validator
from .fakes import FakeStore

ROOT = Path(__file__).resolve().parents[3]


def _create(**overrides: Any) -> None:
    create_app(
        make_settings(**overrides), store_factory=FakeStore, llm=None, feed_validator=ok_validator
    )


def _env_example_value(name: str) -> str:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    match = re.search(rf"^{name}=(.*)$", text, re.MULTILINE)
    assert match, name
    return match.group(1).strip().strip("'\"")


# --- 1. placeholder secrets ---------------------------------------------------


@pytest.mark.parametrize(
    "secret",
    [
        "a-remplacer-par-un-secret-aleatoire",  # the old .env.example value (35 chars)
        "please-CHANGE-me-please-change-me-now",
        "this-is-an-example-secret-value-1234567",
        "mon-secret-aleatoire-0123456789abcdefgh",
        "abababababababababababababababababababab",  # long but only 2 distinct chars
        "0123456789a0123456789a0123456789a0123",  # 11 distinct chars
    ],
)
def test_create_app_refuses_placeholder_secret(secret: str) -> None:
    with pytest.raises(ValueError, match="SESSION_SECRET") as info:
        _create(session_secret=SecretStr(secret))
    assert "secrets.token_urlsafe(48)" in str(info.value)


def test_env_example_secrets_are_refused_as_is() -> None:
    """Copying .env.example without filling it in must not give a running site."""
    secret = _env_example_value("SESSION_SECRET")
    assert secret == ""
    with pytest.raises(ValueError, match="SESSION_SECRET"):
        _create(session_secret=SecretStr(secret))
    assert _env_example_value("APP_PASSWORD_HASH") == ""
    assert _env_example_value("SEARXNG_SECRET") == ""


def test_create_app_accepts_generated_secret() -> None:
    _create(session_secret=SecretStr(secrets.token_urlsafe(48)))


@pytest.mark.parametrize(
    "password_hash",
    ["a-remplacer-par-le-hash-argon2", "$argon2i$v=19$m=8,t=1,p=1$abc$def", "motdepasse"],
)
def test_create_app_refuses_non_argon2id_hash(password_hash: str) -> None:
    with pytest.raises(ValueError, match="APP_PASSWORD_HASH") as info:
        _create(app_password_hash=SecretStr(password_hash))
    assert "veille hash-password" in str(info.value)


def test_create_app_accepts_empty_hash() -> None:
    _create(app_password_hash=SecretStr(""))


# --- 2. proxy trust + rate limits ---------------------------------------------


def test_forwarded_allow_ips_default_is_not_a_wildcard() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.forwarded_allow_ips == "127.0.0.1"
    assert _env_example_value("FORWARDED_ALLOW_IPS") == "172.16.0.0/12"


def test_global_failure_cap_across_ips(env: Env) -> None:
    app = env.app()
    for index in range(20):
        c = TestClient(app, follow_redirects=False, client=(f"203.0.113.{index}", 1234))
        assert login(c, "wrong").status_code == 401
    fresh = TestClient(app, follow_redirects=False, client=("198.51.100.7", 1234))
    response = login(fresh)
    assert response.status_code == 429
    assert "Trop de tentatives" in response.text
    env.clock.advance(timedelta(minutes=11))
    assert login(fresh).status_code == 303


def test_limiter_caps_tracked_keys() -> None:
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    limiter = LoginRateLimiter(clock, max_keys=100, max_global_failures=10**9)
    for index in range(1000):
        assert limiter.reserve(f"10.0.{index // 256}.{index % 256}") is not None
    assert limiter.tracked_keys() <= 100
    # Stale keys are swept first.
    clock.advance(timedelta(minutes=11))
    limiter.reserve("fresh")
    assert limiter.tracked_keys() == 1


def test_limiter_cancel_and_global_expiry() -> None:
    clock = FixedClock(datetime(2026, 1, 1, tzinfo=UTC))
    limiter = LoginRateLimiter(clock, max_failures=2, max_global_failures=3)
    stamp = limiter.reserve("a")
    assert stamp is not None
    limiter.cancel("a", stamp)
    assert limiter.tracked_keys() == 0
    assert limiter.reserve("a") and limiter.reserve("a")
    assert limiter.reserve("a") is None  # per-key cap
    assert limiter.reserve("b") is not None
    assert limiter.reserve("c") is None  # global cap
    assert limiter.is_blocked("d")
    clock.advance(timedelta(minutes=10))
    assert not limiter.is_blocked("a")


# --- 3. atomic reservation + verify gate --------------------------------------


def test_reserve_is_atomic_under_concurrency() -> None:
    limiter = LoginRateLimiter(FixedClock(datetime(2026, 1, 1, tzinfo=UTC)))
    results: list[object] = []
    barrier = threading.Barrier(16)

    def attempt() -> None:
        barrier.wait()
        results.append(limiter.reserve("same-ip"))

    threads = [threading.Thread(target=attempt) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(r is not None for r in results) == 5


def test_busy_verify_gate_returns_429(env: Env) -> None:
    app = env.app()
    gate = VerifyGate(slots=1, wait_s=0.01)
    app.state.verify_gate = gate
    client = TestClient(app, follow_redirects=False)
    assert gate.acquire()
    try:
        for _ in range(6):  # refused attempts are not counted as failures
            response = login(client)
            assert response.status_code == 429
            assert "Trop de tentatives simultanées" in response.text
    finally:
        gate.release()
    assert login(client).status_code == 303


def test_failed_attempt_stays_counted(client: TestClient) -> None:
    limiter: LoginRateLimiter = client.app.state.login_limiter  # type: ignore[attr-defined]
    login(client, "wrong")
    assert limiter.tracked_keys() == 1
    assert login(client).status_code == 303
    assert limiter.tracked_keys() == 0


# --- 4. logout revokes sessions -----------------------------------------------


def test_cookie_captured_before_logout_is_rejected(env: Env) -> None:
    app = env.app()
    client = TestClient(app, follow_redirects=False)
    response = login(client)
    assert f"Max-Age={SESSION_MAX_AGE_S}" in response.headers["set-cookie"]
    assert SESSION_MAX_AGE_S == 7 * 24 * 3600
    stolen = dict(client.cookies)
    assert client.get("/").status_code == 200

    token = csrf_from(client, "/")
    assert client.post("/logout", data={"csrf_token": token}).status_code == 303

    replay = TestClient(app, follow_redirects=False, cookies=stolen)
    assert replay.get("/").status_code == 303
    assert replay.get("/healthz").json() == {"status": "ok", "db": True}
    # Logging in again right away works.
    assert login(client).status_code == 303
    assert client.get("/").status_code == 200


def test_anonymous_logout_does_not_revoke(env: Env) -> None:
    app = env.app()
    owner = TestClient(app, follow_redirects=False)
    login(owner)
    stranger = TestClient(app, follow_redirects=False)
    token = csrf_from(stranger, "/login")
    assert stranger.post("/logout", data={"csrf_token": token}).status_code == 303
    assert owner.get("/").status_code == 200


def test_session_expires_after_seven_days(env: Env, auth_client: TestClient) -> None:
    env.clock.advance(timedelta(days=6))
    assert auth_client.get("/").status_code == 200
    env.clock.advance(timedelta(days=1, seconds=1))
    assert auth_client.get("/").status_code == 303
