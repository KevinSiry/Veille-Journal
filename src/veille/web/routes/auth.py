"""Login and logout."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from veille.web.auth import (
    AUTH_SESSION_KEY,
    ISSUED_AT_SESSION_KEY,
    LoginRateLimiter,
    SessionRegistry,
    VerifyGate,
    is_authenticated,
    verify_password,
)
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
MSG_BUSY = "Trop de tentatives simultanées. Réessayez dans un instant."


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.get("/login")
def login_form(request: Request, settings: SettingsDep) -> Response:
    if is_authenticated(request):
        return RedirectResponse("/", status_code=303)
    configured = bool(settings.app_password_hash.get_secret_value())
    return render(request, "login.html", {"configured": configured, "error": None})


def _login_error(request: Request, message: str, status_code: int) -> Response:
    return render(
        request,
        "login.html",
        {"configured": True, "error": message},
        status_code=status_code,
    )


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
    gate: VerifyGate = request.app.state.verify_gate
    key = _client_key(request)
    # Counted as a failure up front (atomic check + record), undone on success.
    stamp = limiter.reserve(key)
    if stamp is None:
        logger.warning("Login rate limit hit for %s", key)
        return _login_error(request, MSG_RATE_LIMITED, 429)
    if not gate.acquire():
        limiter.cancel(key, stamp)
        logger.warning("Too many concurrent login attempts (from %s)", key)
        return _login_error(request, MSG_BUSY, 429)
    try:
        ok = verify_password(password_hash, password)
    finally:
        gate.release()
    if not ok:
        logger.info("Failed login attempt from %s", key)
        return _login_error(request, MSG_LOGIN_FAILED, 401)
    limiter.cancel(key, stamp)
    limiter.reset(key)
    sessions: SessionRegistry = request.app.state.sessions
    request.session.clear()
    request.session[AUTH_SESSION_KEY] = True
    request.session[ISSUED_AT_SESSION_KEY] = sessions.issue()
    request.session[CSRF_SESSION_KEY] = new_token()
    logger.info("Successful login from %s", key)
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request) -> Response:
    if is_authenticated(request):
        # Stateless signed cookies cannot be deleted server-side: revoke every session
        # issued so far, including copies of this cookie captured elsewhere.
        sessions: SessionRegistry = request.app.state.sessions
        sessions.revoke_all()
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
