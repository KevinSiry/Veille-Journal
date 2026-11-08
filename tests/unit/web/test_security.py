from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from veille.web.security import CONTENT_SECURITY_POLICY, tokens_match

from .conftest import Env, csrf_from, post

EXPECTED_CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)


@pytest.mark.parametrize("path", ["/login", "/healthz", "/static/app.css", "/", "/nope"])
def test_security_headers_everywhere(client: TestClient, path: str) -> None:
    headers = client.get(path).headers
    assert headers["content-security-policy"] == EXPECTED_CSP == CONTENT_SECURITY_POLICY
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "same-origin"
    assert headers["x-frame-options"] == "DENY"


def test_pages_use_no_inline_script_or_style(auth_client: TestClient, env: Env) -> None:
    theme = env.store.create_theme("Climat", "", ["co2"])
    for path in ["/", "/archive", "/search?q=x", "/themes", f"/themes/{theme.id}", "/admin/runs"]:
        html = auth_client.get(path).text
        assert "<script>" not in html
        assert "style=" not in html
        assert "<style" not in html
    html = auth_client.get("/").text
    assert '<script src="/static/htmx.min.js" defer></script>' in html
    assert '"includeIndicatorStyles": false' in html
    assert '<meta name="csrf-token"' in html


def test_vendored_assets_keep_license_headers(client: TestClient) -> None:
    assert "SIL Open Font License" in client.get("/static/fonts/Inter-OFL.txt").text
    assert client.get("/static/icons.svg").status_code == 200
    assert "0BSD" in client.get("/static/htmx.min.js").text[:300]
    assert "htmx:configRequest" in client.get("/static/app.js").text


def test_csrf_missing_is_403(auth_client: TestClient, env: Env) -> None:
    response = auth_client.post("/themes", data={"name": "X"})
    assert response.status_code == 403
    assert "CSRF" in response.text
    assert env.store.themes == {}


def test_csrf_invalid_is_403(auth_client: TestClient, env: Env) -> None:
    response = auth_client.post("/themes", data={"name": "X", "csrf_token": "forged"})
    assert response.status_code == 403
    response = auth_client.post("/themes", data={"name": "X"}, headers={"X-CSRF-Token": "bad"})
    assert response.status_code == 403
    assert env.store.themes == {}


def test_csrf_valid_form_ok(auth_client: TestClient, env: Env) -> None:
    assert post(auth_client, "/themes", {"name": "X"}).status_code == 303
    assert len(env.store.themes) == 1


def test_csrf_valid_header_ok(auth_client: TestClient, env: Env) -> None:
    token = csrf_from(auth_client, "/themes")
    response = auth_client.post(
        "/themes", data={"name": "Y"}, headers={"X-CSRF-Token": token, "HX-Request": "true"}
    )
    assert response.status_code == 303


def test_csrf_json_body_without_header_is_403(auth_client: TestClient) -> None:
    assert auth_client.post("/themes", json={"name": "Z"}).status_code == 403


def test_login_requires_csrf(client: TestClient) -> None:
    assert client.post("/login", data={"password": "x"}).status_code == 403


def test_tokens_match() -> None:
    assert tokens_match("abc", "abc")
    assert not tokens_match("abc", "abd")
    assert not tokens_match(None, "abc")
    assert not tokens_match("abc", None)
