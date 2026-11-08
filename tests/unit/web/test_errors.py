"""Regression tests: 500 responses keep the security headers; page numbers are clamped."""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from fastapi.testclient import TestClient

from veille.web.deps import MAX_PAGE, parse_page

from .conftest import Env, login


def test_500_has_security_headers_and_french_page(env: Env) -> None:
    client = TestClient(env.app(), follow_redirects=False, raise_server_exceptions=False)
    login(client)

    def boom(**kwargs: Any) -> list[Any]:
        raise RuntimeError("disk on fire")

    env.store.list_themes = boom  # type: ignore[method-assign]
    response = client.get("/themes")
    assert response.status_code == 500
    assert "Une erreur interne est survenue" in response.text
    assert "disk on fire" not in response.text
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 1),
        ("", 1),
        ("abc", 1),
        ("-4", 1),
        ("0", 1),
        ("7", 7),
        ("999999999999999999", MAX_PAGE),
        ("9" * 5000, 1),
    ],
)
def test_parse_page_clamped(raw: str | None, expected: int) -> None:
    assert parse_page(raw) == expected


@pytest.mark.parametrize("path", ["/archive", "/search?q=sujet"])
def test_huge_page_number_is_not_a_500(env: Env, auth_client: TestClient, path: str) -> None:
    theme = env.store.create_theme("Climat", "", [])
    env.store.add_article(theme, date(2026, 11, 8), title="Un sujet")
    sep = "&" if "?" in path else "?"
    response = auth_client.get(f"{path}{sep}page=999999999999999999")
    assert response.status_code == 200
