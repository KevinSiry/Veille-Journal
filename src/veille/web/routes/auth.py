"""Login and logout."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from veille.web.auth import AUTH_SESSION_KEY, LoginRateLimiter, is_authenticated, verify_password
from veille.web.deps import SettingsDep
from veille.web.security import CSRF_SESSION_KEY, new_token
from veille.web.templating import render

logger = logging.getLogger(__name__)

router = APIRouter()

MSG_NOT_CONFIGURED = (
    "L'application n'est pas configurée : aucun mot de passe n'est défini "
    "(variable APP_PASSWORD_HASH)."
)
MSG_LOGIN_FAILED = "Mot de passe incorrect."
MSG_RATE_LIMITED = "Trop de tentatives de connexion. Réessayez dans quelques minutes."


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.get("/login")
def login_form(request: Request, settings: SettingsDep) -> Response:
    if is_authenticated(request):
        return RedirectResponse("/", status_code=303)
    configured = bool(settings.app_password_hash.get_secret_value())
    return render(request, "login.html", {"configured": configured, "error": None})


@router.post("/login")
def login(
    request: Request,
    settings: SettingsDep,
    password: Annotated[str, Form()] = "",
) -> Response:
    password_hash = settings.app_password_hash.get_secret_value()
    if not password_hash:
        return render(
            request,
            "login.html",
            {"configured": False, "error": MSG_NOT_CONFIGURED},
            status_code=503,
        )
    limiter: LoginRateLimiter = request.app.state.login_limiter
    key = _client_key(request)
    if limiter.is_blocked(key):
        logger.warning("Login rate limit hit for %s", key)
        return render(
            request,
            "login.html",
            {"configured": True, "error": MSG_RATE_LIMITED},
            status_code=429,
        )
    if not verify_password(password_hash, password):
        limiter.record_failure(key)
        logger.info("Failed login attempt from %s", key)
        return render(
            request,
            "login.html",
            {"configured": True, "error": MSG_LOGIN_FAILED},
            status_code=401,
        )
    limiter.reset(key)
    request.session.clear()
    request.session[AUTH_SESSION_KEY] = True
    request.session[CSRF_SESSION_KEY] = new_token()
    logger.info("Successful login from %s", key)
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request) -> Response:
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
