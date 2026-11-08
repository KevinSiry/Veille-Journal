"""URL canonicalisation (identity key of an item) and URL-shortener resolution."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from veille.contracts import VeilleError

if TYPE_CHECKING:
    from veille.collect.net_safety import SafeHttp

logger = logging.getLogger(__name__)

TRACKING_PARAMS = frozenset(
    {"fbclid", "gclid", "mc_cid", "mc_eid", "ref_src", "igshid"},
)
TRACKING_PREFIXES = ("utm_",)

SHORTENER_HOSTS = frozenset(
    {
        "t.co",
        "bit.ly",
        "ow.ly",
        "buff.ly",
        "tinyurl.com",
        "lnkd.in",
        "goo.gl",
        "dlvr.it",
    }
)

_DEFAULT_PORTS = {"http": 80, "https": 443}


def strip_www(host: str) -> str:
    """Lowercase ``host`` without a leading ``www.`` (shared by every host comparison)."""
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def host_key(url: str) -> str:
    """Lowercase host of ``url`` without ``www.``; '' when it has none or is unparsable."""
    try:
        return strip_www(urlsplit(url.strip()).hostname or "")
    except ValueError:
        return ""


def _idna_host(host: str) -> str:
    host = strip_www(host.lower().rstrip("."))
    if host.isascii():
        return host
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def _is_tracking(key: str) -> bool:
    lowered = key.lower()
    return lowered in TRACKING_PARAMS or lowered.startswith(TRACKING_PREFIXES)


def canonicalize(url: str) -> str:
    """Return the canonical form of ``url`` (pure, no network).

    https scheme, lowercase IDNA host without ``www.``, no default port, no userinfo,
    no fragment, no tracking params, sorted query, no trailing slash except root.
    Non-http(s) or unparsable URLs are returned stripped but otherwise unchanged.
    """
    raw = url.strip()
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError:
        return raw
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS or not parts.hostname:
        return raw
    host = _idna_host(parts.hostname)
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    if port is not None and port != _DEFAULT_PORTS[scheme] and port != 443:
        host = f"{host}:{port}"
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    query_items = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _is_tracking(k)
    ]
    query = urlencode(sorted(query_items))
    return urlunsplit(("https", host, path, query, ""))


def is_shortened(url: str) -> bool:
    return host_key(url) in SHORTENER_HOSTS


def resolve_short(url: str, http: SafeHttp) -> str:
    """Follow redirects of a known shortener URL; return the final URL.

    Non-shortener URLs are returned unchanged. On any failure, returns ``url``.
    """
    if not is_shortened(url):
        return url
    try:
        response = http.request("HEAD", url, max_bytes=0, read_body=False)
        if not response.redirects and response.status_code >= 400:
            response = http.request("GET", url, max_bytes=0, read_body=False)
    except VeilleError as exc:
        logger.info("could not resolve shortened URL %s: %s", url, exc)
        return url
    # The destination may refuse bots (403...), its URL is still the one we want.
    return response.final_url if response.redirects else url
