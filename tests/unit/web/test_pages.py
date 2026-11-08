from __future__ import annotations

from datetime import date, timedelta

from fastapi.testclient import TestClient

from veille.contracts import ArticleStatus

from .conftest import Env

TODAY = date(2026, 11, 8)


def test_today_states(auth_client: TestClient, env: Env) -> None:
    missing = env.store.create_theme("Alpha", "", [])
    empty = env.store.create_theme("Beta", "", [])
    ok = env.store.create_theme("Gamma", "", [])
    failed = env.store.create_theme("Delta", "", [])
    inactive = env.store.create_theme("Zeta", "", [])
    env.store.update_theme(inactive.id, name="Zeta", description="", keywords=[], active=False)
    env.store.add_article(empty, TODAY, status=ArticleStatus.EMPTY)
    env.store.add_article(ok, TODAY, title="Le titre du jour", body_md="Un **fait** marquant.")
    env.store.add_article(failed, TODAY, status=ArticleStatus.ERROR)
    env.store.add_article(missing, TODAY - timedelta(days=1))
    html = auth_client.get("/").text
    assert "Aujourd'hui" in html
    assert "dimanche 8 novembre 2026" in html
    assert "Pas encore généré." in html
    assert "Rien de notable aujourd&#39;hui." in html
    assert "Le titre du jour" in html
    assert "<strong>fait</strong>" in html
    assert "a échoué" in html
    assert "Zeta" not in html
    assert "version 1.2.3-test" in html


def test_today_uses_paris_day(auth_client: TestClient, env: Env) -> None:
    from datetime import UTC, datetime

    env.clock._at = datetime(2026, 11, 8, 23, 30, tzinfo=UTC)  # 00:30 on the 9th in Paris
    theme = env.store.create_theme("Alpha", "", [])
    env.store.add_article(theme, date(2026, 11, 9), title="Neuf")
    html = auth_client.get("/").text
    assert "Neuf" in html
    assert "9 novembre 2026" in html


def test_no_active_theme(auth_client: TestClient) -> None:
    assert "Aucun thème actif" in auth_client.get("/").text


def test_day_navigation(auth_client: TestClient, env: Env) -> None:
    theme = env.store.create_theme("Alpha", "", [])
    for offset in (0, 2, 5):
        env.store.add_article(theme, TODAY - timedelta(days=offset), title=f"T{offset}")
    html = auth_client.get("/?day=2026-11-06").text
    assert "T2" in html
    assert "Journal du jour" in html
    assert 'href="/?day=2026-11-03"' in html
    assert 'href="/?day=2026-11-08"' in html
    assert 'href="/" class="btn btn--ghost btn--sm">Aujourd' in html
    html = auth_client.get("/").text
    assert 'href="/?day=2026-11-06"' in html
    assert 'rel="next"' not in html


def test_invalid_day(auth_client: TestClient) -> None:
    response = auth_client.get("/?day=not-a-date")
    assert response.status_code == 404
    assert "Date invalide." in response.text
    assert auth_client.get("/archive/2026-13-45").status_code == 404


def test_unknown_page_404(auth_client: TestClient) -> None:
    response = auth_client.get("/nope")
    assert response.status_code == 404
    assert "Page introuvable." in response.text


def test_archive_pagination_and_filter(auth_client: TestClient, env: Env) -> None:
    a = env.store.create_theme("Alpha", "", [])
    b = env.store.create_theme("Beta", "", [])
    for offset in range(35):
        env.store.add_article(a, TODAY - timedelta(days=offset))
    env.store.add_article(b, date(2020, 1, 1))
    page1 = auth_client.get("/archive").text
    assert page1.count('<a class="day-tile" href="/archive/') == 30
    assert "Page 1 sur 2" in page1
    assert 'href="/archive?page=2"' in page1
    page2 = auth_client.get("/archive?page=2").text
    assert page2.count('<a class="day-tile" href="/archive/') == 6
    assert "1 janvier 2020" in page2
    assert 'href="/archive?page=1"' in page2
    filtered = auth_client.get(f"/archive?theme={b.id}").text
    assert filtered.count('<a class="day-tile" href="/archive/') == 1
    assert f'href="/archive/2020-01-01?theme={b.id}"' in filtered
    assert "Page 1" not in filtered
    # bad values are ignored
    assert auth_client.get("/archive?page=abc&theme=999").status_code == 200
    assert auth_client.get("/archive?theme=abc&page=-3").status_code == 200


def test_archive_empty(auth_client: TestClient) -> None:
    assert "Aucun article archivé" in auth_client.get("/archive").text


def test_archive_day(auth_client: TestClient, env: Env) -> None:
    a = env.store.create_theme("Alpha", "", [])
    b = env.store.create_theme("Beta", "", [])
    env.store.add_article(a, TODAY, title="Article A")
    env.store.add_article(b, TODAY, title="Article B", status=ArticleStatus.EMPTY)
    html = auth_client.get("/archive/2026-11-08").text
    assert "Article A" in html
    assert "Rien de notable" in html
    html = auth_client.get(f"/archive/2026-11-08?theme={a.id}").text
    assert "Article A" in html
    assert "Rien de notable" not in html
    assert "Aucun article ce jour-là" in auth_client.get("/archive/2001-01-01").text


def test_search_highlight_and_escaping(auth_client: TestClient, env: Env) -> None:
    theme = env.store.create_theme("<b>Th</b>", "", [])
    env.store.add_article(theme, TODAY, title="<script>alert(1)</script> nucléaire", body_md="x")
    html = auth_client.get("/search?q=nucl").text
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<mark>nucl</mark>éaire" in html
    assert "<b>Th</b>" not in html
    assert "1 résultat pour" in html


def test_search_marker_inside_markup_is_escaped(auth_client: TestClient, env: Env) -> None:
    theme = env.store.create_theme("T", "", [])
    env.store.add_article(theme, TODAY, title="a <img src=x onerror=alert(1)> b")
    html = auth_client.get("/search?q=img").text
    assert "&lt;<mark>img</mark> src=x onerror=alert(1)&gt;" in html
    assert "<img" not in html


def test_search_pagination_theme_and_order(auth_client: TestClient, env: Env) -> None:
    a = env.store.create_theme("Alpha", "", [])
    b = env.store.create_theme("Beta", "", [])
    for offset in range(25):
        env.store.add_article(a, TODAY - timedelta(days=offset), title=f"Sujet {offset:02d}")
    env.store.add_article(b, TODAY, title="Sujet beta")
    html = auth_client.get("/search?q=sujet").text
    assert "26 résultats pour" in html
    assert html.count('<p class="snippet">') == 20
    assert "q=sujet&amp;page=2" in html
    page2 = auth_client.get("/search?q=sujet&page=2").text
    assert page2.count('<p class="snippet">') == 6
    filtered = auth_client.get(f"/search?q=sujet&theme={b.id}").text
    assert "1 résultat pour" in filtered
    assert "Sujet beta" in filtered
    # results are shown in store order
    first = auth_client.get(f"/search?q=sujet&theme={a.id}").text
    assert first.index("Sujet 00") < first.index("Sujet 01")


def test_search_empty_query(auth_client: TestClient) -> None:
    html = auth_client.get("/search?q=++").text
    assert "résultat" not in html
    assert '<label for="q">' in html
