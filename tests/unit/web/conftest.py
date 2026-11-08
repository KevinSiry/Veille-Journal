from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from veille.clock import FixedClock
from veille.config import Settings
from veille.web.app import create_app

from .fakes import FakeLLM, FakeStore

PASSWORD = "correct horse battery staple"
# Cheap parameters: verification cost follows the hash's own parameters.
PASSWORD_HASH = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(PASSWORD)
SECRET = "s" * 40
# 2026-11-08 08:00 UTC == 09:00 Europe/Paris.
NOW = datetime(2026, 11, 8, 8, 0, tzinfo=UTC)

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_password_hash": SecretStr(PASSWORD_HASH),
        "session_secret": SecretStr(SECRET),
        "cookie_secure": False,
        "version": "1.2.3-test",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def ok_validator(url: str) -> str:
    return "Flux de test"


class Env:
    def __init__(self) -> None:
        self.store = FakeStore()
        self.llm: FakeLLM | None = FakeLLM()
        self.validator: Callable[[str], str] = ok_validator
        self.clock = FixedClock(NOW)
        self.settings = make_settings()

    def app(self) -> FastAPI:
        return create_app(
            self.settings,
            store_factory=lambda: self.store,
            llm=self.llm,
            feed_validator=lambda url: self.validator(url),
            clock=self.clock,
        )

    def client(self) -> TestClient:
        return TestClient(self.app(), follow_redirects=False)


def csrf_from(client: TestClient, path: str = "/login") -> str:
    match = CSRF_RE.search(client.get(path).text)
    assert match, f"no csrf token on {path}"
    return match.group(1)


def login(client: TestClient, password: str = PASSWORD) -> Any:
    token = csrf_from(client, "/login")
    return client.post("/login", data={"password": password, "csrf_token": token})


@pytest.fixture
def env() -> Env:
    return Env()


@pytest.fixture
def client(env: Env) -> TestClient:
    return env.client()


@pytest.fixture
def auth_client(env: Env) -> TestClient:
    c = env.client()
    assert login(c).status_code == 303
    return c


def post(client: TestClient, path: str, data: dict[str, Any] | None = None, **kw: Any) -> Any:
    """POST a form with a valid CSRF token taken from the home page."""
    token = csrf_from(client, "/themes")
    return client.post(path, data={**(data or {}), "csrf_token": token}, **kw)
