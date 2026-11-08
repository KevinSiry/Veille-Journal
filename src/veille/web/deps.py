"""FastAPI dependencies shared by the routers."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import date
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from veille.clock import local_day
from veille.config import Settings
from veille.contracts import LLM, Clock, Store


def get_store(request: Request) -> Iterator[Store]:
    factory: Callable[[], Store] = request.app.state.store_factory
    store = factory()
    try:
        yield store
    finally:
        store.close()


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_clock(request: Request) -> Clock:
    clock: Clock = request.app.state.clock
    return clock


def get_llm(request: Request) -> LLM | None:
    llm: LLM | None = request.app.state.llm
    return llm


def today(request: Request) -> date:
    settings = get_settings(request)
    return local_day(get_clock(request).now(), settings.tz)


def parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=404, detail="Date invalide.") from None


def parse_page(value: str | None) -> int:
    try:
        page = int(value) if value else 1
    except ValueError:
        return 1
    return max(page, 1)


def parse_theme_id(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


StoreDep = Annotated[Store, Depends(get_store)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
