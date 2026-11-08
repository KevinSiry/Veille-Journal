"""SSRF guard and a safe HTTP fetch helper for user/feed-provided URLs.

Every URL that does not come from trusted configuration (feed URLs typed by the user,
article links found in feeds or search results, redirect targets) must go through
:class:`SafeHttp`, which:

- validates the URL and every IP its host resolves to (:func:`assert_public_url`);
- follows redirects manually (at most :data:`MAX_REDIRECTS`), re-validating each hop;
- asks for uncompressed bodies, streams them and aborts above ``max_bytes``;
- applies a per-host rate limit (1 request per second);
- when it builds its own client, pins the TCP connection to an address validated at
  connect time (see :class:`GuardedBackend`), which closes the DNS-rebinding window
  between the check and the connection.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import TracebackType
from typing import Self
from urllib.parse import urljoin, urlsplit

import httpcore
import httpx

from veille.config import Settings
from veille.contracts import CollectError, UnsafeURLError

logger = logging.getLogger(__name__)

Resolver = Callable[[str, int], Sequence[str]]
"""``(host, port) -> list of IP address strings``."""

MAX_REDIRECTS = 5
RATE_LIMIT_INTERVAL_S = 1.0
ALLOWED_SCHEMES = frozenset({"http", "https"})
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_METADATA_IPS = frozenset(
    {ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("fd00:ec2::254")}
)
_EXTRA_BLOCKED_NETS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fec0::/10"),
)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


# ------------------------------------------------------------------ exceptions


class FetchTimeoutError(CollectError):
    """The remote host did not answer within the timeout."""


class ResponseTooLargeError(CollectError):
    """The response body exceeds the allowed size."""


class TooManyRedirectsError(CollectError):
    """More than MAX_REDIRECTS redirects."""


class HostNotFoundError(CollectError):
    """The host name does not resolve."""


# ------------------------------------------------------------------- resolving


def system_resolver(host: str, port: int) -> list[str]:
    """Resolve ``host`` with ``getaddrinfo`` (IPv4 + IPv6)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise HostNotFoundError(f"cannot resolve {host!r}: {exc}") from exc
    return [str(info[4][0]) for info in infos]


def _parse_ipv4_legacy(host: str) -> ipaddress.IPv4Address | None:
    """Parse inet_aton-style IPv4 literals: ``2130706433``, ``0177.0.0.1``, ``0x7f.1``."""
    parts = host.split(".")
    if not 1 <= len(parts) <= 4:
        return None
    values: list[int] = []
    for part in parts:
        if not part:
            return None
        try:
            if part.lower().startswith("0x"):
                value = int(part[2:] or "0", 16)
            elif len(part) > 1 and part.startswith("0"):
                value = int(part, 8)
            else:
                if not part.isdigit():
                    return None
                value = int(part, 10)
        except ValueError:
            return None
        values.append(value)
    *head, last = values
    if any(v > 255 for v in head) or last >= 256 ** (5 - len(values)):
        return None
    number = 0
    for v in head:
        number = (number << 8) | v
    number = (number << (8 * (5 - len(values)))) | last
    return ipaddress.IPv4Address(number)


def parse_ip_literal(host: str) -> IPAddress | None:
    """Return the IP if ``host`` is an IP literal (any notation), else None."""
    candidate = host.strip("[]").split("%", 1)[0]
    try:
        return ipaddress.ip_address(candidate)
    except ValueError:
        pass
    return _parse_ipv4_legacy(candidate)


def _embedded_ipv4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip.teredo is not None:
        return ip.teredo[1]
    if ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def is_public_ip(ip: IPAddress) -> bool:
    """True only for globally routable unicast addresses."""
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = _embedded_ipv4(ip)
        if embedded is not None and not is_public_ip(embedded):
            return False
        if ip.is_site_local:
            return False
    if ip in _METADATA_IPS:
        return False
    if any(ip in net for net in _EXTRA_BLOCKED_NETS):
        return False
    return not (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _check_ips(host: str, ips: Iterable[str]) -> list[str]:
    checked: list[str] = []
    for raw in ips:
        ip = parse_ip_literal(raw)
        if ip is None or not is_public_ip(ip):
            raise UnsafeURLError(f"host {host!r} resolves to a non-public address ({raw})")
        checked.append(str(ip))
    if not checked:
        raise HostNotFoundError(f"host {host!r} has no address")
    return checked


def resolve_public(host: str, port: int, resolver: Resolver = system_resolver) -> list[str]:
    """Resolve ``host`` and return its addresses, raising unless ALL of them are public."""
    literal = parse_ip_literal(host)
    if literal is not None:
        return _check_ips(host, [str(literal)])
    if host.lower().rstrip(".") == "localhost" or host.lower().endswith(".localhost"):
        raise UnsafeURLError(f"host {host!r} is local")
    return _check_ips(host, resolver(host, port))


def _validate_url_shape(url: str) -> tuple[str, int]:
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError as exc:
        raise UnsafeURLError(f"invalid URL {url!r}") from exc
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeURLError(f"scheme {scheme!r} not allowed")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise UnsafeURLError("credentials in URL not allowed")
    host = parts.hostname
    if not host:
        raise UnsafeURLError("URL has no host")
    if port is not None and port not in (80, 443) and not 1024 <= port <= 65535:
        raise UnsafeURLError(f"port {port} not allowed")
    effective_port = port if port is not None else (443 if scheme == "https" else 80)
    return host, effective_port


def assert_public_url(url: str, *, resolver: Resolver = system_resolver) -> None:
    """Raise :class:`UnsafeURLError` unless ``url`` is a safe public http(s) URL.

    Raises :class:`HostNotFoundError` (a ``CollectError``) when the host does not resolve.
    """
    host, port = _validate_url_shape(url)
    resolve_public(host, port, resolver)


# ------------------------------------------------------------- pinned transport


class GuardedBackend(httpcore.SyncBackend):
    """Network backend that validates the resolved addresses at connect time and
    connects to one of them (no second DNS lookup, so no DNS-rebinding window)."""

    def __init__(self, resolver: Resolver = system_resolver) -> None:
        self._resolver = resolver

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        ips = resolve_public(host, port, self._resolver)
        last_error: Exception | None = None
        for ip in ips:
            try:
                return super().connect_tcp(
                    ip,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                last_error = exc
        assert last_error is not None
        raise last_error


class GuardedTransport(httpx.HTTPTransport):
    """``httpx.HTTPTransport`` whose connection pool uses :class:`GuardedBackend`.

    Never uses proxies (a proxy would bypass the address pinning).
    """

    def __init__(self, resolver: Resolver = system_resolver) -> None:
        super().__init__(trust_env=False)
        self._pool = httpcore.ConnectionPool(
            ssl_context=httpx.create_ssl_context(),
            max_connections=20,
            max_keepalive_connections=10,
            keepalive_expiry=5.0,
            network_backend=GuardedBackend(resolver),
        )


# ------------------------------------------------------------------- SafeHttp


@dataclass(frozen=True, slots=True)
class SafeResponse:
    final_url: str
    status_code: int
    headers: Mapping[str, str]
    content: bytes = b""
    redirects: tuple[str, ...] = field(default=())

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def content_type(self) -> str:
        """Media type without parameters, lowercased (``""`` if absent)."""
        return self.headers.get("content-type", "").split(";", 1)[0].strip().lower()

    @property
    def charset(self) -> str | None:
        for param in self.headers.get("content-type", "").split(";")[1:]:
            key, _, value = param.partition("=")
            if key.strip().lower() == "charset" and value.strip():
                return value.strip().strip("\"'")
        return None


class HostRateLimiter:
    """At most one request per ``interval`` seconds per host (thread-safe)."""

    def __init__(
        self,
        interval: float = RATE_LIMIT_INTERVAL_S,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._interval = interval
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next: dict[str, float] = {}

    def wait(self, host: str) -> float:
        """Block until a request to ``host`` is allowed; return the time slept."""
        with self._lock:
            now = self._clock()
            slot = max(now, self._next.get(host, now))
            self._next[host] = slot + self._interval
        delay = slot - now
        if delay > 0:
            self._sleep(delay)
        return delay


class SafeHttp:
    """SSRF-guarded HTTP client for untrusted URLs."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
        resolver: Resolver = system_resolver,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        max_redirects: int = MAX_REDIRECTS,
    ) -> None:
        self._settings = settings
        self._resolver = resolver
        self._clock = clock
        self._max_redirects = max_redirects
        self._limiter = HostRateLimiter(clock=clock, sleep=sleep)
        self._owns_client = client is None
        self._client = client or httpx.Client(
            transport=GuardedTransport(resolver),
            trust_env=False,
            timeout=httpx.Timeout(settings.http_timeout_s),
            follow_redirects=False,
        )

    # context management
    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # requests
    def get(self, url: str, *, max_bytes: int, accept: str = "*/*") -> SafeResponse:
        return self.request("GET", url, max_bytes=max_bytes, accept=accept)

    def request(
        self,
        method: str,
        url: str,
        *,
        max_bytes: int,
        accept: str = "*/*",
        read_body: bool = True,
    ) -> SafeResponse:
        """Fetch ``url`` following redirects manually, each hop re-validated.

        Raises UnsafeURLError, or CollectError (and its subclasses) on network
        errors, timeouts, too many redirects or a body larger than ``max_bytes``.
        Non-2xx statuses are returned, not raised.
        """
        # identity: no compressed bodies, so ``max_bytes`` bounds what the server sends
        # (httpx would otherwise inflate gzip/brotli transparently: decompression bombs).
        headers = {
            "User-Agent": self._settings.user_agent,
            "Accept": accept,
            "Accept-Encoding": "identity",
        }
        current = url.strip()
        hops: list[str] = []
        for _ in range(self._max_redirects + 1):
            host, port = _validate_url_shape(current)
            resolve_public(host, port, self._resolver)
            self._limiter.wait(host.lower())
            location, response = self._one(method, current, headers, max_bytes, read_body)
            if location is None:
                assert response is not None
                return SafeResponse(
                    final_url=response.final_url,
                    status_code=response.status_code,
                    headers=response.headers,
                    content=response.content,
                    redirects=tuple(hops),
                )
            hops.append(current)
            current = urljoin(current, location)
        raise TooManyRedirectsError(f"more than {self._max_redirects} redirects for {url!r}")

    def _one(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        max_bytes: int,
        read_body: bool,
    ) -> tuple[str | None, SafeResponse | None]:
        timeout = self._settings.http_timeout_s
        try:
            with self._client.stream(
                method,
                url,
                headers=dict(headers),
                follow_redirects=False,
                timeout=timeout,
            ) as resp:
                location = resp.headers.get("location")
                if resp.status_code in _REDIRECT_CODES and location:
                    return location, None
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                if not read_body or method == "HEAD":
                    return None, SafeResponse(str(resp.url), resp.status_code, resp_headers)
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise ResponseTooLargeError(f"{url!r}: {declared} bytes > {max_bytes}")
                chunks: list[bytes] = []
                size = 0
                started = self._clock()
                for chunk in resp.iter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        raise ResponseTooLargeError(f"{url!r}: body larger than {max_bytes}")
                    if self._clock() - started > 2 * timeout:
                        raise FetchTimeoutError(f"{url!r}: body download too slow")
                    chunks.append(chunk)
                content = b"".join(chunks)
                return None, SafeResponse(str(resp.url), resp.status_code, resp_headers, content)
        except httpx.TimeoutException as exc:
            raise FetchTimeoutError(f"timeout fetching {url!r}") from exc
        except httpx.HTTPError as exc:
            raise CollectError(f"HTTP error fetching {url!r}: {exc}") from exc
