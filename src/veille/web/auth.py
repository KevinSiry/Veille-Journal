"""Password verification, login rate limiting, sessions and the authentication dependency."""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict, deque
from datetime import datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Request

from veille.contracts import Clock

logger = logging.getLogger(__name__)

AUTH_SESSION_KEY = "auth"
ISSUED_AT_SESSION_KEY = "iat"
SESSION_MAX_AGE_S = 7 * 24 * 3600
MIN_SECRET_LENGTH = 32
MIN_SECRET_DISTINCT_CHARS = 12
ARGON2ID_PREFIX = "$argon2id$"
# Substrings that betray a copied placeholder instead of a generated secret.
WEAK_SECRET_MARKERS = ("remplacer", "change", "example", "secret-aleatoire")

GENERATE_SECRET_HINT = (  # help text, not a secret
    'générez-en un avec : python -c "import secrets; print(secrets.token_urlsafe(48))"'  # noqa: S105
)
GENERATE_HASH_HINT = (
    "générez-le avec : veille hash-password (ou uv run veille hash-password) "
    "et collez-le entre apostrophes simples dans le fichier .env"
)

_hasher = PasswordHasher()


class AuthRequiredError(Exception):
    """Raised by :func:`require_auth`; turned into a redirect to /login."""


# ---------------------------------------------------------------- configuration


def check_session_secret(secret: str) -> None:
    """Raise ``ValueError`` (French message) unless ``secret`` looks randomly generated."""
    if len(secret) < MIN_SECRET_LENGTH:
        raise ValueError(
            f"SESSION_SECRET est vide ou trop court ({MIN_SECRET_LENGTH} caractères minimum) : "
            f"{GENERATE_SECRET_HINT}"
        )
    lowered = secret.casefold()
    if any(marker in lowered for marker in WEAK_SECRET_MARKERS):
        raise ValueError(
            "SESSION_SECRET contient encore la valeur d'exemple : " + GENERATE_SECRET_HINT
        )
    if len(set(secret)) < MIN_SECRET_DISTINCT_CHARS:
        raise ValueError(
            f"SESSION_SECRET n'est pas aléatoire (moins de {MIN_SECRET_DISTINCT_CHARS} "
            f"caractères différents) : {GENERATE_SECRET_HINT}"
        )


def check_password_hash(password_hash: str) -> None:
    """Raise ``ValueError`` unless the hash is empty (login disabled) or argon2id."""
    if password_hash and not password_hash.startswith(ARGON2ID_PREFIX):
        raise ValueError(
            f"APP_PASSWORD_HASH doit être vide ou commencer par {ARGON2ID_PREFIX} : "
            f"{GENERATE_HASH_HINT}"
        )


# ------------------------------------------------------------------- passwords


def verify_password(password_hash: str, password: str) -> bool:
    """Constant-time argon2 verification. An empty hash never matches."""
    if not password_hash or not password:
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


class VerifyGate:
    """Caps concurrent (CPU and memory hungry) argon2 verifications."""

    def __init__(self, slots: int = 2, wait_s: float = 2.0) -> None:
        self._semaphore = threading.BoundedSemaphore(slots)
        self._wait_s = wait_s

    def acquire(self) -> bool:
        return self._semaphore.acquire(timeout=self._wait_s)

    def release(self) -> None:
        self._semaphore.release()


# -------------------------------------------------------------------- sessions


class SessionRegistry:
    """Process-memory session revocation: sessions issued before the last logout are
    rejected. A restart resets it, which is acceptable since sessions expire after
    :data:`SESSION_MAX_AGE_S` anyway."""

    def __init__(self, clock: Clock, max_age: timedelta = timedelta(seconds=SESSION_MAX_AGE_S)):
        self._clock = clock
        self._max_age = max_age.total_seconds()
        self._invalid_before = 0.0
        self._lock = threading.Lock()

    def issue(self) -> float:
        """Issued-at timestamp for a new session (always after the last revocation)."""
        with self._lock:
            return max(self._clock.now().timestamp(), self._invalid_before + 0.001)

    def revoke_all(self) -> None:
        with self._lock:
            self._invalid_before = max(self._invalid_before, self._clock.now().timestamp())

    def is_valid(self, issued_at: object) -> bool:
        if not isinstance(issued_at, int | float) or isinstance(issued_at, bool):
            return False
        with self._lock:
            invalid_before = self._invalid_before
        if issued_at <= invalid_before:
            return False
        return self._clock.now().timestamp() - issued_at < self._max_age


def is_authenticated(request: Request) -> bool:
    if request.session.get(AUTH_SESSION_KEY) is not True:
        return False
    registry: SessionRegistry = request.app.state.sessions
    return registry.is_valid(request.session.get(ISSUED_AT_SESSION_KEY))


def require_auth(request: Request) -> None:
    if not is_authenticated(request):
        raise AuthRequiredError


# ---------------------------------------------------------------- rate limiting


class LoginRateLimiter:
    """In-memory, thread-safe limiter.

    - at most ``max_failures`` failures per ``window`` per key (client IP);
    - at most ``max_global_failures`` failures per ``window`` across all keys, so that
      rotating (or spoofing) source addresses does not allow unlimited guessing;
    - at most ``max_keys`` keys are tracked (least recently used ones are dropped).

    An attempt is *reserved* (counted as a failure) before the password is verified, so
    concurrent requests cannot all pass the check; a success cancels the reservation.

    The key is ``request.client.host``: behind a reverse proxy, uvicorn must run with
    ``--proxy-headers`` and ``--forwarded-allow-ips`` set to the proxy's address only.
    """

    def __init__(
        self,
        clock: Clock,
        *,
        max_failures: int = 5,
        max_global_failures: int = 20,
        window: timedelta = timedelta(minutes=10),
        max_keys: int = 10_000,
    ) -> None:
        self._clock = clock
        self._max = max_failures
        self._max_global = max_global_failures
        self._window = window
        self._max_keys = max_keys
        self._failures: OrderedDict[str, deque[datetime]] = OrderedDict()
        self._global: deque[datetime] = deque()
        self._lock = threading.Lock()

    def _expire(self, bucket: deque[datetime], now: datetime) -> None:
        while bucket and now - bucket[0] >= self._window:
            bucket.popleft()

    def _bucket(self, key: str, now: datetime) -> deque[datetime] | None:
        bucket = self._failures.get(key)
        if bucket is None:
            return None
        self._expire(bucket, now)
        if not bucket:
            del self._failures[key]
            return None
        return bucket

    def _blocked(self, key: str, now: datetime) -> bool:
        self._expire(self._global, now)
        if len(self._global) >= self._max_global:
            return True
        bucket = self._bucket(key, now)
        return bucket is not None and len(bucket) >= self._max

    def _evict(self, now: datetime) -> None:
        if len(self._failures) < self._max_keys:
            return
        for stale in [k for k, b in self._failures.items() if not b or now - b[-1] >= self._window]:
            del self._failures[stale]
        while len(self._failures) >= self._max_keys:
            self._failures.popitem(last=False)

    def tracked_keys(self) -> int:
        with self._lock:
            return len(self._failures)

    def is_blocked(self, key: str) -> bool:
        with self._lock:
            return self._blocked(key, self._clock.now())

    def reserve(self, key: str) -> datetime | None:
        """Atomically check the limits and count one attempt; None when blocked."""
        with self._lock:
            now = self._clock.now()
            if self._blocked(key, now):
                return None
            bucket = self._failures.get(key)
            if bucket is None:
                self._evict(now)
                bucket = self._failures[key] = deque()
            self._failures.move_to_end(key)
            bucket.append(now)
            self._global.append(now)
            return now

    def cancel(self, key: str, stamp: datetime) -> None:
        """Undo one :meth:`reserve` (the attempt did not take place or succeeded)."""
        with self._lock:
            for bucket in (self._failures.get(key), self._global):
                if bucket is not None and stamp in bucket:
                    bucket.remove(stamp)
            if not self._failures.get(key, True):
                del self._failures[key]

    def reset(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
