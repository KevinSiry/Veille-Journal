"""Theme management: list, create, edit, delete, sources, keyword suggestion."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Annotated, Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from veille.contracts import (
    CollectError,
    FeedCandidate,
    LLMError,
    Store,
    Theme,
    ThemeConflictError,
    UnsafeURLError,
)
from veille.pipeline.prompts import KEYWORDS_SCHEMA, KEYWORDS_SYSTEM, build_keywords_user
from veille.web.deps import SettingsDep, StoreDep, get_llm
from veille.web.templating import render

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_NAME = 100
MAX_DESCRIPTION = 2000
MAX_KEYWORD_LEN = 60
MAX_KEYWORDS = 30
MAX_SUGGESTED = 8
MAX_URL = 2000
MAX_LABEL = 200

MSG_DUPLICATE = "Un thème portant ce nom existe déjà."
MSG_NAME_REQUIRED = "Le nom du thème est obligatoire."
MSG_NAME_TOO_LONG = f"Le nom ne doit pas dépasser {MAX_NAME} caractères."
MSG_DESCRIPTION_TOO_LONG = f"La description ne doit pas dépasser {MAX_DESCRIPTION} caractères."
MSG_URL_REQUIRED = "L'adresse du flux est obligatoire."
MSG_URL_TOO_LONG = "L'adresse du flux est trop longue."
MSG_LLM_BUSY = "Le modèle est occupé ou indisponible, réessayez plus tard."
MSG_DISCOVERY_OFF = "La recherche automatique de flux est indisponible."
MSG_DISCOVERY_BUSY = "Une recherche de flux est déjà en cours, réessayez dans un instant."
MSG_DISCOVERY_FAILED = "La recherche de flux a échoué, réessayez plus tard."
MSG_DISCOVERY_EMPTY = (
    "Aucun flux RSS trouvé pour ce thème. Ajoutez des mots-clés plus précis ou un flux à la main."
)
MSG_NOTHING_SELECTED = "Cochez au moins un flux à ajouter."
MAX_BULK = 10
MSG_LLM_EMPTY = "Le modèle n'a proposé aucun mot-clé exploitable, réessayez."
MSG_SUGGEST_IN_FLIGHT = "Une suggestion est déjà en cours, réessayez dans un instant."
MSG_SOURCE_DUPLICATE = "Ce flux est déjà suivi pour ce thème."


class SuggestionBusyError(Exception):
    """A keyword suggestion is already running (possibly a timed-out one)."""


class KeywordSuggester:
    """Runs one keyword suggestion at a time in a single dedicated worker thread.

    A model call that exceeds the timeout cannot be cancelled: it keeps the slot until it
    finishes, and new suggestions are refused meanwhile instead of piling up threads.
    """

    def __init__(self) -> None:
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="keywords")
        self._slot = threading.Lock()

    def run(self, call: Callable[[], dict[str, Any]], *, timeout: float) -> dict[str, Any]:
        if not self._slot.acquire(blocking=False):
            raise SuggestionBusyError
        try:
            future = self._pool.submit(call)
        except BaseException:
            self._slot.release()
            raise
        future.add_done_callback(self._release)
        return future.result(timeout=timeout)

    def _release(self, future: Future[dict[str, Any]]) -> None:
        self._slot.release()


def normalize_keywords(
    values: Iterable[object], *, limit: int, max_len: int = MAX_KEYWORD_LEN
) -> list[str]:
    """Keep non-empty strings, stripped, length-capped, de-duplicated (case-insensitive)."""
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            continue
        keyword = " ".join(value.split())[:max_len].strip()
        if not keyword or keyword.casefold() in seen:
            continue
        seen.add(keyword.casefold())
        result.append(keyword)
        if len(result) >= limit:
            break
    return result


def parse_keywords(raw: str) -> list[str]:
    return normalize_keywords(raw.replace("\n", ",").split(","), limit=MAX_KEYWORDS)


def _validate_theme_form(name: str, description: str) -> str | None:
    if not name:
        return MSG_NAME_REQUIRED
    if len(name) > MAX_NAME:
        return MSG_NAME_TOO_LONG
    if len(description) > MAX_DESCRIPTION:
        return MSG_DESCRIPTION_TOO_LONG
    return None


def _get_theme_or_404(store: Store, theme_id: int) -> Theme:
    theme = store.get_theme(theme_id)
    if theme is None:
        raise HTTPException(status_code=404, detail="Thème introuvable.")
    return theme


def _render_list(
    request: Request,
    store: Store,
    *,
    error: str | None = None,
    form: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    themes = store.list_themes()
    return render(
        request,
        "themes.html",
        {
            "themes": themes,
            "error": error,
            "form": form or {"name": "", "description": "", "keywords": ""},
        },
        status_code=status_code,
    )


def _render_edit(
    request: Request,
    store: Store,
    theme: Theme,
    *,
    error: str | None = None,
    source_error: str | None = None,
    form: dict[str, Any] | None = None,
    source_form: dict[str, str] | None = None,
    discovery: dict[str, Any] | None = None,
    status_code: int = 200,
) -> Response:
    return render(
        request,
        "theme_edit.html",
        {
            "theme": theme,
            "discovery": discovery,
            "sources": store.list_sources(theme.id),
            "error": error,
            "source_error": source_error,
            "form": form
            or {
                "name": theme.name,
                "description": theme.description,
                "keywords": ", ".join(theme.keywords),
                "active": theme.active,
            },
            "source_form": source_form or {"url": "", "label": ""},
        },
        status_code=status_code,
    )


@router.get("/themes")
def list_themes(request: Request, store: StoreDep) -> Response:
    return _render_list(request, store)


@router.post("/themes")
def create_theme(
    request: Request,
    store: StoreDep,
    name: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    keywords: Annotated[str, Form()] = "",
) -> Response:
    name, description = name.strip(), description.strip()
    form = {"name": name, "description": description, "keywords": keywords}
    error = _validate_theme_form(name, description)
    if error:
        return _render_list(request, store, error=error, form=form, status_code=400)
    try:
        theme = store.create_theme(name, description, parse_keywords(keywords))
    except ThemeConflictError:
        return _render_list(request, store, error=MSG_DUPLICATE, form=form, status_code=409)
    logger.info("Theme %s created", theme.id)
    return RedirectResponse(f"/themes/{theme.id}", status_code=303)


@router.post("/themes/suggest-keywords")
def suggest_keywords(
    request: Request,
    settings: SettingsDep,
    name: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    keywords: Annotated[str, Form()] = "",
) -> Response:
    name, description = name.strip()[:MAX_NAME], description.strip()[:MAX_DESCRIPTION]
    current = parse_keywords(keywords)
    error: str | None = None
    llm = get_llm(request)
    if not name:
        error = "Indiquez d'abord le nom du thème."
    elif llm is None:
        error = "La suggestion de mots-clés est indisponible (aucun modèle configuré)."
    else:
        call: Callable[[], dict[str, Any]] = lambda: llm.chat_json(  # noqa: E731
            KEYWORDS_SYSTEM, build_keywords_user(name, description), KEYWORDS_SCHEMA
        )
        suggester: KeywordSuggester = request.app.state.keyword_suggester
        try:
            result = suggester.run(call, timeout=settings.keyword_suggest_timeout_s)
        except SuggestionBusyError:
            logger.info("Keyword suggestion refused: another one is in flight")
            error = MSG_SUGGEST_IN_FLIGHT
        except (LLMError, FuturesTimeoutError, TimeoutError) as exc:
            logger.warning("Keyword suggestion failed: %s", exc)
            error = MSG_LLM_BUSY
        except Exception:
            logger.exception("Unexpected keyword suggestion failure")
            error = MSG_LLM_BUSY
        else:
            raw = result.get("keywords") if isinstance(result, dict) else None
            suggested = normalize_keywords(
                raw if isinstance(raw, list) else [], limit=MAX_SUGGESTED
            )
            if suggested:
                current = normalize_keywords([*current, *suggested], limit=MAX_KEYWORDS)
            else:
                error = MSG_LLM_EMPTY
    return render(
        request,
        "_keywords_field.html",
        {"keywords_value": ", ".join(current), "suggest_error": error},
    )


@router.get("/themes/{theme_id}")
def edit_theme_form(request: Request, store: StoreDep, theme_id: int) -> Response:
    return _render_edit(request, store, _get_theme_or_404(store, theme_id))


@router.post("/themes/{theme_id}")
def update_theme(
    request: Request,
    store: StoreDep,
    theme_id: int,
    name: Annotated[str, Form()] = "",
    description: Annotated[str, Form()] = "",
    keywords: Annotated[str, Form()] = "",
    active: Annotated[str | None, Form()] = None,
) -> Response:
    theme = _get_theme_or_404(store, theme_id)
    name, description = name.strip(), description.strip()
    is_active = active is not None
    form = {"name": name, "description": description, "keywords": keywords, "active": is_active}
    error = _validate_theme_form(name, description)
    if error:
        return _render_edit(request, store, theme, error=error, form=form, status_code=400)
    try:
        store.update_theme(
            theme_id,
            name=name,
            description=description,
            keywords=parse_keywords(keywords),
            active=is_active,
        )
    except ThemeConflictError:
        return _render_edit(request, store, theme, error=MSG_DUPLICATE, form=form, status_code=409)
    return RedirectResponse(f"/themes/{theme_id}?saved=1", status_code=303)


@router.get("/themes/{theme_id}/delete")
def delete_theme_confirm(request: Request, store: StoreDep, theme_id: int) -> Response:
    theme = _get_theme_or_404(store, theme_id)
    return render(request, "theme_delete.html", {"theme": theme})


@router.post("/themes/{theme_id}/delete")
def delete_theme(request: Request, store: StoreDep, theme_id: int) -> Response:
    _get_theme_or_404(store, theme_id)
    store.delete_theme(theme_id)
    logger.info("Theme %s deleted", theme_id)
    return RedirectResponse("/themes", status_code=303)


@router.post("/themes/{theme_id}/sources")
def add_source(
    request: Request,
    store: StoreDep,
    theme_id: int,
    url: Annotated[str, Form()] = "",
    label: Annotated[str, Form()] = "",
) -> Response:
    theme = _get_theme_or_404(store, theme_id)
    url, label = url.strip(), label.strip()[:MAX_LABEL]
    source_form = {"url": url, "label": label}
    error: str | None = None
    if not url:
        error = MSG_URL_REQUIRED
    elif len(url) > MAX_URL:
        error = MSG_URL_TOO_LONG
    elif any(source.url == url for source in store.list_sources(theme_id)):
        # Checked before validating, to avoid a useless fetch.
        error = MSG_SOURCE_DUPLICATE
    else:
        validator: Callable[[str], str] = request.app.state.feed_validator
        try:
            title = validator(url)
        except (CollectError, UnsafeURLError) as exc:
            error = str(exc) or "Ce flux n'a pas pu être validé."
        else:
            try:
                store.add_source(theme_id, url, label or title.strip()[:MAX_LABEL] or url)
            except ValueError:
                # Concurrent insert of the same URL (UNIQUE constraint).
                error = MSG_SOURCE_DUPLICATE
            else:
                return RedirectResponse(f"/themes/{theme_id}#sources", status_code=303)
    return _render_edit(
        request, store, theme, source_error=error, source_form=source_form, status_code=400
    )


@router.post("/themes/{theme_id}/sources/{source_id}/delete")
def delete_source(request: Request, store: StoreDep, theme_id: int, source_id: int) -> Response:
    _get_theme_or_404(store, theme_id)
    if all(source.id != source_id for source in store.list_sources(theme_id)):
        raise HTTPException(status_code=404, detail="Source introuvable.")
    store.delete_source(source_id)
    return RedirectResponse(f"/themes/{theme_id}#sources", status_code=303)


def _discover(request: Request, store: Store, theme: Theme) -> dict[str, Any]:
    """Run feed discovery once at a time; returns the template context of the result."""
    discoverer: Callable[[Theme, Sequence[str]], Sequence[FeedCandidate]] | None = (
        request.app.state.feed_discoverer
    )
    if discoverer is None:
        return {"candidates": [], "error": MSG_DISCOVERY_OFF}
    lock: threading.Lock = request.app.state.discovery_lock
    if not lock.acquire(blocking=False):
        return {"candidates": [], "error": MSG_DISCOVERY_BUSY}
    try:
        known = [source.url for source in store.list_sources(theme.id)]
        candidates = list(discoverer(theme, known))
    except Exception:
        logger.exception("Feed discovery failed for theme %s", theme.id)
        return {"candidates": [], "error": MSG_DISCOVERY_FAILED}
    finally:
        lock.release()
    return {"candidates": candidates, "error": None if candidates else MSG_DISCOVERY_EMPTY}


@router.post("/themes/{theme_id}/discover-feeds")
def discover_feeds(request: Request, store: StoreDep, theme_id: int) -> Response:
    theme = _get_theme_or_404(store, theme_id)
    discovery = _discover(request, store, theme)
    if request.headers.get("HX-Request") == "true":
        return render(request, "_feed_candidates.html", {"theme": theme, "discovery": discovery})
    return _render_edit(request, store, theme, discovery=discovery)


@router.post("/themes/{theme_id}/sources/bulk")
def add_sources_bulk(
    request: Request,
    store: StoreDep,
    theme_id: int,
    urls: Annotated[list[str] | None, Form()] = None,
) -> Response:
    theme = _get_theme_or_404(store, theme_id)
    wanted = [u.strip() for u in (urls or []) if u.strip()][:MAX_BULK]
    if not wanted:
        return _render_edit(
            request, store, theme, source_error=MSG_NOTHING_SELECTED, status_code=400
        )
    validator: Callable[[str], str] = request.app.state.feed_validator
    known = {source.url for source in store.list_sources(theme_id)}
    errors: list[str] = []
    for url in dict.fromkeys(wanted):
        if url in known or len(url) > MAX_URL:
            continue
        try:  # re-validated: the form content comes from the client
            title = validator(url)
            store.add_source(theme_id, url, title.strip()[:MAX_LABEL] or url)
        except (CollectError, UnsafeURLError) as exc:
            errors.append(f"{url} : {exc}")
        except ValueError:
            continue  # added concurrently
    if errors:
        return _render_edit(
            request,
            store,
            theme,
            source_error="Certains flux n'ont pas pu être ajoutés : " + " ; ".join(errors),
            status_code=400,
        )
    return RedirectResponse(f"/themes/{theme_id}#sources", status_code=303)
