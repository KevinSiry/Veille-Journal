"""Password verification, login rate limiting and the authentication dependency."""

from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Request

from veille.contracts import Clock

logger = logging.getLogger(__name__)

AUTH_SESSION_KEY = "auth"
SESSION_MAX_AGE_S = 30 * 24 * 3600
MIN_SECRET_LENGTH = 32

_hasher = PasswordHasher()


class AuthRequiredError(Exception):
    """Raised by :func:`require_auth`; turned into a redirect to /login."""


def verify_password(password_hash: str, password: str) -> bool:
    """Constant-time argon2 verification. An empty hash never matches."""
    if not password_hash or not password:
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def is_authenticated(request: Request) -> bool:
    return request.session.get(AUTH_SESSION_KEY) is True


def require_auth(request: Request) -> None:
    if not is_authenticated(request):
        raise AuthRequiredError


class LoginRateLimiter:
    """In-memory, thread-safe limiter: ``max_failures`` failures per ``window`` per IP.

    The key is ``request.client.host``: behind a reverse proxy, uvicorn must run with
    ``--proxy-headers`` (and a trusted ``--forwarded-allow-ips``) so it is the real IP.
    """

    def __init__(
        self, clock: Clock, *, max_failures: int = 5, window: timedelta = timedelta(minutes=10)
    ) -> None:
        self._clock = clock
        self._max = max_failures
        self._window = window
        self._failures: dict[str, deque[datetime]] = {}
        self._lock = threading.Lock()

    def _prune(self, key: str, now: datetime) -> deque[datetime]:
        bucket = self._failures.setdefault(key, deque())
        while bucket and now - bucket[0] >= self._window:
            bucket.popleft()
        if not bucket:
            del self._failures[key]
            return deque()
        return bucket

    def is_blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._prune(key, self._clock.now())) >= self._max

    def record_failure(self, key: str) -> None:
        with self._lock:
            now = self._clock.now()
            self._prune(key, now)
            self._failures.setdefault(key, deque()).append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
