"""Regression tests: duplicate feeds, single-flight keyword suggestion."""

from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import Env, post

# --- duplicate feed -----------------------------------------------------------


def test_duplicate_source_is_a_400_without_fetch(env: Env, auth_client: TestClient) -> None:
    theme = env.store.create_theme("Climat", "", [])
    url = "https://example.org/feed.xml"
    assert post(auth_client, f"/themes/{theme.id}/sources", {"url": url}).status_code == 303
    calls: list[str] = []

    def counting(value: str) -> str:
        calls.append(value)
        return "Flux"

    env.validator = counting
    response = post(auth_client, f"/themes/{theme.id}/sources", {"url": url})
    assert response.status_code == 400
    assert "Ce flux est déjà suivi pour ce thème." in response.text
    assert calls == []
    assert len(env.store.list_sources(theme.id)) == 1


def test_duplicate_source_race_is_a_400(env: Env, auth_client: TestClient) -> None:
    theme = env.store.create_theme("Climat", "", [])
    url = "https://example.org/feed.xml"

    def racing(value: str) -> str:
        env.store.add_source(theme.id, value, "inséré entre-temps")
        return "Flux"

    env.validator = racing
    response = post(auth_client, f"/themes/{theme.id}/sources", {"url": url})
    assert response.status_code == 400
    assert "Ce flux est déjà suivi pour ce thème." in response.text
