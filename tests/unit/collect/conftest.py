from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx

from veille.collect.net_safety import HostNotFoundError, SafeHttp
from veille.config import Settings

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "collect"

PUBLIC_IP = "93.184.216.34"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FakeResolver:
    """Resolves every host to a public IP unless overridden."""

    def __init__(self, mapping: dict[str, list[str]] | None = None) -> None:
        self.mapping = mapping or {}
        self.calls: list[tuple[str, int]] = []

    def __call__(self, host: str, port: int) -> list[str]:
        self.calls.append((host, port))
        if host in self.mapping:
            value = self.mapping[host]
            if not value:
                raise HostNotFoundError(f"cannot resolve {host}")
            return value
        return [PUBLIC_IP]


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


@pytest.fixture
def resolver() -> FakeResolver:
    return FakeResolver(
        {
            "internal.example.com": ["10.0.0.5"],
            "mixed.example.com": [PUBLIC_IP, "192.168.1.10"],
            "nxdomain.example.com": [],
        }
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def mock_router() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
def http(
    settings: Settings,
    resolver: FakeResolver,
    clock: FakeClock,
    mock_router: respx.MockRouter,
) -> Iterator[SafeHttp]:
    client = httpx.Client()
    safe = SafeHttp(settings, client=client, resolver=resolver, sleep=clock.sleep, clock=clock)
    yield safe
    client.close()
