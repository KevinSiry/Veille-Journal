from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from veille.web.app import create_app
from veille.web.auth import verify_password

from .conftest import PASSWORD_HASH, Env, csrf_from, login, make_settings, ok_validator
from .fakes import FakeStore


def test_login_page_has_password_field_and_csrf(client: TestClient) -> None:
    response = client.get("/login")
    assert response.status_code == 200
    assert 'type="password"' in response.text
    assert '<label for="password">' in response.text
    assert 'name="csrf_token"' in response.text


def test_login_success_sets_session_and_redirects(client: TestClient) -> None:
    response = login(client)
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    cookie = response.headers["set-cookie"]
    assert "veille_session=" in cookie
    assert "httponly" in cookie.lower()
    assert "samesite=lax" in cookie.lower()
    assert client.get("/").status_code == 200
    # already authenticated: /login redirects home
    assert client.get("/login").status_code == 303


def test_login_regenerates_csrf_token(client: TestClient) -> None:
    before = csrf_from(client, "/login")
    login(client)
    after = csrf_from(client, "/themes")
    assert before != after


def test_secure_cookie_flag(env: Env) -> None:
    env.settings = make_settings(cookie_secure=True)
    c = TestClient(env.app(), base_url="https://testserver", follow_redirects=False)
    response = login(c)
    assert "secure" in response.headers["set-cookie"].lower()


def test_login_failure(client: TestClient) -> None:
    response = login(client, "wrong")
    assert response.status_code == 401
    assert "Mot de passe incorrect." in response.text
    assert client.get("/").status_code == 303


def test_rate_limit_sixth_attempt_is_429(env: Env, client: TestClient) -> None:
    for _ in range(5):
        assert login(client, "wrong").status_code == 401
    response = login(client, "wrong")
    assert response.status_code == 429
    assert "Trop de tentatives" in response.text
    # even the right password is refused while blocked
    assert login(client).status_code == 429
    env.clock.advance(timedelta(minutes=11))
    assert login(client).status_code == 303


def test_success_resets_failures(client: TestClient) -> None:
    for _ in range(4):
        login(client, "wrong")
    assert login(client).status_code == 303
    client.cookies.clear()
    for _ in range(5):
        assert login(client, "wrong").status_code == 401


def test_logout_clears_session(auth_client: TestClient) -> None:
    token = csrf_from(auth_client, "/")
    response = auth_client.post("/logout", data={"csrf_token": token})
    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert auth_client.get("/").status_code == 303


def test_logout_requires_csrf(auth_client: TestClient) -> None:
    assert auth_client.post("/logout").status_code == 403
    assert auth_client.get("/").status_code == 200


@pytest.mark.parametrize(
    "path", ["/", "/archive", "/archive/2026-11-08", "/search", "/themes", "/admin/runs"]
)
def test_pages_require_auth(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_htmx_unauthenticated_gets_401_with_hx_redirect(client: TestClient) -> None:
    response = client.get("/themes", headers={"HX-Request": "true"})
    assert response.status_code == 401
    assert response.headers["HX-Redirect"] == "/login"


def test_public_paths(client: TestClient) -> None:
    assert client.get("/healthz").status_code == 200
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/login").status_code == 200


def test_empty_hash_makes_login_impossible(env: Env) -> None:
    env.settings = make_settings(app_password_hash=SecretStr(""))
    c = env.client()
    page = c.get("/login")
    assert "pas configurée" in page.text
    assert 'type="password"' not in page.text
    # The pre-session CSRF token is still exposed through the meta tag.
    response = c.post("/login", data={"password": "x", "csrf_token": _token(c)})
    assert response.status_code == 503
    assert "pas configurée" in response.text


def _token(c: TestClient) -> str:
    import re

    match = re.search(r'name="csrf-token" content="([^"]+)"', c.get("/login").text)
    assert match
    return match.group(1)


def test_verify_password_edge_cases() -> None:
    assert verify_password(PASSWORD_HASH, "correct horse battery staple")
    assert not verify_password(PASSWORD_HASH, "")
    assert not verify_password("", "x")
    assert not verify_password("not-a-hash", "x")


@pytest.mark.parametrize("secret", ["", "short", "x" * 31])
def test_create_app_refuses_short_secret(secret: str) -> None:
    with pytest.raises(ValueError, match="SESSION_SECRET"):
        create_app(
            make_settings(session_secret=SecretStr(secret)),
            store_factory=FakeStore,
            llm=None,
            feed_validator=ok_validator,
        )


def test_create_app_default_clock() -> None:
    app = create_app(
        make_settings(), store_factory=FakeStore, llm=None, feed_validator=ok_validator
    )
    assert TestClient(app).get("/healthz").json()["status"] == "ok"
