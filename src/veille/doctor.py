"""``veille doctor``: checks that the database, Ollama, its models and SearXNG answer."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from veille.config import Settings


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str


def _has_model(available: list[str], wanted: str) -> bool:
    wanted_full = wanted if ":" in wanted else f"{wanted}:latest"
    return wanted in available or wanted_full in available


def check_database(settings: Settings) -> Check:
    from veille.db import open_store

    try:
        store = open_store(settings.database_path)
        try:
            ok = store.ping()
        finally:
            store.close()
    except Exception as exc:
        return Check("Base de données", False, f"{settings.database_path} : {exc}")
    return Check("Base de données", ok, str(settings.database_path))


def check_ollama(settings: Settings, client: httpx.Client) -> list[Check]:
    try:
        response = client.get(f"{settings.ollama_url.rstrip('/')}/api/tags")
        response.raise_for_status()
        models = [str(m.get("name", "")) for m in response.json().get("models", [])]
    except (httpx.HTTPError, ValueError) as exc:
        return [Check("Ollama", False, f"{settings.ollama_url} injoignable : {exc}")]
    checks = [Check("Ollama", True, settings.ollama_url)]
    for model in (settings.ollama_model, settings.ollama_embed_model):
        present = _has_model(models, model)
        detail = "présent" if present else "absent (le service ollama-init doit le télécharger)"
        checks.append(Check(f"Modèle {model}", present, detail))
    return checks


def check_searxng(settings: Settings, client: httpx.Client) -> Check:
    try:
        response = client.get(
            f"{settings.searxng_url.rstrip('/')}/search",
            params={"q": "test", "format": "json", "categories": "news"},
        )
        response.raise_for_status()
        count = len(response.json().get("results", []))
    except (httpx.HTTPError, ValueError) as exc:
        return Check("SearXNG", False, f"{settings.searxng_url} : {exc}")
    return Check("SearXNG", True, f"{settings.searxng_url} ({count} résultats pour « test »)")


def run_checks(settings: Settings, client: httpx.Client | None = None) -> list[Check]:
    http = client or httpx.Client(timeout=15.0)
    try:
        return [
            check_database(settings),
            *check_ollama(settings, http),
            check_searxng(settings, http),
        ]
    finally:
        if client is None:
            http.close()
