"""Regression tests: duplicate feeds, single-flight keyword suggestion."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from veille.contracts import LLMError
from veille.web.routes.themes import KeywordSuggester, SuggestionBusyError

from .conftest import Env, login, make_settings, post
from .fakes import FakeLLM

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


# --- single-flight keyword suggestion -----------------------------------------


def test_suggester_refuses_while_in_flight() -> None:
    suggester = KeywordSuggester()
    release = threading.Event()

    def slow() -> dict[str, Any]:
        release.wait(5)
        return {"keywords": ["x"]}

    with pytest.raises(TimeoutError):
        suggester.run(slow, timeout=0.01)
    with pytest.raises(SuggestionBusyError):
        suggester.run(lambda: {"keywords": []}, timeout=1)
    release.set()
    deadline = time.monotonic() + 5
    while True:
        try:
            assert suggester.run(lambda: {"keywords": ["ok"]}, timeout=1) == {"keywords": ["ok"]}
            break
        except SuggestionBusyError:
            assert time.monotonic() < deadline
            time.sleep(0.01)


def test_suggester_releases_after_error() -> None:
    suggester = KeywordSuggester()

    def failing() -> dict[str, Any]:
        raise LLMError("boom")

    with pytest.raises(LLMError):
        suggester.run(failing, timeout=1)
    assert suggester.run(lambda: {"a": 1}, timeout=1) == {"a": 1}


def test_suggest_route_reports_in_flight(env: Env) -> None:
    release = threading.Event()

    class BlockingLLM(FakeLLM):
        def chat_json(self, system, user, schema):  # type: ignore[no-untyped-def]
            release.wait(5)
            return {"keywords": ["tard"]}

    env.llm = BlockingLLM()
    env.settings = make_settings(keyword_suggest_timeout_s=0.05)
    client = TestClient(env.app(), follow_redirects=False)
    login(client)
    try:
        first = post(client, "/themes/suggest-keywords", {"name": "Énergie"})
        assert "Le modèle est occupé ou indisponible" in first.text
        second = post(client, "/themes/suggest-keywords", {"name": "Énergie"})
        assert "Une suggestion est déjà en cours, réessayez dans un instant." in second.text
    finally:
        release.set()
