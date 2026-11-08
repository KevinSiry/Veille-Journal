"""FastAPI application factory for the Veille Journal web interface."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response
from starlette.types import ASGIApp

from veille.clock import SystemClock
from veille.config import Settings
from veille.contracts import LLM, Clock, FeedCandidate, Store, Theme
from veille.web.auth import (
    SESSION_MAX_AGE_S,
    AuthRequiredError,
    LoginRateLimiter,
    SessionRegistry,
    VerifyGate,
    check_password_hash,
    check_session_secret,
    require_auth,
)
from veille.web.routes import admin, journal, themes
from veille.web.routes import auth as auth_routes
from veille.web.routes.themes import KeywordSuggester
from veille.web.security import SecurityHeadersMiddleware, csrf_protect
from veille.web.templating import build_templates, render

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
SESSION_COOKIE = "veille_session"

SERVER_ERROR_PAGE = """<!doctype html>
<html lang="fr">
<head><meta charset="utf-8"><title>Erreur 500</title>
<link rel="stylesheet" href="/static/pico.min.css"></head>
<body><main class="container"><article>
<h1>Erreur 500</h1>
<p>Une erreur interne est survenue. Réessayez plus tard.</p>
<p><a href="/">Retour à l'accueil</a></p>
</article></main></body>
</html>
"""


class VeilleApp(FastAPI):
    """FastAPI app whose outermost layer adds the security headers.

    Starlette's ``ServerErrorMiddleware`` sits outside every user middleware, so a
    header middleware added with ``add_middleware`` misses the 500 responses. Wrapping
    the whole stack covers them too.
    """

    def build_middleware_stack(self) -> ASGIApp:
        return SecurityHeadersMiddleware(super().build_middleware_stack())


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


async def _auth_required_handler(request: Request, exc: Exception) -> Response:
    if _is_htmx(request):
        return Response(status_code=401, headers={"HX-Redirect": "/login"})
    return RedirectResponse("/login", status_code=303)


async def _http_error_handler(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, StarletteHTTPException)
    messages = {
        403: "Accès refusé.",
        404: "Page introuvable.",
        405: "Méthode non autorisée.",
    }
    detail = exc.detail if isinstance(exc.detail, str) and exc.status_code != 405 else None
    message = detail or messages.get(exc.status_code, "Une erreur est survenue.")
    if exc.status_code == 404 and message == "Not Found":
        message = messages[404]
    return render(
        request,
        "error.html",
        {"status_code": exc.status_code, "message": message},
        status_code=exc.status_code,
        headers=dict(exc.headers) if exc.headers else None,
    )


async def _validation_error_handler(request: Request, exc: Exception) -> Response:
    return render(
        request,
        "error.html",
        {"status_code": 400, "message": "Requête invalide."},
        status_code=400,
    )


async def _server_error_handler(request: Request, exc: Exception) -> Response:
    # Runs outside the session middleware: no template (it needs the session).
    return HTMLResponse(SERVER_ERROR_PAGE, status_code=500)


def create_app(
    settings: Settings,
    *,
    store_factory: Callable[[], Store],
    llm: LLM | None,
    feed_validator: Callable[[str], str],
    clock: Clock | None = None,
    feed_discoverer: Callable[[Theme, Sequence[str]], Sequence[FeedCandidate]] | None = None,
) -> FastAPI:
    secret = settings.session_secret.get_secret_value()
    check_session_secret(secret)
    password_hash = settings.app_password_hash.get_secret_value()
    check_password_hash(password_hash)
    if not password_hash:
        logger.warning("APP_PASSWORD_HASH is empty: login is impossible")

    app = VeilleApp(
        title="Veille Journal",
        version=settings.version,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        dependencies=[Depends(csrf_protect)],
    )
    resolved_clock: Clock = clock or SystemClock()
    app.state.settings = settings
    app.state.store_factory = store_factory
    app.state.llm = llm
    app.state.feed_validator = feed_validator
    app.state.feed_discoverer = feed_discoverer
    app.state.discovery_lock = threading.Lock()
    app.state.clock = resolved_clock
    app.state.templates = build_templates(settings.tz)
    app.state.login_limiter = LoginRateLimiter(resolved_clock)
    app.state.verify_gate = VerifyGate()
    app.state.sessions = SessionRegistry(resolved_clock)
    app.state.keyword_suggester = KeywordSuggester()

    protected = [Depends(require_auth)]
    app.include_router(auth_routes.router)
    app.include_router(admin.public_router)
    app.include_router(journal.router, dependencies=protected)
    app.include_router(themes.router, dependencies=protected)
    app.include_router(admin.router, dependencies=protected)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    app.add_exception_handler(AuthRequiredError, _auth_required_handler)
    app.add_exception_handler(StarletteHTTPException, _http_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(Exception, _server_error_handler)

    # Security headers are added by VeilleApp around the whole stack.
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        session_cookie=SESSION_COOKIE,
        max_age=SESSION_MAX_AGE_S,
        same_site="lax",
        https_only=settings.cookie_secure,
    )
    return app
