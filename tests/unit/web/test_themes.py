from __future__ import annotations

import time

from fastapi.testclient import TestClient

from veille.contracts import CollectError, LLMError, LLMUnavailableError, UnsafeURLError
from veille.pipeline.prompts import KEYWORDS_SYSTEM
from veille.web.routes.themes import normalize_keywords, parse_keywords

from .conftest import Env, csrf_from, make_settings, post
from .fakes import FakeLLM


def test_theme_list_and_create(auth_client: TestClient, env: Env) -> None:
    html = auth_client.get("/themes").text
    assert "Aucun thème pour l" in html
    for field in ("name", "description", "keywords"):
        assert f'<label for="{field}">' in html
    response = post(
        auth_client,
        "/themes",
        {"name": "  Climat ", "description": "Le climat", "keywords": "co2, GIEC,\nco2 , "},
    )
    assert response.status_code == 303
    theme = next(iter(env.store.themes.values()))
    assert response.headers["location"] == f"/themes/{theme.id}"
    assert theme.name == "Climat"
    assert theme.keywords == ("co2", "GIEC")
    assert "Climat" in auth_client.get("/themes").text


def test_create_duplicate_name(auth_client: TestClient, env: Env) -> None:
    env.store.create_theme("Climat", "", [])
    response = post(auth_client, "/themes", {"name": "Climat", "description": "d2"})
    assert response.status_code == 409
    assert "Un thème portant ce nom existe déjà." in response.text
    assert "d2" in response.text  # form values are kept


def test_create_validation(auth_client: TestClient) -> None:
    response = post(auth_client, "/themes", {"name": "  "})
    assert response.status_code == 400
    assert "Le nom du thème est obligatoire." in response.text
    response = post(auth_client, "/themes", {"name": "x" * 101})
    assert "ne doit pas dépasser 100" in response.text
    response = post(auth_client, "/themes", {"name": "ok", "description": "d" * 2001})
    assert "La description ne doit pas dépasser" in response.text


def test_edit_theme(auth_client: TestClient, env: Env) -> None:
    theme = env.store.create_theme("Climat", "desc", ["a", "b"])
    html = auth_client.get(f"/themes/{theme.id}").text
    assert 'value="Climat"' in html
    assert "a, b" in html
    assert 'role="switch"' in html
    assert " checked" in html
    response = post(
        auth_client,
        f"/themes/{theme.id}",
        {"name": "Climat 2", "description": "new", "keywords": "x, y"},
    )
    assert response.status_code == 303
    updated = env.store.themes[theme.id]
    assert (updated.name, updated.description, updated.keywords, updated.active) == (
        "Climat 2",
        "new",
        ("x", "y"),
        False,
    )
    html = auth_client.get(response.headers["location"]).text
    assert "Modifications enregistrées." in html
    post(auth_client, f"/themes/{theme.id}", {"name": "Climat 2", "active": "1"})
    assert env.store.themes[theme.id].active is True


def test_edit_duplicate_and_validation(auth_client: TestClient, env: Env) -> None:
    env.store.create_theme("A", "", [])
    b = env.store.create_theme("B", "", [])
    response = post(auth_client, f"/themes/{b.id}", {"name": "A"})
    assert response.status_code == 409
    assert "Un thème portant ce nom existe déjà." in response.text
    response = post(auth_client, f"/themes/{b.id}", {"name": ""})
    assert response.status_code == 400


def test_unknown_theme_404(auth_client: TestClient) -> None:
    assert auth_client.get("/themes/999").status_code == 404
    assert "Thème introuvable." in auth_client.get("/themes/999").text
    assert post(auth_client, "/themes/999", {"name": "x"}).status_code == 404
    assert auth_client.get("/themes/abc").status_code == 400


def test_delete_with_confirmation(auth_client: TestClient, env: Env) -> None:
    theme = env.store.create_theme("Climat", "", [])
    edit = auth_client.get(f"/themes/{theme.id}").text
    assert f'href="/themes/{theme.id}/delete"' in edit
    confirm = auth_client.get(f"/themes/{theme.id}/delete")
    assert confirm.status_code == 200
    assert "Supprimer le thème « Climat » ?" in confirm.text
    assert f'action="/themes/{theme.id}/delete"' in confirm.text
    assert theme.id in env.store.themes  # GET does not delete
    response = post(auth_client, f"/themes/{theme.id}/delete")
    assert response.status_code == 303
    assert response.headers["location"] == "/themes"
    assert theme.id not in env.store.themes


def test_delete_get_is_not_a_delete_with_csrf_missing(auth_client: TestClient, env: Env) -> None:
    theme = env.store.create_theme("Climat", "", [])
    assert auth_client.post(f"/themes/{theme.id}/delete").status_code == 403
    assert theme.id in env.store.themes


def test_add_source_success(auth_client: TestClient, env: Env) -> None:
    theme = env.store.create_theme("Climat", "", [])
    seen: list[str] = []
    env.validator = lambda url: seen.append(url) or "Le Monde Planète"
    response = post(auth_client, f"/themes/{theme.id}/sources", {"url": " https://ex.fr/rss "})
    assert response.status_code == 303
    assert seen == ["https://ex.fr/rss"]
    (source,) = env.store.list_sources(theme.id)
    assert (source.url, source.label) == ("https://ex.fr/rss", "Le Monde Planète")
    post(auth_client, f"/themes/{theme.id}/sources", {"url": "https://b.fr/", "label": "Perso"})
    assert {s.label for s in env.store.list_sources(theme.id)} == {"Le Monde Planète", "Perso"}
    assert "Perso" in auth_client.get(f"/themes/{theme.id}").text


def test_add_source_collect_error(auth_client: TestClient, env: Env) -> None:
    theme = env.store.create_theme("Climat", "", [])

    def fail(url: str) -> str:
        raise CollectError("Ce flux est introuvable ou n'est pas un flux RSS valide.")

    env.validator = fail
    response = post(auth_client, f"/themes/{theme.id}/sources", {"url": "https://ex.fr/x"})
    assert response.status_code == 400
    assert "Ce flux est introuvable ou n&#39;est pas un flux RSS valide." in response.text
    assert 'value="https://ex.fr/x"' in response.text
    assert env.store.list_sources(theme.id) == []


def test_add_source_unsafe_url(auth_client: TestClient, env: Env) -> None:
    theme = env.store.create_theme("Climat", "", [])

    def fail(url: str) -> str:
        raise UnsafeURLError("Adresse refusée : réseau privé.")

    env.validator = fail
    response = post(auth_client, f"/themes/{theme.id}/sources", {"url": "http://10.0.0.1/"})
    assert response.status_code == 400
    assert "Adresse refusée : réseau privé." in response.text
    assert env.store.list_sources(theme.id) == []


def test_add_source_validation(auth_client: TestClient, env: Env) -> None:
    theme = env.store.create_theme("Climat", "", [])
    response = post(auth_client, f"/themes/{theme.id}/sources", {"url": ""})
    assert "L&#39;adresse du flux est obligatoire." in response.text
    response = post(auth_client, f"/themes/{theme.id}/sources", {"url": "https://a/" + "x" * 2000})
    assert "trop longue" in response.text


def test_delete_source(auth_client: TestClient, env: Env) -> None:
    a = env.store.create_theme("A", "", [])
    b = env.store.create_theme("B", "", [])
    source = env.store.add_source(a.id, "https://ex.fr/rss", "Ex")
    # a source can only be deleted through its own theme
    assert post(auth_client, f"/themes/{b.id}/sources/{source.id}/delete").status_code == 404
    response = post(auth_client, f"/themes/{a.id}/sources/{source.id}/delete")
    assert response.status_code == 303
    assert env.store.list_sources(a.id) == []


def _suggest(client: TestClient, **data: str) -> str:
    token = csrf_from(client, "/themes")
    response = client.post(
        "/themes/suggest-keywords",
        data=data,
        headers={"X-CSRF-Token": token, "HX-Request": "true"},
    )
    assert response.status_code == 200
    return response.text


def test_suggest_keywords_success(auth_client: TestClient, env: Env) -> None:
    env.llm = FakeLLM(
        {
            "keywords": [
                " énergie ",
                "Énergie",
                42,
                "",
                "x" * 80,
                "solaire",
                "éolien",
                "nucléaire",
                "hydrogène",
                "batteries",
                "réseau",
                "stockage",
                "de trop",
            ]
        }
    )
    client = TestClient(env.app(), follow_redirects=False)
    from .conftest import login

    login(client)
    html = _suggest(client, name="Énergie", description="Transition", keywords="manuel")
    assert env.llm.calls[0][0] == KEYWORDS_SYSTEM
    assert "Nom du thème : Énergie" in env.llm.calls[0][1]
    assert '<div id="keywords-field">' in html
    assert "<html" not in html  # a fragment, not a page
    value = html.split('name="keywords"')[1].split(">", 1)[1].split("</textarea>")[0]
    keywords = value.split(", ")
    assert keywords[0] == "manuel"
    assert keywords[1:3] == ["énergie", "x" * 60]
    assert len(keywords) == 1 + 8
    assert "de trop" not in keywords
    assert "stockage" not in keywords  # 9th suggestion dropped (max 8)


def _client_with(env: Env, llm: FakeLLM | None) -> TestClient:
    from .conftest import login

    env.llm = llm
    client = TestClient(env.app(), follow_redirects=False)
    login(client)
    return client


def test_suggest_keywords_llm_errors(env: Env) -> None:
    for error in (LLMError("boom"), LLMUnavailableError("busy"), RuntimeError("weird")):
        client = _client_with(env, FakeLLM(error))
        html = _suggest(client, name="Énergie", keywords="garde")
        assert "Le modèle est occupé ou indisponible, réessayez plus tard." in html
        assert "garde" in html


def test_suggest_keywords_timeout(env: Env) -> None:
    class SlowLLM(FakeLLM):
        def chat_json(self, system, user, schema):  # type: ignore[no-untyped-def]
            time.sleep(0.5)
            return {"keywords": ["lentement"]}

    env.settings = make_settings(keyword_suggest_timeout_s=0.05)
    client = _client_with(env, SlowLLM())
    html = _suggest(client, name="Énergie")
    assert "Le modèle est occupé ou indisponible" in html
    assert "lentement" not in html


def test_suggest_keywords_bad_payload(env: Env) -> None:
    client = _client_with(env, FakeLLM({"keywords": "pas une liste"}))
    assert "aucun mot-clé exploitable" in _suggest(client, name="Énergie")


def test_suggest_keywords_llm_none(env: Env) -> None:
    client = _client_with(env, None)
    html = _suggest(client, name="Énergie")
    assert "indisponible" in html


def test_suggest_keywords_requires_name(env: Env) -> None:
    llm = FakeLLM({"keywords": ["a"]})
    client = _client_with(env, llm)
    assert "Indiquez d&#39;abord le nom" in _suggest(client, name=" ")
    assert llm.calls == []


def test_suggest_keywords_escapes_llm_output(env: Env) -> None:
    client = _client_with(env, FakeLLM({"keywords": ["</textarea><script>x</script>"]}))
    html = _suggest(client, name="Énergie")
    assert "<script>" not in html
    assert "&lt;/textarea&gt;&lt;script&gt;" in html


def test_suggest_keywords_requires_auth_and_csrf(env: Env, client: TestClient) -> None:
    response = client.post("/themes/suggest-keywords", headers={"HX-Request": "true"})
    assert response.status_code == 403  # CSRF is checked before auth
    token = csrf_from(client, "/login")
    response = client.post(
        "/themes/suggest-keywords",
        data={"name": "x"},
        headers={"HX-Request": "true", "X-CSRF-Token": token},
    )
    assert response.status_code == 401
    assert response.headers["HX-Redirect"] == "/login"


def test_suggest_button_markup(auth_client: TestClient) -> None:
    html = auth_client.get("/themes").text
    assert 'hx-post="/themes/suggest-keywords"' in html
    assert 'hx-target="#keywords-field"' in html


def test_keyword_helpers() -> None:
    assert parse_keywords("a, b\nA ,, c") == ["a", "b", "c"]
    assert normalize_keywords(["  x   y "], limit=8) == ["x y"]
    assert normalize_keywords(["a", "b", "c"], limit=2) == ["a", "b"]
