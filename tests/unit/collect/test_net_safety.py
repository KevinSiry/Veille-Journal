from __future__ import annotations

import ipaddress
import threading

import httpcore
import httpx
import pytest
import respx

from veille.collect import net_safety
from veille.collect.net_safety import (
    FetchTimeoutError,
    GuardedBackend,
    GuardedTransport,
    HostNotFoundError,
    HostRateLimiter,
    ResponseTooLargeError,
    SafeHttp,
    SafeResponse,
    TooManyRedirectsError,
    assert_public_url,
    is_public_ip,
    parse_ip_literal,
    system_resolver,
)
from veille.config import Settings
from veille.contracts import CollectError, UnsafeURLError

from .conftest import PUBLIC_IP, FakeClock, FakeResolver

# ---------------------------------------------------------------- assert_public_url


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost/feed",
        "http://LOCALHOST./feed",
        "http://foo.localhost/",
        "http://10.1.2.3/",
        "http://172.16.0.1/",
        "http://192.168.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://169.254.1.1/",
        "http://100.64.0.1/",
        "http://0.0.0.0/",
        "http://0.1.2.3/",
        "http://224.0.0.1/",
        "http://240.0.0.1/",
        "http://255.255.255.255/",
        "http://[::1]/",
        "http://[::]/",
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:7f00:1]/",
        "http://[fe80::1]/",
        "http://[fe80::1%25eth0]/",
        "http://[fc00::1]/",
        "http://[fd12:3456::1]/",
        "http://[fec0::1]/",
        "http://[ff02::1]/",
        "http://[64:ff9b::a00:1]/",
        "http://[2002:a00:1::]/",
        "http://2130706433/",
        "http://0x7f000001/",
        "http://0177.0.0.1/",
        "http://0x7f.0.0.1/",
        "http://127.1/",
        "http://10.1/",
        "http://internal.example.com/",
        "http://mixed.example.com/",
        "file:///etc/passwd",
        "ftp://example.com/file",
        "gopher://example.com/",
        "javascript:alert(1)",
        "http://user:pass@example.com/",
        "http://user@example.com/",
        "http:///nohost",
        "http://example.com:22/",
        "http://example.com:25/",
        "http://example.com:0/",
        "http://example.com:99999/",
        "http://example.com:abc/",
        "",
    ],
)
def test_assert_public_url_rejects(url: str, resolver: FakeResolver) -> None:
    with pytest.raises(UnsafeURLError):
        assert_public_url(url, resolver=resolver)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/",
        "http://example.com/feed.xml",
        "HTTPS://Example.COM/x",
        "http://example.com:80/",
        "https://example.com:443/",
        "https://example.com:8443/",
        "http://93.184.216.34/",
        "http://[2606:2800:220:1:248:1893:25c8:1946]/",
        "https://exämple.fr/",
    ],
)
def test_assert_public_url_accepts(url: str, resolver: FakeResolver) -> None:
    assert_public_url(url, resolver=resolver)


def test_literal_ip_does_not_hit_resolver(resolver: FakeResolver) -> None:
    assert_public_url("http://8.8.8.8/", resolver=resolver)
    assert resolver.calls == []


def test_resolver_receives_effective_port(resolver: FakeResolver) -> None:
    assert_public_url("https://example.com/", resolver=resolver)
    assert_public_url("http://example.com:8080/", resolver=resolver)
    assert resolver.calls == [("example.com", 443), ("example.com", 8080)]


def test_unresolvable_host_is_collect_error(resolver: FakeResolver) -> None:
    with pytest.raises(HostNotFoundError):
        assert_public_url("http://nxdomain.example.com/", resolver=resolver)


def test_resolver_garbage_ip_rejected() -> None:
    with pytest.raises(UnsafeURLError):
        assert_public_url("http://x.example/", resolver=lambda h, p: ["not-an-ip"])


@pytest.mark.parametrize(
    ("literal", "expected"),
    [
        ("2130706433", "127.0.0.1"),
        ("0x7f.1", "127.0.0.1"),
        ("0177.0.0.01", "127.0.0.1"),
        ("10.1", "10.0.0.1"),
        ("192.168.257", "192.168.1.1"),
        ("[::1]", "::1"),
        ("fe80::1%eth0", "fe80::1"),
    ],
)
def test_parse_ip_literal(literal: str, expected: str) -> None:
    assert parse_ip_literal(literal) == ipaddress.ip_address(expected)


@pytest.mark.parametrize(
    "value", ["example.com", "1.2.3.4.5", "256.1.1.1", "1..2", "09.1.1.1", "0xzz", "4294967296"]
)
def test_parse_ip_literal_non_ip(value: str) -> None:
    assert parse_ip_literal(value) is None


def test_is_public_ip_public() -> None:
    assert is_public_ip(ipaddress.ip_address("8.8.8.8"))
    assert is_public_ip(ipaddress.ip_address("2001:4860:4860::8888"))


def test_system_resolver_localhost_and_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_getaddrinfo(host: str, port: int, **kwargs: object) -> list[object]:
        if host == "bad":
            raise net_safety.socket.gaierror("nope")
        return [(2, 1, 6, "", ("1.2.3.4", port)), (10, 1, 6, "", ("::1", port, 0, 0))]

    monkeypatch.setattr(net_safety.socket, "getaddrinfo", fake_getaddrinfo)
    assert system_resolver("ok", 80) == ["1.2.3.4", "::1"]
    with pytest.raises(HostNotFoundError):
        system_resolver("bad", 80)


def test_no_address_is_host_not_found() -> None:
    with pytest.raises(HostNotFoundError):
        assert_public_url("http://x.example/", resolver=lambda h, p: [])


# ---------------------------------------------------------------- SafeHttp.get


def test_get_ok_sends_user_agent_and_accept(
    http: SafeHttp, mock_router: respx.MockRouter, settings: Settings
) -> None:
    route = mock_router.get("https://example.com/feed").respond(
        200, content=b"hello", headers={"Content-Type": "text/plain; charset=utf-8"}
    )
    resp = http.get("https://example.com/feed", max_bytes=100, accept="text/plain")
    assert resp == SafeResponse(
        "https://example.com/feed",
        200,
        resp.headers,
        b"hello",
    )
    assert resp.ok and resp.content_type == "text/plain" and resp.charset == "utf-8"
    sent = route.calls.last.request
    assert sent.headers["user-agent"] == settings.user_agent
    assert sent.headers["accept"] == "text/plain"


def test_requests_uncompressed_bodies(http: SafeHttp, mock_router: respx.MockRouter) -> None:
    route = mock_router.get("https://example.com/feed").respond(200, content=b"ok")
    http.get("https://example.com/feed", max_bytes=100)
    assert route.calls.last.request.headers["accept-encoding"] == "identity"


def test_gzip_bomb_still_bounded_by_max_bytes(
    http: SafeHttp, mock_router: respx.MockRouter
) -> None:
    """A server ignoring ``identity`` cannot inflate past ``max_bytes``."""
    import gzip

    bomb = gzip.compress(b"\0" * 1_000_000)
    assert len(bomb) < 5_000
    mock_router.get("https://example.com/bomb").respond(
        200, content=bomb, headers={"Content-Encoding": "gzip"}
    )
    with pytest.raises(ResponseTooLargeError):
        http.get("https://example.com/bomb", max_bytes=10_000)


def test_non_2xx_is_returned(http: SafeHttp, mock_router: respx.MockRouter) -> None:
    mock_router.get("https://example.com/missing").respond(404)
    resp = http.get("https://example.com/missing", max_bytes=10)
    assert resp.status_code == 404 and not resp.ok
    assert resp.content_type == "" and resp.charset is None


def test_rejects_private_before_any_request(http: SafeHttp, mock_router: respx.MockRouter) -> None:
    route = mock_router.get(url__regex=r".*").respond(200)
    with pytest.raises(UnsafeURLError):
        http.get("http://192.168.1.1/admin", max_bytes=10)
    with pytest.raises(UnsafeURLError):
        http.get("http://internal.example.com/", max_bytes=10)
    assert not route.called


def test_follows_redirects_and_revalidates(http: SafeHttp, mock_router: respx.MockRouter) -> None:
    mock_router.get("https://a.example/start").respond(301, headers={"Location": "/next"})
    mock_router.get("https://a.example/next").respond(
        302, headers={"Location": "https://b.example/final"}
    )
    mock_router.get("https://b.example/final").respond(200, content=b"ok")
    resp = http.get("https://a.example/start", max_bytes=10)
    assert resp.final_url == "https://b.example/final"
    assert resp.content == b"ok"
    assert resp.redirects == ("https://a.example/start", "https://a.example/next")


@pytest.mark.parametrize(
    "target",
    [
        "http://127.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://internal.example.com/",
        "file:///etc/passwd",
        "http://[::1]:8080/",
    ],
)
def test_redirect_to_private_rejected(
    http: SafeHttp, mock_router: respx.MockRouter, target: str
) -> None:
    mock_router.get("https://public.example/r").respond(302, headers={"Location": target})
    private = mock_router.get(url__regex=r"^(http://(127|169|internal|\[)).*").respond(200)
    with pytest.raises(UnsafeURLError):
        http.get("https://public.example/r", max_bytes=10)
    assert not private.called


def test_too_many_redirects(http: SafeHttp, mock_router: respx.MockRouter) -> None:
    for i in range(10):
        mock_router.get(f"https://loop.example/{i}").respond(302, headers={"Location": f"/{i + 1}"})
    with pytest.raises(TooManyRedirectsError):
        http.get("https://loop.example/0", max_bytes=10)
    # 1 initial request + 5 redirects
    assert sum(r.call_count for r in mock_router.routes) == 6


def test_five_redirects_allowed(http: SafeHttp, mock_router: respx.MockRouter) -> None:
    for i in range(5):
        mock_router.get(f"https://five.example/{i}").respond(302, headers={"Location": f"/{i + 1}"})
    mock_router.get("https://five.example/5").respond(200, content=b"done")
    assert http.get("https://five.example/0", max_bytes=10).content == b"done"


def test_redirect_without_location_is_final(http: SafeHttp, mock_router: respx.MockRouter) -> None:
    mock_router.get("https://example.com/x").respond(302)
    assert http.get("https://example.com/x", max_bytes=10).status_code == 302


def test_body_size_limit_streaming(http: SafeHttp, mock_router: respx.MockRouter) -> None:
    def chunks() -> object:
        for _ in range(10):
            yield b"x" * 100

    mock_router.get("https://example.com/big").mock(
        return_value=httpx.Response(200, content=chunks())  # type: ignore[arg-type]
    )
    with pytest.raises(ResponseTooLargeError):
        http.get("https://example.com/big", max_bytes=500)


def test_body_size_limit_content_length(http: SafeHttp, mock_router: respx.MockRouter) -> None:
    mock_router.get("https://example.com/big").respond(200, content=b"y" * 600)
    with pytest.raises(ResponseTooLargeError):
        http.get("https://example.com/big", max_bytes=500)
    assert http.get("https://example.com/big", max_bytes=600).content == b"y" * 600


def test_slow_body_aborted(
    settings: Settings, resolver: FakeResolver, mock_router: respx.MockRouter
) -> None:
    times = iter([0.0, 0.0, 100.0, 200.0])
    with httpx.Client() as client:
        safe = SafeHttp(
            settings,
            client=client,
            resolver=resolver,
            clock=lambda: next(times),
            sleep=lambda s: None,
        )

        def chunks() -> object:
            yield b"a"
            yield b"b"

        mock_router.get("https://slow.example/").mock(
            return_value=httpx.Response(200, content=chunks())  # type: ignore[arg-type]
        )
        with pytest.raises(FetchTimeoutError):
            safe.get("https://slow.example/", max_bytes=100)


def test_timeout_maps_to_fetch_timeout(http: SafeHttp, mock_router: respx.MockRouter) -> None:
    mock_router.get("https://example.com/slow").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(FetchTimeoutError):
        http.get("https://example.com/slow", max_bytes=10)


def test_connect_error_maps_to_collect_error(http: SafeHttp, mock_router: respx.MockRouter) -> None:
    mock_router.get("https://example.com/down").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(CollectError):
        http.get("https://example.com/down", max_bytes=10)


def test_head_without_body(http: SafeHttp, mock_router: respx.MockRouter) -> None:
    mock_router.head("https://example.com/h").respond(200, content=b"ignored")
    resp = http.request("HEAD", "https://example.com/h", max_bytes=0, read_body=False)
    assert resp.status_code == 200 and resp.content == b""


def test_timeout_setting_is_passed(http: SafeHttp, mock_router: respx.MockRouter) -> None:
    route = mock_router.get("https://example.com/t").respond(200)
    http.get("https://example.com/t", max_bytes=10)
    timeout = route.calls.last.request.extensions["timeout"]
    assert timeout["read"] == 15.0 and timeout["connect"] == 15.0


# ---------------------------------------------------------------- rate limiting


def test_rate_limit_one_per_second_per_host(
    http: SafeHttp, mock_router: respx.MockRouter, clock: FakeClock
) -> None:
    mock_router.get(url__regex=r"https://(a|b)\.example/.*").respond(200)
    http.get("https://a.example/1", max_bytes=10)
    http.get("https://b.example/1", max_bytes=10)  # other host: no wait
    assert clock.sleeps == []
    http.get("https://a.example/2", max_bytes=10)
    assert clock.sleeps == [pytest.approx(1.0)]
    clock.now += 0.4
    http.get("https://a.example/3", max_bytes=10)
    assert clock.sleeps[-1] == pytest.approx(0.6)
    clock.now += 5
    http.get("https://a.example/4", max_bytes=10)
    assert len(clock.sleeps) == 2


def test_rate_limiter_thread_safe_reservations() -> None:
    clock = FakeClock(0.0)
    delays: list[float] = []
    lock = threading.Lock()
    limiter = HostRateLimiter(clock=clock, sleep=lambda s: None)

    def worker() -> None:
        d = limiter.wait("h")
        with lock:
            delays.append(d)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(delays) == [0.0, 1.0, 2.0, 3.0, 4.0]


# ---------------------------------------------------------------- pinning backend


def test_guarded_backend_rejects_private(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(
        httpcore.SyncBackend, "connect_tcp", lambda self, host, port, **kw: called.append(host)
    )
    backend = GuardedBackend(FakeResolver({"evil.example": ["127.0.0.1"]}))
    with pytest.raises(UnsafeURLError):
        backend.connect_tcp("evil.example", 80)
    assert called == []


def test_guarded_backend_connects_to_validated_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts: list[str] = []

    def fake_connect(self: object, host: str, port: int, **kw: object) -> str:
        attempts.append(host)
        if host == "1.1.1.1":
            raise httpcore.ConnectError("refused")
        return f"stream:{host}"

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", fake_connect)
    backend = GuardedBackend(FakeResolver({"ok.example": ["1.1.1.1", PUBLIC_IP]}))
    assert backend.connect_tcp("ok.example", 443) == f"stream:{PUBLIC_IP}"  # type: ignore[comparison-overlap]
    assert attempts == ["1.1.1.1", PUBLIC_IP]


def test_guarded_backend_all_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_connect(self: object, host: str, port: int, **kw: object) -> None:
        raise httpcore.ConnectError("refused")

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", fake_connect)
    with pytest.raises(httpcore.ConnectError):
        GuardedBackend(FakeResolver()).connect_tcp("ok.example", 443)


def test_default_client_uses_guarded_transport(settings: Settings) -> None:
    with SafeHttp(settings) as safe:
        transport = safe._client._transport
        assert isinstance(transport, GuardedTransport)
        assert isinstance(transport._pool._network_backend, GuardedBackend)


def test_injected_client_not_closed(settings: Settings) -> None:
    client = httpx.Client()
    SafeHttp(settings, client=client).close()
    assert not client.is_closed
    client.close()
