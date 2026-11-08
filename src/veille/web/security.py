"""Security primitives: CSRF tokens and HTTP security headers."""

from __future__ import annotations

import hmac
import logging
import secrets

from fastapi import HTTPException, Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger(__name__)

CSRF_SESSION_KEY = "csrf"
CSRF_FORM_FIELD = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)

SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"same-origin"),
    (b"x-frame-options", b"DENY"),
)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def get_csrf_token(request: Request) -> str:
    """Return the session's CSRF token, creating one if needed (also pre-login)."""
    token = request.session.get(CSRF_SESSION_KEY)
    if not isinstance(token, str) or not token:
        token = new_token()
        request.session[CSRF_SESSION_KEY] = token
    return token


def tokens_match(expected: str | None, provided: str | None) -> bool:
    if not expected or not provided:
        return False
    return hmac.compare_digest(expected.encode(), provided.encode())


async def csrf_protect(request: Request) -> None:
    """App-wide dependency: reject unsafe requests without a valid CSRF token."""
    if request.method not in UNSAFE_METHODS:
        return
    expected = request.session.get(CSRF_SESSION_KEY)
    provided: str | None = request.headers.get(CSRF_HEADER)
    if not provided:
        content_type = request.headers.get("content-type", "")
        if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            form = await request.form()
            value = form.get(CSRF_FORM_FIELD)
            provided = value if isinstance(value, str) else None
    if not tokens_match(expected if isinstance(expected, str) else None, provided):
        logger.warning("CSRF check failed for %s %s", request.method, request.url.path)
        raise HTTPException(status_code=403, detail="Jeton CSRF manquant ou invalide.")


class SecurityHeadersMiddleware:
    """Pure ASGI middleware adding security headers to every HTTP response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [
                    (k, v)
                    for k, v in message.get("headers", [])
                    if k.lower() not in {name for name, _ in SECURITY_HEADERS}
                ]
                headers.extend(SECURITY_HEADERS)
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)
