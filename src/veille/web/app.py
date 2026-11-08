"""FastAPI application factory for the Veille Journal web interface."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response

from veille.clock import SystemClock
from veille.config import Settings
from veille.contracts import LLM, Clock, Store
from veille.web.auth import (
    MIN_SECRET_LENGTH,
    SESSION_MAX_AGE_S,
    AuthRequiredError,
    LoginRateLimiter,
    require_auth,
)
from veille.web.routes import admin, journal, themes
from veille.web.routes import auth as auth_routes
from veille.web.security import SecurityHeadersMiddleware, csrf_protect
from veille.web.templating import build_templates, render

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
SESSION_COOKIE = "veille_session"


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


def create_app(
    settings: Settings,
    *,
    store_factory: Callable[[], Store],
    llm: LLM | None,
    feed_validator: Callable[[str], str],
    clock: Clock | None = None,
) -> FastAPI:
    secret = settings.session_secret.get_secret_value()
    if len(secret) < MIN_SECRET_LENGTH:
        raise ValueError(
            f"SESSION_SECRET must be set and at least {MIN_SECRET_LENGTH} characters long"
        )
    if not settings.app_password_hash.get_secret_value():
        logger.warning("APP_PASSWORD_HASH is empty: login is impossible")

    app = FastAPI(
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
    app.state.clock = resolved_clock
    app.state.templates = build_templates(settings.tz)
    app.state.login_limiter = LoginRateLimiter(resolved_clock)

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

    # Order: the last added middleware is the outermost one.
    app.add_middleware(
        SessionMiddleware,
        secret_key=secret,
        session_cookie=SESSION_COOKIE,
        max_age=SESSION_MAX_AGE_S,
        same_site="lax",
        https_only=settings.cookie_secure,
    )
    app.add_middleware(SecurityHeadersMiddleware)
    return app
