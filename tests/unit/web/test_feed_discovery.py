from __future__ import annotations

import threading
from collections.abc import Sequence

from veille.contracts import CollectError, FeedCandidate, Theme

from .conftest import Env, login, post

CANDIDATE = FeedCandidate("https://blog.example/feed", "Blog <b>", "blog.example", "Dernière")


def _client(env: Env, discoverer: object) -> object:
    env_app = env.app()
    env_app.state.feed_discoverer = discoverer
    from fastapi.testclient import TestClient

    client = TestClient(env_app, follow_redirects=False)
    assert login(client).status_code == 303
    return client


def _theme(env: Env) -> Theme:
    return env.store.create_theme("Azure", "", ["azure"])


def test_discover_lists_candidates_escaped_and_excludes_known(env: Env) -> None:
    theme = _theme(env)
    env.store.add_source(theme.id, "https://known.example/feed", "Known")
    seen: list[Sequence[str]] = []

    def discoverer(t: Theme, exclude: Sequence[str]) -> list[FeedCandidate]:
        seen.append(exclude)
        return [CANDIDATE]

    client = _client(env, discoverer)
    r = post(client, f"/themes/{theme.id}/discover-feeds", headers={"HX-Request": "true"})
    assert r.status_code == 200
    assert 'value="https://blog.example/feed"' in r.text
    assert "Blog &lt;b&gt;" in r.text and "<html" not in r.text
    assert seen == [["https://known.example/feed"]]
    full = post(client, f"/themes/{theme.id}/discover-feeds")
    assert "<html" in full.text and "https://blog.example/feed" in full.text


def test_discover_without_results_or_discoverer(env: Env) -> None:
    theme = _theme(env)
    client = _client(env, lambda t, e: [])
    assert "Aucun flux RSS trouvé" in post(client, f"/themes/{theme.id}/discover-feeds").text
    client = _client(env, None)
    assert "indisponible" in post(client, f"/themes/{theme.id}/discover-feeds").text


def test_discover_failure_and_busy(env: Env) -> None:
    theme = _theme(env)

    def boom(t: Theme, e: Sequence[str]) -> list[FeedCandidate]:
        raise RuntimeError("x")

    client = _client(env, boom)
    assert "a échoué" in post(client, f"/themes/{theme.id}/discover-feeds").text
    app = client.app  # type: ignore[attr-defined]
    lock: threading.Lock = app.state.discovery_lock
    lock.acquire()
    try:
        assert "déjà en cours" in post(client, f"/themes/{theme.id}/discover-feeds").text
    finally:
        lock.release()


def test_bulk_add_validates_each_feed(env: Env) -> None:
    theme = _theme(env)
    env.validator = lambda url: (
        (_ for _ in ()).throw(CollectError("Le flux ne répond pas")) if "bad" in url else "Titre"
    )
    client = _client(env, lambda t, e: [])
    ok = post(client, f"/themes/{theme.id}/sources/bulk", {"urls": ["https://a.example/feed"]})
    assert ok.status_code == 303
    assert [s.label for s in env.store.list_sources(theme.id)] == ["Titre"]
    bad = post(
        client,
        f"/themes/{theme.id}/sources/bulk",
        {"urls": ["https://a.example/feed", "https://bad.example/rss"]},
    )
    assert bad.status_code == 400 and "ne répond pas" in bad.text
    assert len(env.store.list_sources(theme.id)) == 1
    none = post(client, f"/themes/{theme.id}/sources/bulk", {})
    assert none.status_code == 400 and "Cochez au moins un flux" in none.text
